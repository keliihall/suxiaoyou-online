"""Private subscription/node receipts cannot alter platform-paid attempts."""
import hashlib
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest_asyncio
import pytest
from sqlalchemy import select

from app.cloud import metering
from app.cloud.config import CloudSettings
from app.cloud.database import Database
from app.cloud.models import CloudBase, RequestRecord, TenantRuntime, UsageEvent, User
from app.cloud.tenant_provider_metering import router
from app.schemas.provider import StreamChunk

PREFIX = "/api/cloud/internal/tenant-provider"
CAPABILITY = "private-runtime-capability-for-receipts"


@pytest_asyncio.fixture
async def gateway(tmp_path):
    settings = CloudSettings(environment="test", database_url=f"sqlite+aiosqlite:///{tmp_path}/local.db")
    database = Database(settings)
    async with database.engine.begin() as connection:
        await connection.run_sync(CloudBase.metadata.create_all)
    async with database.sessions() as db:
        db.add(User(id="owner", email="owner@test.local", display_name="Owner", password_hash="unused",
                    token_limit=1000000, max_concurrent_jobs=1))
        await db.flush()
        db.add(TenantRuntime(user_id="owner", capability_hash=hashlib.sha256(CAPABILITY.encode()).hexdigest(),
            capability_encrypted="unused", engine_token_encrypted="unused", container_name="private", state="running"))
        await db.commit()
    app = FastAPI()
    app.state.database, app.state.settings = database, settings
    app.include_router(router)
    @app.exception_handler(metering.QuotaError)
    async def quota_error(request, error):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=error.status_code, content={"code": error.code})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost",
        headers={"authorization": "Bearer " + CAPABILITY}) as client:
        yield client, database
    await database.close()


def payload(**kwargs):
    return {"provider_id": "openai-subscription", "model": "gpt-owned", "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 128, **kwargs}


async def test_private_provider_counts_original_receipt_once(gateway):
    client, database = gateway
    response = await client.post(PREFIX + "/admit", json=payload())
    assert response.status_code == 200, response.text
    request_id = response.json()["request_id"]
    assert (await client.post(PREFIX + "/heartbeat", json={"request_id": request_id})).status_code == 200
    receipt = {"request_id": request_id, "usage": {"input_total": 100, "output_total": 30, "total_tokens": 130}}
    for _ in range(2):
        assert (await client.post(PREFIX + "/finish", json=receipt)).json()["status"] == "settled"
    async with database.sessions() as db:
        records = (await db.scalars(select(RequestRecord))).all()
        events = (await db.scalars(select(UsageEvent))).all()
        assert len(records) == len(events) == 1
        assert records[0].source == "tenant-provider" and records[0].total_tokens == 130
        assert records[0].raw_usage["receipt_source"] == "tenant_runtime"
        assert (await metering.usage_summary(db, "owner"))["used_tokens"] == 130


async def test_private_receipt_cannot_settle_platform_paid_attempt(gateway):
    client, database = gateway
    async with database.sessions() as db:
        platform = await metering.reserve(db, "owner", "shared-platform-model", None, 1000, source="agent")
        await metering.mark_dispatched(db, platform.id)
    response = await client.post(PREFIX + "/finish", json={"request_id": platform.id,
        "usage": {"input_total": 0, "output_total": 0, "total_tokens": 0}})
    assert response.status_code == 404
    async with database.sessions() as db:
        assert (await db.get(RequestRecord, platform.id)).state == "dispatched"


async def test_missing_receipt_retains_reservation_and_concurrency(gateway):
    client, database = gateway
    first = await client.post(PREFIX + "/admit", json=payload(provider_id="ollama"))
    assert first.status_code == 200
    assert (await client.post(PREFIX + "/admit", json=payload())).status_code == 429
    request_id = first.json()["request_id"]
    assert (await client.post(PREFIX + "/finish", json={"request_id": request_id})).json()["status"] == "unknown"
    async with database.sessions() as db:
        summary = await metering.usage_summary(db, "owner")
        assert summary["reserved_tokens"] > 0 and summary["used_tokens"] == 0


async def test_receipts_require_scoped_runtime_identity(gateway):
    client, _ = gateway
    client.headers.pop("authorization")
    assert (await client.post(PREFIX + "/admit", json=payload())).status_code == 401


async def test_provider_bridge_preserves_type_and_executes_after_admission(monkeypatch):
    from app.cloud.tenant_provider_bridge import install_tenant_provider_metering
    from app.provider.registry import ProviderRegistry
    original_register = ProviderRegistry.register
    monkeypatch.setattr(ProviderRegistry, "register", original_register)
    monkeypatch.setattr(ProviderRegistry, "_cloud_metering_installed", False, raising=False)
    monkeypatch.setenv("CLOUD_ENGINE_GATEWAY_URL", "http://control/api/cloud/internal/inference/v1")
    monkeypatch.setenv("CLOUD_ENGINE_CAPABILITY", CAPABILITY)
    calls = []
    def control(request):
        calls.append(request.url.path)
        assert request.headers["authorization"] == "Bearer " + CAPABILITY
        return httpx.Response(200, json={"request_id": "r" * 32, "status": "settled"})
    client_class = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_class(transport=httpx.MockTransport(control), **kwargs))
    async def stream(model, messages, **kwargs):
        assert calls == [PREFIX + "/admit"]
        yield StreamChunk(type="text-delta", data={"text": "private-provider"})
        yield StreamChunk(type="usage", data={"input": 100, "output": 30, "reasoning": 5, "total": 130})
    provider = SimpleNamespace(id="openai-subscription", stream_chat=stream)
    install_tenant_provider_metering()
    ProviderRegistry().register(provider)
    chunks = [chunk async for chunk in provider.stream_chat("owned-model", [{"role": "user", "content": "hello"}])]
    assert len(chunks) == 2 and calls == [PREFIX + "/admit", PREFIX + "/finish"]
    assert isinstance(provider, SimpleNamespace)


