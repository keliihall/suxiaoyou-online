"""Write-only custom authentication and real loopback HTTP (no external models)."""
import asyncio
import hashlib
import json
import socket
from types import SimpleNamespace
import traceback

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select

from app.cloud import metering
from app.cloud.models import AuditEvent, ModelRoute, RequestRecord, TenantRuntime, User
from app.cloud.providers import ProviderConfigurationError, build_provider
from app.cloud.secrets import ModelSecretVault
from tests.test_cloud.test_managed_model_routes import managed, P

SECRET = "synthetic-header-credential-never-a-live-secret"
HEADERS = {"X-Vendor-Token": SECRET, "X-Team": "project team"}


def payload(**changes):
    return {"id": "custom-auth", "name": "Custom", "provider": "custom_test",
        "model": "test-model", "base_url": "https://vendor.example/v1", **changes}


async def test_headers_only_encrypted_write_only_preserve_rotate_and_clear(managed):
    app, settings, admin, user = managed
    response = await admin.post(P + "/admin/models", json=payload(custom_headers=HEADERS))
    assert response.status_code == 201, response.text
    view = response.json()
    assert not view["key_configured"] and view["custom_headers_configured"]
    assert view["authentication_configured"] and view["authentication_mode"] == "headers"
    async with app.state.database.sessions() as db:
        row = await db.get(ModelRoute, "custom-auth")
        original = row.custom_headers_encrypted
        assert original and SECRET not in original and row.credential_version == 1
        assert SECRET in ModelSecretVault(settings).unseal(original)
    for client, endpoint in ((admin, "/admin/models"), (user, "/models"), (admin, "/admin/audit")):
        listing = await client.get(P + endpoint)
        assert SECRET not in listing.text and original not in listing.text
        assert "x-vendor-token" not in listing.text.lower()
    for headers in (None, "omitted"):
        body = payload(custom_headers=headers) if headers is None else payload()
        assert (await admin.put(P + "/admin/models/custom-auth", json=body)).status_code == 200
        async with app.state.database.sessions() as db:
            row = await db.get(ModelRoute, "custom-auth")
            assert row.custom_headers_encrypted == original and row.credential_version == 1
    response = await admin.put(P + "/admin/models/custom-auth", json=payload(custom_headers={"X-New": SECRET + "-new"}))
    assert response.status_code == 200
    async with app.state.database.sessions() as db:
        row = await db.get(ModelRoute, "custom-auth")
        assert row.credential_version == 2
        assert "x-vendor-token" not in ModelSecretVault(settings).unseal(row.custom_headers_encrypted)
    assert (await admin.put(P + "/admin/models/custom-auth", json=payload(custom_headers={}))).status_code == 400
    response = await admin.put(P + "/admin/models/custom-auth", json=payload(custom_headers={}, enabled=False))
    assert response.status_code == 200 and not response.json()["custom_headers_configured"]
    async with app.state.database.sessions() as db:
        row = await db.get(ModelRoute, "custom-auth")
        assert row.custom_headers_encrypted is None and row.credential_version == 3
        events = list((await db.scalars(select(AuditEvent))).all())
        serialized = json.dumps([event.details for event in events])
        assert SECRET not in serialized and original not in serialized
        assert any(e.action == "model.headers_cleared" for e in events)


@pytest.mark.parametrize("bad", [
    {"X-Token": SECRET + "\r\nX-Injected: value"}, {"X-Token": SECRET + "中文"},
    {"X-Token": SECRET + "\x00"}, {"X-Token": " " + SECRET}, {"X-Token": ""},
    {"X-Token": [SECRET]}, {"X-Token": {"secret": SECRET}}, [SECRET], SECRET,
    {"X-Token": "suxiaoyou-credential://other-user-secret"},
    {"Host": SECRET}, {"Content-Length": SECRET}, {"Proxy-Authorization": SECRET},
    {"X-Forwarded-Host": SECRET}, {"X-Test": SECRET, "x-test": SECRET},
    {SECRET + "\r\n": "test"}, {"X-Token": SECRET * 100},
])
async def test_malformed_header_422_never_echoes_raw_values_or_dict_keys(managed, caplog, bad):
    _, _, admin, _ = managed
    response = await admin.post(P + "/admin/models", json=payload(custom_headers=bad))
    assert response.status_code == 422
    assert SECRET not in response.text + caplog.text
    assert "suxiaoyou-credential://other-user-secret" not in response.text + caplog.text


