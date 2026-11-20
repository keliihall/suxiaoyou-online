"""Cookie-authenticated ACP WebSocket routing with continuous revocation."""
import asyncio
from contextlib import suppress

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from app.cloud.identity import check_origin, require_user
from app.cloud.runtime import RuntimeUnavailable

router = APIRouter()
MAX_FRAME = 1024 * 1024


@router.websocket("/api/cloud/acp")
async def acp(socket: WebSocket):
    # Reuse cloud identity without accepting an alternative query/bearer login.
    # Browser WebSockets carry cookies; Origin must be checked at the handshake.
    scope = dict(socket.scope, type="http", method="GET")
    request = Request(scope)
    try:
        check_origin(request)
        async with socket.app.state.database.sessions() as db:
            user = await require_user(request, db)
        base, engine_token = await socket.app.state.runtime_supervisor.endpoint(user.id)
    except (HTTPException, RuntimeUnavailable):
        await socket.close(code=1008)
        return
    try:
        async with connect(base.replace("http://", "ws://", 1) + "/api/cloud-runtime/acp",
            additional_headers={"Authorization": "Bearer " + engine_token}, proxy=None,
            max_size=MAX_FRAME, max_queue=16, open_timeout=10) as engine:
            await socket.accept()
            async def to_engine():
                while True:
                    frame = await socket.receive_text()
                    if len(frame.encode("utf-8")) > MAX_FRAME:
                        await socket.close(code=1009)
                        return
                    await engine.send(frame)
            async def to_client():
                async for frame in engine:
                    if isinstance(frame, bytes):
                        frame = frame.decode("utf-8")
                    await socket.send_text(frame)
            async def authorization():
                while True:
                    await asyncio.sleep(3)
                    async with socket.app.state.database.sessions() as db:
                        await require_user(request, db)
            tasks = [asyncio.create_task(fn()) for fn in (to_engine, to_client, authorization)]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except (OSError, ConnectionClosed, WebSocketDisconnect, TimeoutError):
        pass
    finally:
        with suppress(RuntimeError):
            await socket.close()
