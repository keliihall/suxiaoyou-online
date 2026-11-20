"""Cloud cookie/CSRF and tenant routing boundaries for original engine APIs."""
from contextlib import AsyncExitStack

import httpx
import pytest
import pytest_asyncio

from app.cloud.config import CloudSettings
from app.cloud.identity import create_user
from app.cloud.main import create_app
from app.cloud import runtime
from captcha_helpers import captcha_proof


@pytest_asyncio.fixture
async def gateway(tmp_path, monkeypatch):
    app = create_app(CloudSettings(environment="test", cookie_secure=False, auto_migrate=True,
        database_url=f"sqlite+aiosqlite:///{tmp_path}/gateway.db", data_dir=tmp_path / "data"))
    original_client, received, owners = httpx.AsyncClient, [], {}
    async with app.router.lifespan_context(app):
        async with app.state.database.sessions() as db:
            for name in ("alice", "bob"):
                user = await create_user(db, email=name + "@test.local", password="private-password-2026")
                owners[name] = user.id
            await db.commit()
        async def endpoint(user_id):
            return "http://" + user_id + ".internal", "private-engine-" + user_id
        monkeypatch.setattr(app.state.runtime_supervisor, "endpoint", endpoint)
        def upstream(request):
            received.append(request)
            return httpx.Response(200, headers={"content-type": "application/json", "set-cookie": "private=forbidden"},
                json={"engine": request.url.host, "path": request.url.path})
        async with AsyncExitStack() as stack:
            clients = {}
            for name in ("alice", "bob"):
                client = await stack.enter_async_context(original_client(
                    transport=httpx.ASGITransport(app=app), base_url="http://localhost:3000"))
                proof = await captcha_proof(client, app.state.database)
                response = await client.post("/api/cloud/auth/login", json={
                    "email": name + "@test.local", "password": "private-password-2026", "captcha_proof": proof})
                client.headers["x-csrf-token"] = response.json()["csrf_token"]
                clients[name] = client
            clients["anonymous"] = await stack.enter_async_context(original_client(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:3000"))
            monkeypatch.setattr(runtime.httpx, "AsyncClient", lambda **kw:
                original_client(transport=httpx.MockTransport(upstream), **kw))
            yield app, clients, owners, received


async def test_original_apis_always_resolve_own_engine_and_strip_credentials(gateway):
    _, clients, owners, received = gateway
    for name in ("alice", "bob"):
        response = await clients[name].get("/api/conversations?token=forged&session_token=x&query=hello",
            headers={"authorization": "Bearer forged-client-token"})
        assert response.status_code == 200
        assert response.json()["engine"] == owners[name] + ".internal"
        assert "set-cookie" not in response.headers
        request = received[-1]
        assert request.headers["authorization"] == "Bearer private-engine-" + owners[name]
        assert all(header not in request.headers for header in ("cookie", "x-csrf-token", "origin"))
        assert str(request.url.query) == "b'query=hello'"
    assert len(received) == 2


async def test_original_apis_cannot_bypass_cloud_session_or_csrf(gateway):
    _, clients, _, received = gateway
    assert (await clients["anonymous"].get("/api/config")).status_code == 401
    clients["alice"].headers.pop("x-csrf-token")
    assert (await clients["alice"].post("/api/files/attach", json={"path": "/etc/passwd"})).status_code == 403
    assert (await clients["bob"].post("/api/config", json={}, headers={"origin": "https://attacker.invalid"})).status_code == 403
    assert not received


async def test_unknown_control_routes_never_reach_engine(gateway):
    _, clients, _, received = gateway
    assert (await clients["alice"].get("/api/cloud/nonexistent")).status_code == 404
    assert not received


async def test_revoked_session_cannot_access_original_engine(gateway):
    _, clients, _, received = gateway
    assert (await clients["alice"].post("/api/cloud/auth/logout")).status_code == 200
    assert (await clients["alice"].get("/api/conversations")).status_code == 401
    assert not received


async def test_sse_disables_transform_buffering(gateway, monkeypatch):
    app, clients, _, _ = gateway
    from fastapi.responses import StreamingResponse
    @app.get("/test-live-events")
    async def events():
        async def stream():
            yield 'event: permission-request\ndata: {}\n\n'
        return StreamingResponse(stream(), media_type="text/event-stream")
    response = await clients["alice"].get("/test-live-events")
    assert response.headers["cache-control"] == "no-store, no-transform"
    assert "permission-request" in response.text