async def test_ownership_clear_and_unsupported_transports_fail_closed(managed):
    app, _, admin, user = managed
    assert (await user.post(P + "/admin/models", json=payload(custom_headers=HEADERS))).status_code == 403
    created = await user.post(P + "/user/models", json=payload(custom_headers=HEADERS))
    assert created.status_code == 201 and created.json()["owner_user_id"]
    private_id = created.json()["id"]
    assert (await admin.put(P + "/admin/models/" + private_id,
        json=payload(id=private_id, custom_headers={"X-Evil": "other-owner"}))).status_code == 403
    assert private_id not in (await admin.get(P + "/user/models")).text
    assert (await user.delete(P + "/user/models/" + private_id)).status_code == 200
    async with app.state.database.sessions() as db:
        assert (await db.get(ModelRoute, private_id)).custom_headers_encrypted is None
    for provider in ("google", "anthropic", "deepseek"):
        response = await admin.post(P + "/admin/models", json=payload(provider=provider,
            api_key="synthetic-key", custom_headers=HEADERS))
        assert response.status_code == 400 and response.json()["detail"]["code"] == "custom_headers_unsupported"
    assert (await admin.post(P + "/admin/models", json=payload(base_url="http://127.0.0.1/v1", custom_headers=HEADERS))).status_code == 422
    assert (await user.post(P + "/user/models", json=payload(api_key_env="SUXIAOYOU_MODEL_OTHER_KEY", custom_headers=HEADERS))).status_code == 400


async def test_public_endpoint_requires_explicit_mode_and_no_remaining_credentials(managed):
    _, _, admin, _ = managed
    assert (await admin.post(P + "/admin/models", json=payload())).status_code == 400
    response = await admin.post(P + "/admin/models", json=payload(allow_unauthenticated=True))
    assert response.status_code == 201
    assert response.json()["authentication_mode"] == "none" and response.json()["authentication_configured"]
    assert not response.json()["key_configured"] and not response.json()["custom_headers_configured"]
    assert (await admin.put(P + "/admin/models/custom-auth",
        json=payload(allow_unauthenticated=True, custom_headers=HEADERS))).status_code == 400
    assert (await admin.put(P + "/admin/models/custom-auth",
        json=payload(provider="openai", allow_unauthenticated=True))).status_code == 400


@pytest.mark.parametrize("damaged", ["bad-ciphertext", ""])
def test_damaged_header_cipher_never_falls_back_or_echoes(tmp_path, monkeypatch, damaged):
    from tests.test_cloud.test_managed_provider_keys import settings_for
    settings = settings_for(tmp_path)
    route = SimpleNamespace(provider="custom_test", kind="chat", enabled=True, strict_quota=False,
        base_url="https://vendor.example/v1", api_key_env="", api_key_encrypted=None,
        custom_headers_encrypted=damaged, allow_unauthenticated=False)
    def forbidden(*args, **kwargs):
        pytest.fail("Broken authentication must not construct a provider")
    monkeypatch.setattr("app.provider.factory.create_provider", forbidden)
    with pytest.raises(ProviderConfigurationError) as error:
        build_provider(route, settings)
    assert damaged not in "".join(traceback.format_exception(error.value)) if damaged else True


