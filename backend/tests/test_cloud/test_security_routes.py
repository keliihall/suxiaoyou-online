"""Negative authorization and content/ledger boundary tests for cloud APIs."""

from contextlib import AsyncExitStack

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.cloud import metering
from app.cloud.config import CloudSettings
from app.cloud.identity import create_user
from app.cloud.main import create_app
from app.cloud.models import Conversation, FileAsset, Job, JobEvent, Message, ModelRoute, RequestRecord, UsageEvent, utcnow
from app.cloud.storage import StorageError
from captcha_helpers import captcha_proof

PREFIX = "/api/cloud"
PASSWORD = "private-password-123"


@pytest_asyncio.fixture
async def secured(tmp_path):
    settings = CloudSettings(environment="test", database_url=f"sqlite+aiosqlite:///{tmp_path}/security.db", data_dir=tmp_path / "data", auto_migrate=True, cookie_secure=False, allow_mock_provider=True, max_upload_bytes=1024 * 1024)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with app.state.database.sessions() as db:
            accounts = {}
            for name, role in (("a", "user"), ("b", "user"), ("admin", "admin")):
                user = await create_user(db, email=f"{name}@example.com", password=PASSWORD, role=role)
                accounts[name] = user.id
            db.add(ModelRoute(id="mock", name="Test", provider="mock", model="mock", enabled=True, api_key_env="SUXIAOYOU_MODEL_PRIVATE_TEST", base_url="https://provider.example/api"))
            await db.commit()
        async with AsyncExitStack() as stack:
            clients = {}
            for name in accounts:
                client = await stack.enter_async_context(httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:3000"))
                proof = await captcha_proof(client, app.state.database)
                result = await client.post(PREFIX + "/auth/login", json={"email": f"{name}@example.com", "password": PASSWORD, "captcha_proof": proof})
                assert result.status_code == 200, result.text
                client.headers["X-CSRF-Token"] = result.json()["csrf_token"]
                clients[name] = client
            clients["anonymous"] = await stack.enter_async_context(httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:3000"))
            yield app, clients, accounts


async def resources(client, *, suffix="a"):
    workspace = await client.post(PREFIX + "/workspaces", json={"name": "private-" + suffix})
    assert workspace.status_code == 201, workspace.text
    workspace_id = workspace.json()["id"]
    conversation = await client.post(PREFIX + "/conversations", json={"workspace_id": workspace_id})
    assert conversation.status_code == 201, conversation.text
    upload = await client.post(PREFIX + "/files", params={"workspace_id": workspace_id}, files={"file": ("private.txt", b"private tenant content", "text/plain")})
    assert upload.status_code == 201, upload.text
    return workspace_id, conversation.json()["id"], upload.json()["id"]


async def submit(client, conversation_id, file_ids=None, key="request-unique-a"):
    return await client.post(PREFIX + "/jobs", json={"conversation_id": conversation_id, "prompt": "private prompt", "model_id": "mock", "file_ids": file_ids or [], "idempotency_key": key, "max_output_tokens": 32})


async def test_two_users_cannot_access_or_reference_each_others_resources(secured):
    _, clients, _ = secured
    a, b = clients["a"], clients["b"]
    workspace_a, conversation_a, file_a = await resources(a)
    _, conversation_b, file_b = await resources(b, suffix="b")
    job = await submit(a, conversation_a, [file_a])
    assert job.status_code == 201, job.text
    job_a = job.json()["id"]
    checks = [
        await b.get(PREFIX + "/conversations", params={"workspace_id": workspace_a}),
        await b.post(PREFIX + "/conversations", json={"workspace_id": workspace_a}),
        await b.get(PREFIX + f"/conversations/{conversation_a}"),
        await b.delete(PREFIX + f"/conversations/{conversation_a}"),
        await b.get(PREFIX + "/files", params={"workspace_id": workspace_a}),
        await b.get(PREFIX + f"/files/{file_a}/download"),
        await b.delete(PREFIX + f"/files/{file_a}"),
        await b.post(PREFIX + "/files", params={"workspace_id": workspace_a}, files={"file": ("forged.txt", b"x")}),
        await b.get(PREFIX + f"/jobs/{job_a}"),
        await b.post(PREFIX + f"/jobs/{job_a}/cancel"),
        await b.get(PREFIX + f"/jobs/{job_a}/events"),
        await submit(b, conversation_a, [file_b]),
        await submit(b, conversation_b, [file_a]),
    ]
    assert all(response.status_code == 404 for response in checks), [(r.status_code, r.text) for r in checks]
    assert all(item["id"] != workspace_a for item in (await b.get(PREFIX + "/workspaces")).json())
    assert all(item["id"] != job_a for item in (await b.get(PREFIX + "/jobs")).json())
    assert "private tenant content" not in str([r.text for r in checks])


async def test_admin_endpoints_require_admin_and_original_apis_require_private_runtime(secured):
    _, clients, _ = secured
    user, anonymous = clients["a"], clients["anonymous"]
    for endpoint in ("/admin/users", "/admin/jobs", "/admin/usage", "/admin/usage/requests", "/admin/usage/export", "/admin/models", "/admin/audit"):
        assert (await user.get(PREFIX + endpoint)).status_code == 403, endpoint
        assert (await anonymous.get(PREFIX + endpoint)).status_code == 401, endpoint
    assert (await user.post(PREFIX + "/admin/jobs/unknown/cancel")).status_code == 403
    assert (await user.post(PREFIX + "/admin/requests/unknown/reconcile", json={"action": "release", "reason": "forged"})).status_code == 403
    for endpoint in ("/api/files/content", "/api/files/attach", "/api/config/api-key", "/api/remote/enable"):
        assert (await anonymous.post(endpoint, json={"path": "/etc/passwd"})).status_code == 401, endpoint
        # Authentication alone grants no access to the control-plane host.
        assert (await clients["admin"].post(endpoint, json={"path": "/etc/passwd"})).status_code == 503, endpoint
    assert (await clients["admin"].post("/shutdown")).status_code == 404


async def test_cookie_unsafe_resource_writes_require_csrf_and_identity_is_not_json(secured):
    _, clients, accounts = secured
    a = clients["a"]
    workspace, conversation, file_id = await resources(a)
    csrf = a.headers.pop("X-CSRF-Token")
    assert (await a.post(PREFIX + "/workspaces", json={"name": "forged"})).status_code == 403
    assert (await a.delete(PREFIX + f"/files/{file_id}")).status_code == 403
    assert (await submit(a, conversation)).status_code == 403
    a.headers["X-CSRF-Token"] = csrf
    result = await a.post(PREFIX + "/workspaces", json={"name": "forged", "user_id": accounts["b"]})
    assert result.status_code == 422
    a.headers["Origin"] = "https://evil.example"
    assert (await a.post(PREFIX + "/workspaces", json={"name": "cross-site"})).status_code == 403


async def test_file_and_model_metadata_exclude_private_storage_and_credentials(secured):
    app, clients, accounts = secured
    a, b = clients["a"], clients["b"]
    workspace, _, _ = await resources(a)
    workspace_b, _, _ = await resources(b, suffix="b")
    uploads = []
    for client, workspace_id in ((a, workspace), (b, workspace_b)):
        result = await client.post(PREFIX + "/files", params={"workspace_id": workspace_id}, files={"file": ("../../shared.txt", b"same private content")})
        assert result.status_code == 201, result.text
        uploads.append(result.json())
        assert result.json()["filename"] == "shared.txt"
        assert not ({"object_key", "path", "user_id"} & result.json().keys())
        assert str(app.state.settings.data_dir) not in result.text
    assert uploads[0]["id"] != uploads[1]["id"]
    models = (await a.get(PREFIX + "/models")).json()
    assert "api_key_env" not in models[0] and "base_url" not in models[0]
    assert (await a.post(PREFIX + "/files", params={"workspace_id": workspace}, files={"file": ("active.html", b"<script>alert(1)</script>")})).status_code == 400
    for key in ("../outside/private", "/etc/passwd", "a/b/../../../outside"):
        with pytest.raises(StorageError):
            app.state.object_store._path(key)


async def test_cross_workspace_attachments_and_cross_user_idempotency_are_isolated(secured):
    _, clients, _ = secured
    a, b = clients["a"], clients["b"]
    _, conv_a, file_a = await resources(a)
    _, second_conversation, second_file = await resources(a, suffix="second")
    _, conv_b, _ = await resources(b, suffix="b")
    mismatch = await submit(a, conv_a, [second_file])
    assert mismatch.status_code == 400
    assert mismatch.json()["detail"]["code"] == "file_workspace_mismatch"
    same_key = "same-key-across-tenants"
    first, second = await submit(a, conv_a, [file_a], same_key), await submit(b, conv_b, [], same_key)
    assert first.status_code == second.status_code == 201, [first.text, second.text]
    assert first.json()["id"] != second.json()["id"]


async def test_deleting_conversation_and_file_retains_settled_usage(secured):
    app, clients, accounts = secured
    a = clients["a"]
    _, conv_id, file_id = await resources(a)
    result = await submit(a, conv_id, [file_id])
    assert result.status_code == 201
    job_id = result.json()["id"]
    assert (await a.delete(PREFIX + f"/files/{file_id}")).status_code == 409
    assert (await a.delete(PREFIX + f"/conversations/{conv_id}")).status_code == 409
    assert (await a.post(PREFIX + f"/jobs/{job_id}/cancel")).status_code == 200
    async with app.state.database.sessions() as db:
        record = await metering.reserve(db, accounts["a"], "mock", job_id, 100)
        await metering.mark_dispatched(db, record.id)
        await metering.settle(db, record.id, {"input_total": 20, "output_total": 10})
        db.add(Message(user_id=accounts["a"], conversation_id=conv_id, role="assistant", content="content to delete"))
        await db.commit()
    assert (await a.delete(PREFIX + f"/conversations/{conv_id}")).status_code == 200
    assert (await a.get(PREFIX + f"/conversations/{conv_id}")).status_code == 404
    assert (await a.delete(PREFIX + f"/files/{file_id}")).status_code == 200
    assert (await a.get(PREFIX + f"/files/{file_id}/download")).status_code == 404
    usage = (await a.get(PREFIX + "/usage")).json()
    assert usage["used_tokens"] == 30
    history = (await a.get(PREFIX + "/jobs")).json()
    deleted_job = next(item for item in history if item["id"] == job_id)
    assert deleted_job["answer"] == "" and deleted_job["file_ids"] == []
    assert deleted_job["prompt"] != "private prompt"
    async with app.state.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Message).where(Message.conversation_id == conv_id)) == 0
        assert await db.scalar(select(func.count()).select_from(UsageEvent).where(UsageEvent.user_id == accounts["a"])) == 1
        assert await db.scalar(select(func.count()).select_from(RequestRecord).where(RequestRecord.user_id == accounts["a"])) == 1
        assert (await db.get(Conversation, conv_id)).deleted_at is not None


