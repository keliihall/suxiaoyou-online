"""Gateway HTTP/ledger contracts; no paid model requests or credentials."""
import asyncio
import hashlib
import json
import os
from datetime import timedelta

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from sqlalchemy import select, update
from sqlalchemy.engine import make_url

from app.cloud import inference, metering
from app.cloud.config import CloudSettings
from app.cloud.database import Database
from app.cloud.models import CloudBase, ModelRoute, RequestRecord, TenantRuntime, UsageEvent, User, utcnow
from app.schemas.provider import StreamChunk

BASE = "/api/cloud/internal/inference/v1"
CAPABILITY = "synthetic-runtime-capability"


def receipt(partial=False):
    return {"input_total": 100, "output_total": 30, "total_tokens": 130,
            "cache_read": 40, "cache_write": 0, "reasoning": 8,
            "usage_status": "partial" if partial else "complete", "raw_usage": {"total": 130}}


class Vendor:
    def __init__(self):
        self.tools, self.usage, self.partial, self.error, self.pause = False, True, False, False, False
        self.calls, self.closed = 0, False
        self.entered, self.resume = asyncio.Event(), asyncio.Event()

    async def stream_chat(self, model, messages, **kwargs):
        self.calls += 1
        self.model, self.messages, self.kwargs = model, messages, kwargs
        yield StreamChunk(type="reasoning-delta", data={"text": "分析"})
        if self.tools:
            yield StreamChunk(type="tool-call", data={"id": "call_read", "name": "read", "arguments": {"path": "data.csv"}})
        else:
            yield StreamChunk(type="text-delta", data={"text": "协议测试"})
        self.entered.set()
        if self.pause:
            await self.resume.wait()
        if self.usage:
            yield StreamChunk(type="usage", data=receipt(self.partial))
            yield StreamChunk(type="usage", data=receipt(self.partial))
        if self.error:
            raise RuntimeError("VENDOR_SECRET_MUST_NEVER_APPEAR")
        yield StreamChunk(type="finish", data={"reason": "tool_calls" if self.tools else "stop"})

    async def aclose(self):
        self.closed = True


@pytest_asyncio.fixture(params=["sqlite", "postgresql"])
async def gateway(tmp_path, monkeypatch, request):
    url, control, database_name = f"sqlite+aiosqlite:///{tmp_path}/gateway.db", None, None
    if request.param == "postgresql":
        configured = os.getenv("CLOUD_TEST_POSTGRES_URL")
        if not configured:
            pytest.skip("CLOUD_TEST_POSTGRES_URL not configured")
        import asyncpg
        parsed = make_url(configured)
        control = await asyncpg.connect(parsed.set(drivername="postgresql").render_as_string(hide_password=False))
        from app.cloud.models import new_id
        database_name = "cloud_inference_test_" + new_id()
        await control.execute(f'CREATE DATABASE "{database_name}"')
        url = parsed.set(database=database_name).render_as_string(hide_password=False)
    settings = CloudSettings(environment="test", database_url=url,
                             data_dir=tmp_path / "files", cookie_secure=False)
    database = Database(settings)
    async with database.engine.begin() as connection:
        await connection.run_sync(CloudBase.metadata.create_all)
    async with database.sessions() as db:
        db.add(User(id="owner", email="owner@test.local", display_name="Owner", password_hash="unused", token_limit=1000000,
                    max_concurrent_jobs=1))
        await db.flush()
        db.add(TenantRuntime(user_id="owner", capability_hash=hashlib.sha256(CAPABILITY.encode()).hexdigest(),
            capability_encrypted="not-a-key", engine_token_encrypted="not-a-key", container_name="test", state="running"))
        db.add(ModelRoute(id="deepseek-team", name="团队模型", provider="deepseek", model="deepseek-flash",
            api_key_env="SYNTHETIC_NEVER_READ", input_price_microusd_per_million=1000000,
            output_price_microusd_per_million=2000000))
        db.add(ModelRoute(id="hidden", name="Hidden", provider="deepseek", model="hidden", enabled=False))
        db.add(ModelRoute(id="mock", name="Mock", provider="mock", model="mock"))
        await db.commit()
    app = FastAPI()
    app.state.settings, app.state.database = settings, database
    app.include_router(inference.router)
    vendor = Vendor()
    monkeypatch.setattr(inference, "build_provider", lambda *_: vendor)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost",
                                  headers={"Authorization": "Bearer " + CAPABILITY}) as client:
        yield app, database, client, vendor
    await database.close()
    if control:
        await control.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1 AND pid<>pg_backend_pid()", database_name)
        await control.execute(f'DROP DATABASE "{database_name}"')
        await control.close()