@pytest.mark.parametrize("auth", ["headers", "authorization", "key_and_headers", "none", "error_echo"])
@pytest.mark.parametrize("private", [False, True])
async def test_actual_socket_http_receives_headers_and_ledger_never_persists_them(managed, monkeypatch, caplog, auth, private):
    """Use the real SDK, TCP server, gateway and DB, not MockTransport."""
    app, settings, admin, user = managed
    import logging
    caplog.set_level(logging.DEBUG, logger="openai")
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    captured = []
    vendor = FastAPI()
    @vendor.post("/v1/chat/completions")
    async def completion(request: Request):
        captured.append(dict(request.headers))
        if auth == "error_echo":
            return JSONResponse({"error": {"message": "vendor echoes " + SECRET}}, status_code=401)
        records = [{"id": "local", "object": "chat.completion.chunk", "created": 1, "model": "test-model",
            "choices": [{"index": 0, "delta": {"content": "LOCAL_HEADER_PROTOCOL_OK"}, "finish_reason": None}]},
            {"id": "local", "object": "chat.completion.chunk", "created": 1, "model": "test-model",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}]
        return StreamingResponse(iter(["data: " + json.dumps(row) + "\n\n" for row in records] + ["data: [DONE]\n\n"]), media_type="text/event-stream")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(vendor, log_level="error", access_log=False))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(.01)
        headers = {"authorization": "Bearer " + SECRET} if auth == "authorization" else HEADERS
        body = payload(allow_unauthenticated=True) if auth == "none" else payload(custom_headers=headers)
        if auth == "key_and_headers":
            body["api_key"] = "synthetic-separate-key"
        created = await (user if private else admin).post(P + ("/user/models" if private else "/admin/models"), json=body)
        assert created.status_code == 201, created.text
        model_id = created.json()["id"]
        async with app.state.database.sessions() as db:
            owner = await db.scalar(select(User).where(User.email == "user@example.com"))
            db.add(TenantRuntime(user_id=owner.id, capability_hash=hashlib.sha256(b"local-test-capability").hexdigest(),
                capability_encrypted="not-needed", engine_token_encrypted="not-needed", container_name="local-test", state="running"))
            route = await db.get(ModelRoute, model_id)
            # Test-only direct DB fixture substitution sends real HTTP to a
            # loopback listener. The public API still rejects this URL above.
            route.base_url = f"http://127.0.0.1:{port}/v1"
            ciphertext = route.custom_headers_encrypted
            await db.commit()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:3000",
                headers={"Authorization": "Bearer local-test-capability"}) as runtime:
            response = await runtime.post(P + "/internal/inference/v1/chat/completions", json={
                "model": model_id, "messages": [{"role": "user", "content": "local test"}], "stream": False, "max_tokens": 16})
        assert len(captured) == 1
        if auth == "none":
            assert "authorization" not in captured[0] and "x-vendor-token" not in captured[0]
        elif auth == "authorization":
            assert captured[0]["authorization"] == "Bearer " + SECRET
        else:
            assert captured[0]["x-vendor-token"] == SECRET and captured[0]["x-team"] == "project team"
            assert captured[0].get("authorization") == ("Bearer synthetic-separate-key" if auth == "key_and_headers" else None)
        if auth == "error_echo":
            assert response.status_code == 502
        else:
            assert response.status_code == 200 and response.json()["choices"][0]["message"]["content"] == "LOCAL_HEADER_PROTOCOL_OK"
        async with app.state.database.sessions() as db:
            rows = list((await db.scalars(select(RequestRecord))).all())
            assert len(rows) == 1 and rows[0].state == ("unknown" if auth == "error_echo" else "settled")
            assert rows[0].total_tokens == (0 if auth == "error_echo" else 5)
            serialized = json.dumps([{"price": r.price_snapshot, "usage": r.normalized_usage} for r in rows])
        assert SECRET not in response.text + serialized + caplog.text
        assert not ciphertext or ciphertext not in serialized
    finally:
        server.should_exit = True
        await task
        listener.close()


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
async def test_0007_additive_migration_retains_existing_credentials_and_reaches_expected_revision(tmp_path, dialect):
    import os
    import secrets
    from alembic import command
    from sqlalchemy import text
    from sqlalchemy.engine import make_url
    from app.cloud.config import CloudSettings
    from app.cloud.database import Database, migration_config, SCHEMA_REVISION
    url = f"sqlite+aiosqlite:///{tmp_path}/migration.sqlite"
    control = None
    name = None
    if dialect == "postgresql":
        configured = os.getenv("CLOUD_TEST_POSTGRES_URL")
        if not configured:
            pytest.skip("Dedicated PostgreSQL test endpoint is not configured")
        import asyncpg
        parsed = make_url(configured)
        control = await asyncpg.connect(parsed.set(drivername="postgresql").render_as_string(hide_password=False))
        name = "cloud_headers_test_" + secrets.token_hex(8)
        await control.execute(f'CREATE DATABASE "{name}"')
        url = parsed.set(database=name).render_as_string(hide_password=False)
    database = Database(CloudSettings(environment="test", database_url=url, data_dir=tmp_path / "migration-data"))
    try:
        async with database.engine.begin() as connection:
            await connection.run_sync(lambda c: _migrate_to(c, url, "cloud_0006"))
            await connection.execute(text("INSERT INTO cloud_model_routes (id,name,provider,model,enabled,api_key_env,api_key_encrypted,credential_version,strict_quota,input_price_microusd_per_million,output_price_microusd_per_million,created_at,kind,image_price_microusd,capabilities) VALUES ('existing','Existing','deepseek','model',true,'','opaque-old-ciphertext',7,false,0,0,CURRENT_TIMESTAMP,'chat',0,'{}')"))
        async with database.engine.begin() as connection:
            await connection.run_sync(lambda c: _migrate_to(c, url, "head"))
        await database.initialize()
        async with database.engine.connect() as connection:
            row = (await connection.execute(text("SELECT api_key_encrypted,credential_version,custom_headers_encrypted,allow_unauthenticated FROM cloud_model_routes WHERE id='existing'"))).one()
            assert tuple(row[:3]) == ("opaque-old-ciphertext", 7, None)
            assert not row[3]
            assert (await connection.execute(text("SELECT version_num FROM cloud_alembic_version"))).scalar_one() == SCHEMA_REVISION == "cloud_0008"
    finally:
        await database.close()
        if control is not None:
            if name is not None:
                await control.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
            await control.close()


def _migrate_to(connection, url, revision):
    from alembic import command
    from app.cloud.database import migration_config
    config = migration_config(url)
    config.attributes["connection"] = connection
    command.upgrade(config, revision)
