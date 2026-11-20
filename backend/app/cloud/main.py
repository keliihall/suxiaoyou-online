"""Cloud identity, control plane and authenticated per-tenant engine gateway."""
import asyncio
from contextlib import asynccontextmanager
from contextlib import suppress

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import http_exception_handler
from sqlalchemy import text

from app.cloud.config import CloudSettings
from app.cloud.database import Database
from app.cloud.identity import require_user, router as identity_router
from app.cloud.metering import QuotaError
from app.cloud.routes import router
from app.cloud.storage import ObjectStore
from app.cloud.runtime import RuntimeSupervisor, proxy_engine, router as runtime_router
from app.cloud.inference import router as inference_router, recover_inference_requests
from app.cloud.tenant_provider_metering import router as tenant_provider_router
from app.cloud.office_gateway import OfficeGateway, MAX_BODY_BYTES as OFFICE_BODY_LIMIT, router as office_router
from app.cloud.image_inference import router as image_router
from app.cloud.acp_gateway import router as acp_router
from app.cloud.mlx_nodes import router as mlx_nodes_router
from app.cloud import inference_payload


class BodyLimitMiddleware:
    """Bound actual received bytes, including chunked multipart without a length."""
    def __init__(self, app, upload_limit: int, runtime_limit: int):
        self.app = app
        self.upload_limit = upload_limit
        self.runtime_limit = runtime_limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        is_inference = path.startswith("/api/cloud/internal/inference/")
        if path == "/api/cloud/internal/office/render":
            limit = OFFICE_BODY_LIMIT
        elif is_inference:
            limit = inference_payload.MAX_INFERENCE_BODY_BYTES
        elif path.startswith("/api/cloud/internal/tenant-provider/"):
            limit = 4 * 1024 * 1024
        elif path == "/api/cloud/files":
            limit = self.upload_limit + 1024 * 1024
        elif path.startswith("/api/") and not path.startswith("/api/cloud/"):
            limit = self.runtime_limit
        else:
            limit = 256 * 1024
        received = 0
        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    if is_inference:
                        raise HTTPException(413, detail={"code": inference_payload.MODEL_REQUEST_TOO_LARGE})
                    raise HTTPException(413, detail={"code": "body_too_large", "message": "请求超过大小限制"})
            return message
        await self.app(scope, limited_receive, send)


def create_app(settings: CloudSettings | None = None) -> FastAPI:
    settings = settings or CloudSettings()

    @asynccontextmanager
    async def lifespan(app):
        database = Database(settings)
        app.state.settings = settings
        app.state.database = database
        app.state.object_store = ObjectStore(settings)
        maintenance = None
        try:
            await database.initialize()
            app.state.runtime_supervisor = RuntimeSupervisor(database, settings)
            app.state.office_gateway = OfficeGateway(settings)
            app.state.runtime_supervisor.office_gateway = app.state.office_gateway
            async def reclaim_expired_inference():
                while True:
                    await recover_inference_requests(database)
                    await asyncio.sleep(15)
            maintenance = asyncio.create_task(reclaim_expired_inference())
            yield
        finally:
            if maintenance:
                maintenance.cancel()
                with suppress(asyncio.CancelledError):
                    await maintenance
            await database.close()

    app = FastAPI(title="苏小有云端", version="0.1.0", lifespan=lifespan,
                  docs_url="/api/cloud/docs" if settings.environment != "production" else None,
                  openapi_url="/api/cloud/openapi.json" if settings.environment != "production" else None,
                  redoc_url=None)
    app.state.settings = settings
    app.add_middleware(BodyLimitMiddleware, upload_limit=settings.max_upload_bytes,
                      runtime_limit=settings.runtime_max_body_bytes)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if (exc.status_code == 413 and isinstance(exc.detail, dict)
                and exc.detail.get("code") == inference_payload.MODEL_REQUEST_TOO_LARGE):
            return JSONResponse(status_code=413, content=inference_payload.payload_error_body())
        return await http_exception_handler(request, exc)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # API keys and passwords are write-only even for malformed payloads.
        # Pydantic's default input/ctx members can contain the submitted secret.
        return JSONResponse(status_code=422, content={"detail": [
            {"type": issue["type"], "loc": list(issue["loc"]), "msg": issue["msg"]}
            for issue in exc.errors()
        ]})

    @app.exception_handler(QuotaError)
    async def quota_error(request: Request, exc: QuotaError):
        return JSONResponse(status_code=exc.status_code,
                            content={"detail": {"code": exc.code, "message": exc.message}})

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        if request.method == "POST" and request.url.path == "/api/cloud/files":
            # Reject unauthenticated uploads before multipart parsing can spool.
            async with request.app.state.database.sessions() as db:
                try:
                    await require_user(request, db)
                except HTTPException as exc:
                    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        # Refuse large declared bodies before multipart parsing spools files.
        try:
            length = int(request.headers.get("content-length", "0"))
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "无效请求长度"})
        max_body = max(settings.max_upload_bytes + 1024 * 1024, settings.runtime_max_body_bytes)
        if request.url.path.startswith("/api/cloud/internal/inference/"):
            max_body = inference_payload.MAX_INFERENCE_BODY_BYTES
            if length > max_body:
                return JSONResponse(status_code=413, content=inference_payload.payload_error_body())
        elif request.url.path == "/api/cloud/internal/office/render":
            max_body = OFFICE_BODY_LIMIT
        if length > max_body:
            return JSONResponse(status_code=413, content={"detail": "请求超过上传限制"})
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        # Compression proxies can buffer small SSE permission/approval events
        # until EOF. Preserve incremental delivery through the Web rewrite.
        response.headers["Cache-Control"] = (
            "no-store, no-transform"
            if response.headers.get("content-type", "").startswith("text/event-stream")
            else "no-store"
        )
        return response

    @app.get("/livez")
    async def live():
        return {"status": "ok", "mode": "cloud"}

    @app.get("/readyz")
    async def ready(request: Request):
        async with request.app.state.database.sessions() as db:
            await db.execute(text("SELECT 1"))
        return {"status": "ready", "mode": "cloud"}

    app.include_router(identity_router, prefix="/api/cloud")
    app.include_router(router, prefix="/api/cloud")
    app.include_router(runtime_router, prefix="/api/cloud")
    app.include_router(inference_router)
    app.include_router(tenant_provider_router)
    app.include_router(office_router, prefix="/api/cloud")
    app.include_router(image_router)
    app.include_router(acp_router)
    app.include_router(mlx_nodes_router)

    @app.api_route("/api/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def tenant_api(request: Request, path: str):
        return await proxy_engine(request, "/api/" + path)

    @app.get("/health")
    async def tenant_health(request: Request):
        return await proxy_engine(request, "/health")

    return app
