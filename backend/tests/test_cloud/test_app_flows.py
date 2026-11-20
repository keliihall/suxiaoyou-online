"""Real cloud HTTP + durable worker integration, without paid model calls."""
import asyncio
import hashlib
import os
from datetime import timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.engine import make_url

from app.cloud.config import CloudSettings
from app.cloud.identity import create_user
from app.cloud.main import create_app
from app.cloud.models import Conversation, Job, JobEvent, ModelRoute, RequestRecord, UsageEvent, new_id, utcnow
from app.cloud.worker import CloudWorker
from app.cloud.storage import ObjectStore, StorageError, extract_text_isolated
from app.cloud import metering
from captcha_helpers import captcha_proof

P = "/api/cloud"
PASSWORD = "cloud-test-pass-2026"


@pytest_asyncio.fixture(params=["sqlite", "postgresql"])
async def cloud(tmp_path, request):
    url = f"sqlite+aiosqlite:///{tmp_path}/cloud.db"
    control, database_name = None, None
    if request.param == "postgresql":
        configured = os.getenv("CLOUD_TEST_POSTGRES_URL")
        if not configured:
            pytest.skip("CLOUD_TEST_POSTGRES_URL not configured")
        import asyncpg
        parsed = make_url(configured)
        control = await asyncpg.connect(parsed.set(drivername="postgresql").render_as_string(hide_password=False))
        database_name = "cloud_flow_test_" + new_id()
        await control.execute(f'CREATE DATABASE "{database_name}"')
        url = parsed.set(database=database_name).render_as_string(hide_password=False)
    settings = CloudSettings(environment="test", database_url=url, data_dir=tmp_path / "data",
                             cookie_secure=False, auto_migrate=True, allow_mock_provider=True,
                             max_upload_bytes=1024 * 1024)
    app = create_app(settings)
    try:
        async with app.router.lifespan_context(app):
            db = app.state.database
            async with db.sessions() as session:
                admin = await create_user(session, email="admin@example.com", password=PASSWORD, role="admin")
                alice = await create_user(session, email="alice@example.com", password=PASSWORD)
                bob = await create_user(session, email="bob@example.com", password=PASSWORD)
                session.add(ModelRoute(id="mock-office", name="测试模型", provider="mock", model="mock-v1", strict_quota=True))
                await session.commit()
                ids = {"admin": admin.id, "alice": alice.id, "bob": bob.id}
            clients = {}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:3000") as a, \
                       httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:3000") as b, \
                       httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:3000") as c:
                for name, client in zip(("alice", "bob", "admin"), (a, b, c)):
                    proof = await captcha_proof(client, app.state.database)
                    response = await client.post(P + "/auth/login", json={"email": f"{name}@example.com", "password": PASSWORD, "captcha_proof": proof})
                    assert response.status_code == 200, response.text
                    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
                    clients[name] = client
                yield app, clients, ids, settings
    finally:
        if control:
            await control.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1 AND pid<>pg_backend_pid()", database_name)
            await control.execute(f'DROP DATABASE "{database_name}"')
            await control.close()


async def setup_chat(client):
    workspace = await client.post(P + "/workspaces", json={"name": "测试资料"})
    assert workspace.status_code == 201, workspace.text
    conversation = await client.post(P + "/conversations", json={"workspace_id": workspace.json()["id"]})
    assert conversation.status_code == 201, conversation.text
    return workspace.json(), conversation.json()


async def submit(client, conversation, **extras):
    return await client.post(P + "/jobs", json={"conversation_id": conversation["id"],
        "prompt": "请总结材料并给出计划", "model_id": "mock-office", "max_output_tokens": 256,
        "idempotency_key": new_id(), **extras})


