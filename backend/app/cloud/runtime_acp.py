"""ACP transport in the existing tenant lifespan, never a second DB owner."""
import hmac
import os

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()
MAX_FRAME = 1024 * 1024


class WebSocketLines:
    def __init__(self, socket):
        self.socket = socket

    async def readline(self):
        try:
            frame = await self.socket.receive_text()
        except WebSocketDisconnect:
            return b""
        encoded = frame.encode("utf-8")
        if len(encoded) > MAX_FRAME:
            raise ValueError("ACP frame exceeds its budget")
        return encoded.rstrip(b"\n") + b"\n"


class WebSocketWriter:
    def __init__(self, socket):
        self.socket, self.pending = socket, b""

    def write(self, encoded):
        if len(encoded) + len(self.pending) > MAX_FRAME:
            raise ValueError("ACP output exceeds its budget")
        self.pending += encoded

    async def drain(self):
        encoded, self.pending = self.pending, b""
        await self.socket.send_text(encoded.decode("utf-8"))


@router.websocket("/api/cloud-runtime/acp")
async def acp(socket: WebSocket):
    supplied = socket.headers.get("authorization", "")
    expected = os.environ.get("CLOUD_ENGINE_AUTH_TOKEN", "")
    if not expected or not supplied.startswith("Bearer ") or not hmac.compare_digest(supplied[7:], expected):
        await socket.close(code=1008)
        return
    from app.acp.server import AcpServer, acp_runtime_enabled
    from app.acp.session_bridge import ProductionSessionPromptBridge
    if not acp_runtime_enabled():
        await socket.close(code=1003)
        return
    await socket.accept()
    # All dependencies, emergency-stop controls, streams and the SQLite lease
    # belong to the already initialized original engine in this process.
    server = AcpServer(ProductionSessionPromptBridge.from_app_dependencies())
    try:
        await server.serve(WebSocketLines(socket), WebSocketWriter(socket))
    except WebSocketDisconnect:
        pass
    finally:
        from contextlib import suppress
        with suppress(RuntimeError):
            await socket.close()