async def test_usage_requests_and_terminal_sse_only_contain_owner_events(secured):
    app, clients, accounts = secured
    a, b = clients["a"], clients["b"]
    _, conv_a, _ = await resources(a)
    _, conv_b, _ = await resources(b, suffix="b")
    first, second = await submit(a, conv_a), await submit(b, conv_b)
    job_a, job_b = first.json()["id"], second.json()["id"]
    async with app.state.database.sessions() as db:
        a_job, b_job = await db.get(Job, job_a), await db.get(Job, job_b)
        a_job.state = b_job.state = "completed"
        db.add(JobEvent(user_id=accounts["a"], job_id=job_a, kind="completed", data={"private": "a-only-event"}))
        db.add(JobEvent(user_id=accounts["b"], job_id=job_b, kind="completed", data={"private": "b-only-event"}))
        await db.commit()
    for name, amount in (("a", 30), ("b", 7)):
        async with app.state.database.sessions() as db:
            record = await metering.reserve(db, accounts[name], "mock", None, 100)
            await metering.mark_dispatched(db, record.id)
            await metering.settle(db, record.id, {"input_total": amount, "output_total": 0})
    stream = await a.get(PREFIX + f"/jobs/{job_a}/events")
    assert stream.status_code == 200
    assert "a-only-event" in stream.text and "b-only-event" not in stream.text
    assert (await b.get(PREFIX + f"/jobs/{job_a}/events")).status_code == 404
    assert (await a.get(PREFIX + "/usage")).json()["used_tokens"] == 30
    assert (await b.get(PREFIX + "/usage")).json()["used_tokens"] == 7
    a_requests = (await a.get(PREFIX + "/usage/requests")).json()
    assert len(a_requests) == 1 and a_requests[0]["user_id"] == accounts["a"]
    assert (await clients["admin"].get(PREFIX + "/admin/usage")).json()["total_tokens"] == 37