@pytest.mark.asyncio
async def test_document_chat_usage_sse_and_deleted_content_keeps_ledger(cloud):
    app, clients, ids, settings = cloud
    alice = clients["alice"]
    workspace, conversation = await setup_chat(alice)
    upload = await alice.post(P + "/files", params={"workspace_id": workspace["id"]},
                              files={"file": ("会议纪要.txt", "目标：上线多用户云服务。".encode(), "text/plain")})
    assert upload.status_code == 201, upload.text
    asset = upload.json()
    assert "object_key" not in asset and "path" not in asset
    queued = await submit(alice, conversation, file_ids=[asset["id"]])
    assert queued.status_code == 201, queued.text
    job = queued.json()
    assert await CloudWorker(app.state.database, settings).run_once()
    result = await alice.get(P + f"/jobs/{job['id']}")
    assert result.json()["state"] == "completed", result.text
    assert "上线多用户" in result.json()["answer"]
    history = (await alice.get(P + f"/conversations/{conversation['id']}")).json()
    assert [m["role"] for m in history["messages"]] == ["user", "assistant"]
    usage = (await alice.get(P + "/usage")).json()
    assert usage["used_tokens"] > 0 and usage["reserved_tokens"] == 0
    stream = await alice.get(P + f"/jobs/{job['id']}/events")
    assert "event: delta" in stream.text and "event: completed" in stream.text
    last = int([line for line in stream.text.splitlines() if line.startswith("id:")][-1].split(":")[1])
    replay = await alice.get(P + f"/jobs/{job['id']}/events", headers={"Last-Event-ID": str(last)})
    assert not replay.text
    assert (await alice.delete(P + f"/conversations/{conversation['id']}")).status_code == 200
    assert (await alice.get(P + f"/conversations/{conversation['id']}")).status_code == 404
    after = (await alice.get(P + "/usage")).json()
    assert after["used_tokens"] == usage["used_tokens"]
    assert (await alice.get(P + f"/jobs/{job['id']}")).json()["answer"] == ""
    assert "目标" not in (await alice.get(P + f"/jobs/{job['id']}/events")).text


@pytest.mark.asyncio
async def test_cross_user_and_admin_boundaries(cloud):
    app, clients, ids, settings = cloud
    alice, bob = clients["alice"], clients["bob"]
    workspace, conversation = await setup_chat(alice)
    asset = (await alice.post(P + "/files", params={"workspace_id": workspace["id"]},
                         files={"file": ("private.txt", b"private", "text/plain")})).json()
    job = (await submit(alice, conversation)).json()
    for path in (f"/conversations/{conversation['id']}", f"/files/{asset['id']}/download",
                 f"/jobs/{job['id']}", f"/jobs/{job['id']}/events"):
        assert (await bob.get(P + path)).status_code == 404, path
    assert (await bob.post(P + f"/jobs/{job['id']}/cancel")).status_code == 404
    assert (await bob.delete(P + f"/files/{asset['id']}")).status_code == 404
    assert (await bob.post(P + "/conversations", json={"workspace_id": workspace["id"]})).status_code == 404
    assert (await bob.get(P + "/admin/users")).status_code == 403
    assert (await bob.get(P + "/files")).json() == []
    assert (await bob.get(P + "/usage")).json()["used_tokens"] == 0
    admin_job = (await clients["admin"].get(P + "/admin/jobs")).json()[0]
    assert "prompt" not in admin_job and "answer" not in admin_job
    for original_api in ("/api/config", "/api/files/content?path=/etc/passwd"):
        # No private runtime was provisioned in this fixture. These APIs must
        # never fall through to the control-plane host's filesystem/config.
        assert (await alice.get(original_api)).status_code == 503
    assert (await alice.get("/v1/chat/completions")).status_code == 404


@pytest.mark.asyncio
async def test_idempotency_queue_admission_and_cancel_events(cloud):
    app, clients, ids, settings = cloud
    alice = clients["alice"]
    _, conversation = await setup_chat(alice)
    key = new_id()
    one = await submit(alice, conversation, idempotency_key=key)
    duplicate = await submit(alice, conversation, idempotency_key=key)
    assert one.status_code == duplicate.status_code == 201
    assert one.json()["id"] == duplicate.json()["id"]
    mismatch = await submit(alice, conversation, idempotency_key=key, prompt="另一个请求")
    assert mismatch.status_code == 409
    assert (await submit(alice, conversation)).status_code == 409
    job = one.json()
    stopped = await alice.post(P + f"/jobs/{job['id']}/cancel")
    assert stopped.json()["state"] == "cancelled"
    stream = await alice.get(P + f"/jobs/{job['id']}/events")
    assert "event: cancelled" in stream.text
    assert not await CloudWorker(app.state.database, settings).run_once()


