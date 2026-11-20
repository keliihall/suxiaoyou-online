"""ACP transport authentication and bounded framing before original dispatch."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect

from app.cloud.config import CloudSettings
from app.cloud.identity import create_user
from app.cloud.main import create_app
from app.cloud.runtime_acp import MAX_FRAME, WebSocketLines, WebSocketWriter, router
from captcha_helpers import sync_captcha_proof


def test_private_engine_websocket_rejects_browser_and_wrong_bearer(monkeypatch):
    monkeypatch.setenv("CLOUD_ENGINE_AUTH_TOKEN", "engine-secret-is-not-a-browser-credential")
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        for headers in ({}, {"authorization": "Bearer forged"}):
            with pytest.raises(WebSocketDisconnect) as error:
                with client.websocket_connect("/api/cloud-runtime/acp", headers=headers):
                    pass
            assert error.value.code == 1008


def test_cloud_acp_rejects_anonymous_and_cross_origin_before_runtime(tmp_path):
    app = create_app(CloudSettings(environment="test", auto_migrate=True, cookie_secure=False,
        database_url=f"sqlite+aiosqlite:///{tmp_path}/acp.db", data_dir=tmp_path / "data"))
    with TestClient(app) as client:
        endpoint = AsyncMock()
        app.state.runtime_supervisor.endpoint = endpoint
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/api/cloud/acp"):
                pass
        async def invite():
            async with app.state.database.sessions() as db:
                await create_user(db, email="acp@test.local", password="private-password-2026")
                await db.commit()
        client.portal.call(invite)
        proof = sync_captcha_proof(client, app.state.database)
        assert client.post("/api/cloud/auth/login", json={"email": "acp@test.local", "password": "private-password-2026", "captcha_proof": proof}).status_code == 200
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/api/cloud/acp", headers={"origin": "https://attacker.invalid"}):
                pass
        endpoint.assert_not_awaited()


async def test_acp_reader_and_writer_preserve_utf8_ndjson_and_limit():
    socket = SimpleNamespace(receive_text=AsyncMock(return_value='{"文本":"测试"}\n'), send_text=AsyncMock())
    assert await WebSocketLines(socket).readline() == '{"文本":"测试"}\n'.encode()
    writer = WebSocketWriter(socket)
    writer.write('{"文本":"结果"}\n'.encode())
    await writer.drain()
    socket.send_text.assert_awaited_once_with('{"文本":"结果"}\n')
    socket.receive_text.return_value = "a" * (MAX_FRAME + 1)
    with pytest.raises(ValueError):
        await WebSocketLines(socket).readline()
    with pytest.raises(ValueError):
        writer.write(b"a" * (MAX_FRAME + 1))