def payload(**values):
    return {"model": "deepseek-team", "messages": [{"role": "system", "content": "系统"},
            {"role": "user", "content": "读取 data.csv"}], "max_tokens": 256, **values}


async def records(database):
    async with database.sessions() as db:
        rows = (await db.scalars(select(RequestRecord))).all()
        events = (await db.scalars(select(UsageEvent))).all()
        return rows, events, await metering.usage_summary(db, "owner")


async def test_models_require_runtime_capability_not_cookie(gateway):
    _, database, client, vendor = gateway
    response = await client.get(BASE + "/models")
    assert [row["id"] for row in response.json()["data"]] == ["deepseek-team"]
    assert "SYNTHETIC" not in response.text and "api_key" not in response.text
    client.headers.pop("Authorization")
    client.cookies.set("suxiaoyou_cloud_session", "administrator-cookie")
    assert (await client.get(BASE + "/models")).status_code == 401
    assert vendor.calls == 0 and (await records(database))[0] == []


async def test_nonstream_reasoning_usage_and_alias_metered(gateway):
    _, database, client, vendor = gateway
    response = await client.post(BASE + "/chat/completions", json=payload())
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["object"] == "chat.completion" and data["model"] == "deepseek-team"
    assert data["choices"][0]["message"] == {"role": "assistant", "content": "协议测试", "reasoning_content": "分析"}
    assert data["usage"]["total_tokens"] == 130 and data["usage"]["prompt_tokens_details"]["cached_tokens"] == 40
    rows, events, summary = await records(database)
    assert len(rows) == len(events) == vendor.calls == 1
    assert rows[0].source == "agent" and rows[0].job_id is None and rows[0].state == "settled"
    assert rows[0].lease_owner and rows[0].lease_until
    assert (summary["used_tokens"], summary["reserved_tokens"], summary["total_cost_microusd"]) == (130, 0, 160)
    assert vendor.closed and vendor.model == "deepseek-flash"
    assert response.headers["X-Request-ID"] == rows[0].id