@pytest.mark.asyncio
async def test_quota_exhaustion_is_durable_and_no_provider_send(cloud):
    app, clients, ids, settings = cloud
    alice = clients["alice"]
    result = await clients["admin"].patch(P + f"/admin/users/{ids['alice']}",
                                    json={"token_limit": 1, "reason": "测试限额"})
    assert result.status_code == 200, result.text
    _, conversation = await setup_chat(alice)
    job = (await submit(alice, conversation)).json()
    await CloudWorker(app.state.database, settings).run_once()
    assert (await alice.get(P + f"/jobs/{job['id']}")).json()["state"] == "failed"
    usage = (await alice.get(P + "/usage")).json()
    assert usage["used_tokens"] == usage["reserved_tokens"] == 0
    assert (await alice.get(P + "/usage/requests")).json() == []
    # A fully depleted budget is rejected before queuing. A positive but
    # insufficient reservation is rejected by the definitive Worker gate above.
    result = await clients["admin"].patch(P + f"/admin/users/{ids['alice']}",
                                    json={"token_limit": 0, "reason": "测试零额度"})
    assert result.status_code == 200
    rejected = await submit(alice, conversation)
    assert rejected.status_code == 429
    assert rejected.json()["detail"]["code"] == "quota_exceeded"


@pytest.mark.asyncio
async def test_two_workers_claim_once_and_recover_expired_attempt(cloud):
    app, clients, ids, settings = cloud
    database = app.state.database
    _, conversation = await setup_chat(clients["alice"])
    job = (await submit(clients["alice"], conversation)).json()
    workers = [CloudWorker(database, settings) for _ in range(2)]
    claims = await asyncio.gather(*(worker.claim() for worker in workers))
    claimed = [row for row in claims if row is not None]
    assert len(claimed) == 1
    async with database.sessions() as db:
        attempt = await metering.reserve(db, ids["alice"], "mock-office", job["id"], 100)
        await metering.mark_dispatched(db, attempt.id)
        await db.execute(update(Job).where(Job.id == job["id"]).values(lease_until=utcnow()-timedelta(seconds=10)))
        await db.commit()
    assert await workers[0].recover_expired() == 1
    async with database.sessions() as db:
        assert (await db.get(RequestRecord, attempt.id)).state == "unknown"
        assert (await db.get(Job, job["id"])).state == "needs_review"
    assert (await clients["alice"].get(P + "/usage")).json()["reserved_tokens"] == 100
    assert not await workers[0].run_once()


@pytest.mark.asyncio
async def test_upload_path_and_actual_body_limits(cloud):
    app, clients, ids, settings = cloud
    alice = clients["alice"]
    workspace, _ = await setup_chat(alice)
    response = await alice.post(P + "/files", params={"workspace_id": workspace["id"]},
                                 files={"file": ("../../safe.txt", b"hello", "text/plain")})
    assert response.status_code == 201
    assert response.json()["filename"] == "safe.txt"
    response = await alice.post(P + "/files", params={"workspace_id": workspace["id"]},
                                 files={"file": ("run.py", b"print('bad')", "text/plain")})
    assert response.status_code == 400
    async def too_large():
        for _ in range(20):
            yield b"x" * 200000
    response = await alice.post(P + "/workspaces", content=too_large(), headers={"content-type": "application/json"})
    assert response.status_code == 413, response.text
    store = ObjectStore(settings)
    with pytest.raises(StorageError):
        await store.get("../../etc/passwd")


@pytest.mark.asyncio
async def test_isolated_parser_process_reads_text_and_rejects_broken_office():
    assert await extract_text_isolated("中文材料".encode(), "notes.txt") == "中文材料"
    with pytest.raises(StorageError):
        await extract_text_isolated(b"not a zip", "bad.docx")
