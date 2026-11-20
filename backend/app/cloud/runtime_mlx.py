"""Original Rapid-MLX UI backed by the user's exclusive managed Mac node."""
from contextlib import asynccontextmanager, suppress
import hmac

import httpx
from openai import AsyncOpenAI
from starlette.responses import JSONResponse


def node_gateway(gateway):
    return gateway.rstrip("/").removesuffix("/inference/v1") + "/mlx-node"


def install_cloud_mlx(app, *, gateway, capability, engine_token):
    from app.provider.rapid_mlx import RapidMLXProvider
    from app.schemas.provider import ProviderStatus
    base = node_gateway(gateway)

    class ManagedMacProvider(RapidMLXProvider):
        # The central node proxy observes raw SDK usage and reserves quota.
        # Wrapping this a second time in the tenant observer would double bill.
        _cloud_metered = True
        _cloud_gateway_managed = True

        def __init__(self, model):
            self._base_url, self._seed_model = base + "/v1", model
            self._models_cache = None
            self._client = AsyncOpenAI(api_key=capability, base_url=self._base_url, max_retries=0,
                http_client=httpx.AsyncClient(trust_env=False), timeout=httpx.Timeout(300, connect=10))

        async def health_check(self):
            try:
                data = await self._client.models.list()
                return ProviderStatus(status="connected", model_count=len(data.data))
            except Exception:
                return ProviderStatus(status="error", error="专属 Mac 节点未就绪")

    async def update_provider(data):
        registry = app.state.provider_registry
        if not data.get("running"):
            registry.unregister("rapid-mlx")
            return
        current = registry.get_provider("rapid-mlx")
        model = data.get("current_model") or "qwen3.5-4b"
        if not isinstance(current, ManagedMacProvider) or current._seed_model != model:
            registry.register(ManagedMacProvider(model))
        with suppress(Exception):
            await registry.refresh_provider("rapid-mlx")

    original_lifespan = app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(application):
        async with original_lifespan(application):
            # Engine start must work with no node or an offline node; the UI
            # reports unavailable and never invents a locally runnable MLX.
            with suppress(httpx.HTTPError, ValueError):
                async with httpx.AsyncClient(trust_env=False, timeout=10,
                    headers={"Authorization": "Bearer " + capability}) as client:
                    response = await client.get(base + "/rapid-mlx/status")
                    if response.status_code == 200:
                        await update_provider(response.json())
            yield
    app.router.lifespan_context = lifespan

    class ManagedMacApi:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            path = scope.get("path", "")
            if scope.get("type") != "http" or not path.startswith("/api/rapid-mlx/"):
                return await self.app(scope, receive, send)
            supplied = dict(scope.get("headers", [])).get(b"authorization", b"")
            if not hmac.compare_digest(supplied, ("Bearer " + engine_token).encode()):
                return await JSONResponse({"detail": "Invalid engine credential"}, status_code=401)(scope, receive, send)
            from starlette.requests import Request
            request = Request(scope, receive)
            # The central router validates allowed operations/methods and body.
            operation = path.removeprefix("/api/rapid-mlx/")
            if operation not in {"status", "cached", "start", "stop", "remove", "uninstall"}:
                return await JSONResponse({"detail": "Unknown node operation"}, status_code=404)(scope, receive, send)
            try:
                from app.cloud.mlx_node_server import MAX_BODY
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > MAX_BODY:
                        return await JSONResponse({"detail": "Node payload too large"}, status_code=413)(scope, receive, send)
                async with httpx.AsyncClient(trust_env=False, timeout=130,
                    headers={"Authorization": "Bearer " + capability}) as client:
                    response = await client.request(request.method, base + "/rapid-mlx/" + operation,
                        content=bytes(body), params=request.query_params,
                        headers={"Content-Type": "application/json"})
                data = response.json()
                if response.status_code == 200:
                    if operation in {"status", "start", "stop"}:
                        await update_provider(data)
                    elif operation == "uninstall":
                        app.state.provider_registry.unregister("rapid-mlx")
                return await JSONResponse(data, status_code=response.status_code)(scope, receive, send)
            except (httpx.HTTPError, ValueError):
                return await JSONResponse({"detail": "专属 Mac 节点连接不可用"}, status_code=503)(scope, receive, send)
    app.add_middleware(ManagedMacApi)
