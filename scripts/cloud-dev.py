#!/usr/bin/env python3
"""Initialize cloud development services and configure private real-model keys."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
CONFIG = ROOT / ".cloud-dev.json"
MODEL_SECRETS = ROOT / ".env.cloud.local"
MODEL_KEY_NAME = re.compile(r"SUXIAOYOU_MODEL_[A-Z0-9_]+")
PRESETS = {
    "deepseek": {"id": "deepseek-office", "name": "DeepSeek", "provider": "deepseek",
                 "model": "deepseek-flash", "base_url": "https://api.deepseek.com/v1",
                 "api_key_env": "SUXIAOYOU_MODEL_DEEPSEEK_KEY"},
}
sys.path.insert(0, str(BACKEND))


def read_private(path: Path) -> str:
    """Read a private regular file without following a symlink."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor) as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise ValueError(f"{path.name} must be a private regular file (chmod 600)")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise ValueError(f"{path.name} must be owned by the current user")
        return handle.read()


def write_private(path: Path, text: str) -> None:
    """Atomically replace configuration with a 0600 file."""
    temporary = path.with_name(path.name + ".tmp-" + secrets.token_hex(8))
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_configuration(data) -> None:
    write_private(CONFIG, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def model_secrets() -> dict[str, str]:
    """Only model-key assignments are allowed; values are never interpolated."""
    try:
        text = read_private(MODEL_SECRETS)
    except FileNotFoundError:
        return {}
    result, seen = {}, set()
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        name, separator, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if not separator or not MODEL_KEY_NAME.fullmatch(name) or name in seen:
            raise ValueError("Private model file contains an invalid or duplicate variable name")
        seen.add(name)
        if value.startswith('"'):
            try:
                value = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                raise ValueError("Private model file contains an invalid quoted value") from None
        # Empty placeholders are unconfigured, never usable credentials.
        if value != "":
            result[name] = validate_key(value)
    return result


def validate_key(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 8192 or any(char in value for char in "\r\n\0"):
        raise ValueError("Model API key must be a non-empty, single-line value")
    if value != value.strip():
        raise ValueError("Model API key must not contain leading or trailing whitespace")
    return value


def configuration():
    if not CONFIG.exists():
        raise SystemExit("Run backend/.venv/bin/python scripts/cloud-dev.py init first")
    data = json.loads(read_private(CONFIG))
    return data


async def initialize(args):
    from app.cloud.config import CloudSettings
    from app.cloud.database import Database
    from app.cloud.identity import create_user
    from app.cloud.models import AuditEvent, ModelRoute, User
    from sqlalchemy import select
    prior = configuration() if CONFIG.exists() else {}
    database_url = args.database_url or prior.get("database_url") or f"sqlite+aiosqlite:///{BACKEND / 'data/cloud-dev/cloud.db'}"
    api_port = args.api_port or prior.get("api_port") or 8018
    web_port = args.web_port or prior.get("web_port") or 3018
    allow_mock = bool(args.allow_mock)
    settings = CloudSettings(environment="development", database_url=database_url,
        data_dir=Path(prior.get("data_dir", BACKEND / "data/cloud-dev")),
        auto_migrate=True, allow_mock_provider=allow_mock,
        cookie_secure=False, allowed_origin=f"http://localhost:{web_port}")
    database = Database(settings)
    try:
        await database.initialize()
        password = prior.get("password") or secrets.token_urlsafe(18)
        async with database.sessions() as db:
            email = prior.get("email", "admin@suxiaoyou.test")
            user = await db.scalar(select(User).where(User.email == email))
            if user is None:
                user = await create_user(db, email=email, password=password,
                                  display_name="测试管理员", role="admin", token_limit=2_000_000)
            if allow_mock:
                route = await db.get(ModelRoute, "mock-office")
                if route is None:
                    db.add(ModelRoute(id="mock-office", name="模拟模型（仅显式开发测试）", provider="mock",
                                      model="mock-v1", strict_quota=True))
                elif route.provider == "mock":
                    route.enabled = True
                else:
                    raise ValueError("mock-office already identifies a real route; no changes made")
                db.add(AuditEvent(actor_user_id=user.id, action="dev.mock.enable",
                                  details={"model_id": "mock-office", "source": "cloud-dev.init"}))
            else:
                await disable_mocks(db, user.id, source="cloud-dev.init")
            await db.commit()
        result = dict(prior, database_url=database_url, data_dir=str(settings.data_dir),
                      api_port=api_port, web_port=web_port, email=email, password=password,
                      allow_mock_provider=allow_mock)
        # Credentials never go into tracked files or shell command arguments.
        write_configuration(result)
        print(f"Initialized cloud test environment; credentials: {CONFIG}")
        print(f"Web: http://localhost:{web_port}/cloud")
        print(f"API: http://127.0.0.1:{api_port}/api/cloud/docs")
        print("Mock provider explicitly enabled." if allow_mock else "Mock provider disabled. Configure a real model before submitting work.")
    finally:
        await database.close()


async def disable_mocks(db, actor_id: str, *, source: str):
    from app.cloud.models import AuditEvent, ModelRoute
    from sqlalchemy import select
    rows = await db.scalars(select(ModelRoute).where(ModelRoute.provider == "mock", ModelRoute.enabled.is_(True)))
    for route in rows:
        route.enabled = False
        db.add(AuditEvent(actor_user_id=actor_id, action="dev.mock.disable",
                          details={"model_id": route.id, "source": source}))


async def configure_model(args):
    from app.cloud.config import CloudSettings
    from app.cloud.database import Database
    from app.cloud.models import AuditEvent, ModelRoute, User
    from app.cloud.routes import RouteCreate
    from app.provider.catalog import PROVIDER_CATALOG
    from sqlalchemy import select
    data = configuration()
    provider = args.provider or args.preset
    preset = PRESETS.get(provider, {})
    if not provider or provider == "mock" or (provider != "openai" and provider not in PROVIDER_CATALOG and not provider.startswith("custom_")):
        raise ValueError("Choose a supported real provider (for example --preset deepseek)")
    model = args.model or preset.get("model")
    if not model:
        raise ValueError("--model is required for this provider")
    body = RouteCreate(id=args.id or preset.get("id", "office-model"),
        name=args.name or preset.get("name", "办公模型"), provider=provider, model=model,
        api_key_env=args.api_key_env or preset.get("api_key_env", "SUXIAOYOU_MODEL_TEAM_KEY"),
        base_url=args.base_url or preset.get("base_url"), strict_quota=False,
        input_price_microusd_per_million=args.input_price,
        output_price_microusd_per_million=args.output_price)
    definition = PROVIDER_CATALOG.get(provider)
    if not body.base_url and (provider.startswith("custom_") or (definition and definition.kind == "openai_compat_azure")):
        raise ValueError("--base-url is required for this provider")
    stored = model_secrets()
    if args.key_file:
        key = read_private(args.key_file).rstrip("\r\n")
    elif args.key_source_env:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.key_source_env):
            raise ValueError("--key-source-env must identify an environment variable")
        key = os.environ.get(args.key_source_env, "")
    else:
        key = os.environ.get(body.api_key_env, "") or stored.get(body.api_key_env, "")
        if not key and not args.no_prompt:
            key = getpass.getpass("Model API key (hidden): ")
    key = validate_key(key)
    database = Database(CloudSettings(environment="development", database_url=data["database_url"],
                                    data_dir=data["data_dir"], allow_mock_provider=False))
    try:
        await database.initialize()
        async with database.sessions() as db:
            admin = await db.scalar(select(User).where(User.email == data["email"], User.role == "admin", User.is_active.is_(True)))
            if admin is None:
                raise ValueError("The configured local administrator must be active")
            route = await db.get(ModelRoute, body.id)
            route_values = body.model_dump(exclude={"api_key", "clear_api_key", "custom_headers"})
            if route is None:
                route = ModelRoute(**route_values, api_key_encrypted=None, credential_version=1)
                db.add(route)
                action = "model.create"
            else:
                for name, value in route_values.items():
                    setattr(route, name, value)
                # Explicit CLI provisioning switches this route to the local
                # environment source, replacing any previous managed key.
                route.api_key_encrypted = None
                route.credential_version = (route.credential_version or 0) + 1
                action = "model.update"
            await disable_mocks(db, admin.id, source="cloud-dev.configure-model")
            db.add(AuditEvent(actor_user_id=admin.id, action=action,
                details={"model_id": body.id, "provider": provider, "model": model,
                         "api_key_env": body.api_key_env, "source": "cloud-dev.configure-model"}))
            stored[body.api_key_env] = key
            write_private(MODEL_SECRETS, "# Private model keys; no interpolation; do not commit.\n" +
                          "".join(f"{name}={json.dumps(value)}\n" for name, value in sorted(stored.items())))
            # Configuration contains references only. Disable mock before the
            # DB commit so a failed commit cannot accidentally restore it.
            data.update(allow_mock_provider=False,
                        model_route={"id": body.id, "provider": provider, "model": model,
                                     "api_key_env": body.api_key_env})
            write_configuration(data)
            await db.commit()
        print(f"Configured real model route {body.id} ({provider}/{model}); key stored privately in {MODEL_SECRETS.name}.")
        print("Mock routes disabled. Restart API and Worker explicitly to load the private key; no model request was sent.")
    finally:
        await database.close()


def environment(data, *, include_model_secrets=False):
    env = dict(os.environ)
    if include_model_secrets:
        env.update(model_secrets())
    else:
        # Web builds and the frontend server do not need provider credentials.
        env = {name: value for name, value in env.items()
               if not name.startswith("SUXIAOYOU_MODEL_") and name != "SUXIAOYOU_CLOUD_MODEL_MASTER_KEY"}
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(key, None)
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env.update({
        "SUXIAOYOU_CLOUD_ENVIRONMENT": "development",
        "SUXIAOYOU_CLOUD_DATABASE_URL": data["database_url"],
        "SUXIAOYOU_CLOUD_DATA_DIR": data["data_dir"],
        "SUXIAOYOU_CLOUD_ALLOWED_ORIGIN": f"http://localhost:{data['web_port']}",
        "SUXIAOYOU_CLOUD_COOKIE_SECURE": "false",
        "SUXIAOYOU_CLOUD_ALLOW_MOCK_PROVIDER": "true" if data.get("allow_mock_provider") is True else "false",
        "SUXIAOYOU_CLOUD_AUTO_MIGRATE": "false",
        "NEXT_PUBLIC_CLOUD_MODE": "true",
        "NEXT_PUBLIC_API_URL": f"http://127.0.0.1:{data['api_port']}",
    })
    for name in ("runtime_docker_host", "runtime_image", "runtime_gateway_url", "runtime_office_url"):
        if data.get(name):
            env["SUXIAOYOU_CLOUD_" + name.upper()] = data[name]
    return env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--database-url")
    init.add_argument("--api-port", type=int)
    init.add_argument("--web-port", type=int)
    init.add_argument("--allow-mock", action="store_true", help="Explicitly enable and seed the network-free development mock")
    configure = commands.add_parser("configure-model", help="Configure a real model; keys never appear in command-line arguments")
    configure.add_argument("--preset", choices=sorted(PRESETS))
    configure.add_argument("--provider")
    configure.add_argument("--model")
    configure.add_argument("--id")
    configure.add_argument("--name")
    configure.add_argument("--base-url")
    configure.add_argument("--api-key-env")
    configure.add_argument("--input-price", type=int, default=0, help="Micro-USD per million input tokens; zero means price unknown")
    configure.add_argument("--output-price", type=int, default=0, help="Micro-USD per million output tokens; zero means price unknown")
    source = configure.add_mutually_exclusive_group()
    source.add_argument("--key-source-env", help="Read key from this existing environment variable")
    source.add_argument("--key-file", type=Path, help="Read key from a private 0600 regular file")
    configure.add_argument("--no-prompt", action="store_true", help="Fail if no environment or saved key is available")
    commands.add_parser("api")
    commands.add_parser("worker")
    commands.add_parser("web")
    commands.add_parser("build")
    args = parser.parse_args()
    if args.command in {"init", "configure-model"}:
        try:
            return asyncio.run(initialize(args) if args.command == "init" else configure_model(args))
        except (ValueError, OSError) as exc:
            # Pydantic errors may include arbitrary user input. Do not print
            # their repr or a traceback containing private configuration.
            from pydantic import ValidationError
            message = "Invalid model route configuration" if isinstance(exc, ValidationError) else str(exc)
            parser.exit(1, message + "\n")
    data = configuration()
    env = environment(data, include_model_secrets=args.command in {"api", "worker"})
    if args.command in {"web", "build"}:
        os.chdir(ROOT / "frontend")
        argv = ["npm", "run", "build"] if args.command == "build" else ["npm", "run", "start", "--", "--hostname", "127.0.0.1", "--port", str(data["web_port"])]
    else:
        os.chdir(BACKEND)
        argv = [sys.executable, "-m", "app.cloud.worker"] if args.command == "worker" else [
            sys.executable, "-m", "uvicorn", "app.cloud.main:create_app", "--factory",
            "--host", "127.0.0.1", "--port", str(data["api_port"])]
    os.execvpe(argv[0], argv, env)


if __name__ == "__main__":
    main()