async def test_stream_tools_openai_protocol_and_schema_reservation(gateway):
    _, database, client, vendor = gateway
    vendor.tools = True
    tools = [{"type": "function", "function": {"name": "read", "description": "x" * 4000,
             "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]
    response = await client.post(BASE + "/chat/completions", json=payload(stream=True, tools=tools,
                 stream_options={"include_usage": True}, tool_choice="auto", reasoning={"enabled": False}))
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
    frames = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
    assert frames[-1] == "[DONE]"
    frames = [json.loads(row) for row in frames[:-1]]
    call = next(row for row in frames if row["choices"] and "tool_calls" in row["choices"][0]["delta"])
    call = call["choices"][0]["delta"]["tool_calls"][0]
    assert call["index"] == 0 and call["id"] == "call_read" and call["function"]["name"] == "read"
    assert json.loads(call["function"]["arguments"]) == {"path": "data.csv"}
    assert frames[-2]["choices"][0]["finish_reason"] == "tool_calls"
    assert frames[-1]["choices"] == [] and frames[-1]["usage"]["total_tokens"] == 130
    assert vendor.kwargs["tools"] == tools and vendor.kwargs["extra_body"]["thinking"] == {"type": "disabled"}
    rows, events, _ = await records(database)
    assert rows[0].reserved_tokens > 4000 and len(events) == 1


@pytest.mark.parametrize("values", [{"usage": False}, {"partial": True}, {"usage": False, "error": True}])
async def test_incomplete_calls_preserve_unknown(gateway, values):
    _, database, client, vendor = gateway
    for key, value in values.items():
        setattr(vendor, key, value)
    response = await client.post(BASE + "/chat/completions", json=payload())
    assert response.status_code == 502 and "VENDOR_SECRET" not in response.text
    rows, events, summary = await records(database)
    assert rows[0].state == "unknown" and summary["reserved_tokens"] > 0 and events == []
    assert vendor.closed and vendor.calls == 1


async def test_error_after_receipt_charged_redacted(gateway):
    _, database, client, vendor = gateway
    vendor.error = True
    response = await client.post(BASE + "/chat/completions", json=payload(stream=True))
    assert "VENDOR_SECRET" not in response.text and '"error"' in response.text
    rows, events, summary = await records(database)
    assert rows[0].state == "settled" and len(events) == 1 and summary["used_tokens"] == 130


async def test_zero_quota_no_dispatch(gateway):
    _, database, client, vendor = gateway
    async with database.sessions() as db:
        await db.execute(update(User).values(token_limit=0))
        await db.commit()
    response = await client.post(BASE + "/chat/completions", json=payload())
    assert response.status_code == 429 and vendor.calls == 0 and vendor.closed
    assert (await records(database))[0] == []


async def test_parallel_calls_share_atomic_concurrency(gateway):
    _, database, client, vendor = gateway
    vendor.pause = True
    first = asyncio.create_task(client.post(BASE + "/chat/completions", json=payload()))
    await asyncio.wait_for(vendor.entered.wait(), 3)
    second = await client.post(BASE + "/chat/completions", json=payload())
    assert second.status_code == 429 and second.json()["error"]["code"] == "concurrency_exceeded"
    vendor.resume.set()
    assert (await first).status_code == 200
    assert vendor.calls == 1 and len((await records(database))[0]) == 1


@pytest.mark.parametrize("change", ["disabled", "rotated", "stopped"])
async def test_disabled_or_revoked_runtime_cannot_call(gateway, change):
    _, database, client, vendor = gateway
    async with database.sessions() as db:
        if change == "disabled":
            await db.execute(update(User).values(is_active=False))
        else:
            await db.execute(update(TenantRuntime).values(**({"capability_hash": "different"} if change == "rotated" else {"state": "stopped"})))
        await db.commit()
    assert (await client.post(BASE + "/chat/completions", json=payload())).status_code == 401
    assert vendor.calls == 0


@pytest.mark.parametrize("model", ["mock", "hidden", "unknown"])
async def test_only_enabled_real_routes(gateway, model):
    _, database, client, vendor = gateway
    assert (await client.post(BASE + "/chat/completions", json=payload(model=model))).status_code == 404
    assert vendor.calls == 0 and (await records(database))[0] == []


@pytest.mark.parametrize("extra", [{"base_url": "http://attacker"}, {"user_id": "another"}, {"n": 2}, {"max_tokens": True},
    {"tools": [{"type": "deepseek_web_search"}]}, {"tool_choice": "required"}])
async def test_invalid_payload_no_dispatch(gateway, extra):
    _, database, client, vendor = gateway
    assert (await client.post(BASE + "/chat/completions", json=payload(**extra))).status_code == 422
    assert vendor.calls == 0 and (await records(database))[0] == []


async def test_cancel_dispatched_call_unknown_and_transport_closed(gateway):
    app, database, _, vendor = gateway
    vendor.pause = True
    async with database.sessions() as db:
        user, route = await db.get(User, "owner"), await db.get(ModelRoute, "deepseek-team")
    request = Request({"type": "http", "app": app, "headers": [(b"authorization", ("Bearer " + CAPABILITY).encode())]})
    attempt = inference.InferenceAttempt(request, inference.ChatCompletionRequest(**payload()), user, route, vendor)
    await attempt.reserve()
    async def consume():
        return [chunk async for chunk in attempt.chunks()]
    task = asyncio.create_task(consume())
    await asyncio.wait_for(vendor.entered.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    rows, events, summary = await records(database)
    assert rows[0].state == "unknown" and summary["reserved_tokens"] > 0 and vendor.closed
    assert vendor.calls == 1 and events == []


async def test_recovery_fences_only_expired_no_resend(gateway):
    _, database, _, vendor = gateway
    async with database.sessions() as db:
        reserved = await metering.reserve(db, "owner", "deepseek-team", None, 1000, source="agent",
                lease_owner="old", lease_until=utcnow() - timedelta(seconds=10))
        sent = await metering.reserve(db, "owner", "deepseek-team", None, 1000, source="agent",
                lease_owner="old", lease_until=utcnow() - timedelta(seconds=10))
        await metering.mark_dispatched(db, sent.id)
        live = await metering.reserve(db, "owner", "deepseek-team", None, 1000, source="agent",
                lease_owner="another-live-api", lease_until=utcnow() + timedelta(seconds=100))
    assert await inference.recover_inference_requests(database) == 2
    assert await inference.recover_inference_requests(database) == 0
    rows, events, summary = await records(database)
    assert {r.id: r.state for r in rows} == {reserved.id: "released", sent.id: "unknown", live.id: "reserved"}
    assert vendor.calls == 0 and summary["reserved_tokens"] == 2000 and events == []
    async with database.sessions() as db:
        await metering.settle(db, sent.id, receipt())
        await metering.settle(db, sent.id, receipt())
    assert len((await records(database))[1]) == 1


async def test_cancel_after_reserve_commit_releases_known_reservation(gateway, monkeypatch):
    _, database, client, vendor = gateway
    committed, release = asyncio.Event(), asyncio.Event()
    original = metering.reserve
    async def pause(*args, **kwargs):
        record = await original(*args, **kwargs)
        committed.set()
        await release.wait()
        return record
    monkeypatch.setattr(metering, "reserve", pause)
    task = asyncio.create_task(client.post(BASE + "/chat/completions", json=payload(stream=True)))
    await asyncio.wait_for(committed.wait(), 3)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    rows, events, summary = await records(database)
    assert rows[0].state == "released" and summary["reserved_tokens"] == 0
    assert vendor.calls == 0 and vendor.closed and events == []


async def test_heartbeat_revokes_running_request(gateway, monkeypatch):
    app, database, _, vendor = gateway
    vendor.pause = True
    monkeypatch.setattr(inference, "_LEASE_SECONDS", 0.4)
    async with database.sessions() as db:
        user, route = await db.get(User, "owner"), await db.get(ModelRoute, "deepseek-team")
    request = Request({"type": "http", "app": app, "headers": [(b"authorization", ("Bearer " + CAPABILITY).encode())]})
    attempt = inference.InferenceAttempt(request, inference.ChatCompletionRequest(**payload()), user, route, vendor)
    await attempt.reserve()
    async def consume():
        return [chunk async for chunk in attempt.chunks()]
    task = asyncio.create_task(consume())
    await asyncio.wait_for(vendor.entered.wait(), 3)
    async with database.sessions() as db:
        await db.execute(update(TenantRuntime).values(capability_hash="revoked"))
        await db.commit()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 3)
    rows, events, summary = await records(database)
    assert rows[0].state == "unknown" and summary["reserved_tokens"] > 0
    assert vendor.calls == 1 and vendor.closed and events == []