@pytest.mark.parametrize("code", ["quota_exceeded", "concurrency_exceeded", "account_disabled"])
async def test_provider_admission_rejects_before_sdk_and_disables_retry(monkeypatch, code):
    from app.cloud.tenant_provider_bridge import install_tenant_provider_metering
    from app.provider.registry import ProviderRegistry
    monkeypatch.setattr(ProviderRegistry, "register", ProviderRegistry.register)
    monkeypatch.setattr(ProviderRegistry, "_cloud_metering_installed", False, raising=False)
    monkeypatch.setenv("CLOUD_ENGINE_GATEWAY_URL", "http://control/api/cloud/internal/inference/v1")
    monkeypatch.setenv("CLOUD_ENGINE_CAPABILITY", CAPABILITY)
    client_class = httpx.AsyncClient
    def reject(request):
        return httpx.Response(429, json={"detail": {"code": code, "message": "PRIVATE_ERROR"}})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client_class(transport=httpx.MockTransport(reject), **kw))
    async def forbidden_sdk(*args, **kwargs):
        raise AssertionError("Rejected requests must never reach the provider")
        yield  # async generator protocol
    provider = SimpleNamespace(id="ollama", stream_chat=forbidden_sdk)
    install_tenant_provider_metering()
    ProviderRegistry().register(provider)
    chunks = [chunk async for chunk in provider.stream_chat("local-model", [])]
    assert len(chunks) == 1 and chunks[0].type == "error"
    assert chunks[0].data["code"] == code and chunks[0].data["retryable"] is False
    assert "PRIVATE_ERROR" not in str(chunks[0].data)


async def test_owned_sdk_has_one_http_attempt_per_admission(monkeypatch):
    """A real SDK 500 must not silently dispatch extra unmetered retries."""
    import json
    from openai import AsyncOpenAI
    from app.cloud.tenant_provider_bridge import install_tenant_provider_metering
    from app.provider.ollama import OllamaProvider
    from app.provider.registry import ProviderRegistry

    monkeypatch.setattr(ProviderRegistry, "register", ProviderRegistry.register)
    monkeypatch.setattr(ProviderRegistry, "_cloud_metering_installed", False, raising=False)
    monkeypatch.setenv("CLOUD_ENGINE_GATEWAY_URL", "http://control/api/cloud/internal/inference/v1")
    monkeypatch.setenv("CLOUD_ENGINE_CAPABILITY", CAPABILITY)
    client_class = httpx.AsyncClient
    upstream_calls, control_calls = [], []
    def upstream(request):
        upstream_calls.append(request.url.path)
        return httpx.Response(500, json={"error": {"message": "upstream failed", "type": "server_error"}})
    def control(request):
        control_calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"request_id": "r" * 32, "status": "unknown"})

    provider = OllamaProvider()
    await provider._client.close()
    provider._client = AsyncOpenAI(api_key="ollama", base_url="http://private-ollama/v1",
        http_client=client_class(transport=httpx.MockTransport(upstream)))
    assert provider._client.max_retries > 0
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client_class(transport=httpx.MockTransport(control), **kw))
    install_tenant_provider_metering()
    ProviderRegistry().register(provider)
    try:
        assert provider._client.max_retries == 0
        chunks = [chunk async for chunk in provider.stream_chat("owned-model", [{"role": "user", "content": "hello"}])]
        assert len(upstream_calls) == 1
        assert len(chunks) == 1 and chunks[0].type == "error"
        assert [path for path, _ in control_calls] == [PREFIX + "/admit", PREFIX + "/finish"]
        assert control_calls[-1][1]["usage"] is None
    finally:
        await provider._client.close()
