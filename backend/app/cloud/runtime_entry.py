"""Launch the original single-user engine inside one private tenant container.

This module is not imported by the cloud control-plane application. The
supervisor supplies an internal API token and a separately scoped inference
capability. Neither is a provider key or a browser credential.
"""

from __future__ import annotations

import hmac
import json
import os
from pathlib import Path
from urllib.parse import urlsplit


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value or any(ord(char) < 32 for char in value):
        raise RuntimeError(f"{name} must be configured by the runtime supervisor")
    return value


def _bootstrap() -> tuple[Path, Path, str, str, str]:
    root = Path(os.environ.get("CLOUD_ENGINE_TENANT_ROOT", "/tenant"))
    if not root.is_absolute() or root.is_symlink():
        raise RuntimeError("The tenant root must be a private absolute directory")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = root.resolve(strict=True)
    home, state, workspace = root / "home", root / "state", root / "workspaces"
    for directory in (home, state, state / "data", state / "tmp", workspace):
        if directory.is_symlink():
            raise RuntimeError("Runtime directories must not be symbolic links")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    auth = _required("CLOUD_ENGINE_AUTH_TOKEN")
    if not auth.startswith("suxiaoyou_st_") or len(auth) < 50:
        raise RuntimeError("CLOUD_ENGINE_AUTH_TOKEN must be a strong session token")
    capability = _required("CLOUD_ENGINE_CAPABILITY")
    if len(capability) < 32:
        raise RuntimeError("CLOUD_ENGINE_CAPABILITY is too short")
    gateway = _required("CLOUD_ENGINE_GATEWAY_URL").rstrip("/")
    parsed = urlsplit(gateway)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise RuntimeError("CLOUD_ENGINE_GATEWAY_URL must be an HTTP API base URL")
    os.umask(0o077)
    os.environ["HOME"] = str(home)
    os.environ["XDG_CACHE_HOME"] = str(home / ".cache")
    os.environ["XDG_CONFIG_HOME"] = str(home / ".config")
    os.environ["XDG_DATA_HOME"] = str(home / ".local/share")
    os.environ["MPLCONFIGDIR"] = str(home / ".cache/matplotlib")
    os.environ["TMPDIR"] = str(state / "tmp")
    os.environ["SUXIAOYOU_PRIVATE_DATA_DIR"] = str(state)
    os.environ["SUXIAOYOU_MANAGED_WORKSPACE_ROOT"] = str(state / "managed-workspaces")
    os.environ["CLOUD_ENGINE_WORKSPACE_ROOT"] = str(workspace)
    os.chdir(state)
    return state, workspace, auth, capability, gateway


class _AuthenticatedLoopback:
    """Reclassify only the supervisor-authenticated connection as local.

    Original local-only dependencies continue to require the original rotating
    bearer. No forwarded peer headers participate in this decision.
    """

    def __init__(self, app, *, token: str):
        self.app, self.token = app, token

    async def __call__(self, scope, receive, send):
        if scope.get("type") in {"http", "websocket"}:
            headers = dict(scope.get("headers", []))
            supplied = headers.get(b"authorization", b"").decode("latin-1")
            if supplied.startswith("Bearer ") and hmac.compare_digest(supplied[7:].encode("latin-1"), self.token.encode("utf-8")):
                scope = dict(scope, client=("127.0.0.1", 0))
        await self.app(scope, receive, send)