async def test_disabled_user_cannot_reconnect_sse_or_submit(secured):
    _, clients, accounts = secured
    a, admin = clients["a"], clients["admin"]
    _, conversation, _ = await resources(a)
    job = await submit(a, conversation)
    job_id = job.json()["id"]
    update = await admin.patch(PREFIX + f"/admin/users/{accounts['a']}", json={"is_active": False, "reason": "security test"})
    assert update.status_code == 200
    assert (await a.get(PREFIX + f"/jobs/{job_id}/events")).status_code == 401
    assert (await submit(a, conversation, key="after-revocation")).status_code == 401


async def test_admin_job_metadata_does_not_include_user_content(secured):
    _, clients, _ = secured
    _, conversation, file_id = await resources(clients["a"])
    result = await submit(clients["a"], conversation, [file_id])
    assert result.status_code == 201
    jobs = (await clients["admin"].get(PREFIX + "/admin/jobs")).json()
    assert len(jobs) == 1
    assert not ({"prompt", "answer", "file_ids", "error"} & jobs[0].keys())


async def test_chunked_multipart_and_json_have_actual_byte_limit(secured):
    app, clients, _ = secured
    a = clients["a"]
    workspace, _, _ = await resources(a)
    boundary = "security-test-boundary"
    async def multipart():
        yield (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"large.txt\"\r\nContent-Type: text/plain\r\n\r\n").encode()
        for _ in range(5):
            yield b"x" * (512 * 1024)
        yield f"\r\n--{boundary}--\r\n".encode()
    response = await a.post(PREFIX + "/files", params={"workspace_id": workspace}, content=multipart(), headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    assert response.status_code == 413, response.text
    async def json_stream():
        yield b'{"name":"'
        yield b"x" * (300 * 1024)
        yield b'"}'
    response = await a.post(PREFIX + "/workspaces", content=json_stream(), headers={"Content-Type": "application/json"})
    assert response.status_code == 413, response.text


async def test_anonymous_upload_rejected_before_consuming_body(secured):
    _, clients, _ = secured
    consumed = []
    async def chunks():
        consumed.append(True)
        yield b"x" * 1024
    response = await clients["anonymous"].post(PREFIX + "/files", params={"workspace_id": "unknown"}, content=chunks(), headers={"Content-Type": "multipart/form-data; boundary=unused"})
    assert response.status_code == 401
    assert not consumed