class _ManagedModelBoundary:
    """Keep all paid model routes under platform identity and accounting."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            path, method = scope.get("path", ""), scope.get("method", "GET")
            blocked = path.startswith("/api/remote")
            if method not in {"GET", "HEAD", "OPTIONS"}:
                blocked = blocked or path in {"/api/config/api-key", "/api/config/local", "/api/config/custom"}
                blocked = blocked or path.startswith("/api/config/custom/") or path.startswith("/api/config/providers/")
            if blocked:
                from starlette.responses import JSONResponse
                response = JSONResponse(status_code=409, content={"detail": {
                    "code": "cloud_managed_models", "message": "请在云端账户的个人模型或管理后台配置供应商",
                    "management_path": "/cloud"}})
                return await response(scope, receive, send)
        await self.app(scope, receive, send)


def _generation_defaults(settings):
    # Reasoning and visible text share max_tokens. The desktop's 8K default
    # can be consumed by thinking alone. Cloud runtimes allow 64K, still
    # clamped to each model/context and Goal/specialist budgets downstream.
    # Explicit operator settings, including tenant .env, remain authoritative.
    if "hard_max_output_tokens" not in settings.model_fields_set:
        settings.hard_max_output_tokens = 65_536
    # Session utilities read the separate process-cached config accessor.
    # Pin the resolved operator/default ceiling before any generation starts,
    # so both access paths use the same value (including explicit .env caps).
    os.environ["SUXIAOYOU_HARD_MAX_OUTPUT_TOKENS"] = str(settings.hard_max_output_tokens)
    from app.config import get_settings
    get_settings.cache_clear()
    return settings


def create_app():
    state, workspace, auth, capability, gateway = _bootstrap()
    # Import after environment setup: several original services read their
    # process-owned private roots when the modules are imported.
    from app.cloud.tenant_provider_bridge import install_tenant_provider_metering
    install_tenant_provider_metering()
    from app.cloud.gateway_provider import install_gateway_provider
    install_gateway_provider()
    from app.cloud.runtime_ollama import install_private_ollama
    install_private_ollama()
    from app.config import Settings, get_custom_endpoints
    from app.main import create_app as create_original_app
    from app.cloud.runtime_files import router

    settings = _generation_defaults(Settings(
        _env_file=state / ".env",
        database_url=f"sqlite+aiosqlite:///{state / 'data/suxiaoyou.db'}",
        project_dir=str(workspace),
        host="0.0.0.0",
        port=int(os.environ.get("CLOUD_ENGINE_PORT", "8080")),
        session_token_path=str(state / "session_token.json"),
        allow_dev_session_token=True,
        dev_session_token=auth,
        rapid_mlx_auto_start=False,
        remote_access_enabled=False,
        extra_allowed_origins=os.environ.get("CLOUD_ENGINE_ALLOWED_ORIGIN", ""),
    ))
    # Paid BYOK credentials are configured centrally, including personal
    # routes. Never register an old .env key or custom endpoint beside the
    # metered gateway. Subscription/Ollama/Rapid-MLX are preserved and receive
    # their separate central observer in the tenant-provider integration.
    for field in Settings.model_fields:
        if field.endswith("_api_key") and field != "brave_search_api_key":
            setattr(settings, field, "")
    settings.local_base_url = ""
    # A Linux engine must never instantiate or adopt an arbitrary local MLX
    # server from an old desktop .env. The exclusive Mac adapter owns this path.
    settings.rapid_mlx_base_url = ""
    endpoints = []
    endpoints.append({
        "id": "custom_cloud", "slug": "cloud", "name": "云端模型",
        "base_url": gateway, "api_key": capability, "enabled": True,
        # The platform's authorized route listing is the source of truth.
        "models": [], "headers": {},
    })
    settings.custom_endpoints = json.dumps(endpoints, ensure_ascii=False)
    from app.cloud.runtime_image import install_cloud_image_tool
    install_cloud_image_tool(gateway=gateway, capability=capability)
    app = create_original_app(settings)
    from app.cloud.office_provider import install_cloud_office_lifespan
    install_cloud_office_lifespan(app)
    from app.cloud.runtime_mlx import install_cloud_mlx
    install_cloud_mlx(app, gateway=gateway, capability=capability, engine_token=auth)
    app.add_middleware(_ManagedModelBoundary)
    app.state.cloud_runtime = True
    app.state.cloud_workspace_root = workspace
    app.include_router(router, prefix="/api/cloud-runtime/files")
    from app.cloud.runtime_acp import router as acp_router
    app.include_router(acp_router)
    app.add_middleware(_AuthenticatedLoopback, token=auth)
    return app


def main() -> None:
    import uvicorn

    uvicorn.run(
        create_app(), host="0.0.0.0",
        port=int(os.environ.get("CLOUD_ENGINE_PORT", "8080")),
        proxy_headers=False, access_log=False,
    )


if __name__ == "__main__":
    main()
