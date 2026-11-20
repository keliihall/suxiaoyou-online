#!/usr/bin/env python3
"""Export only enabled shared DeepSeek chat routes to private deployment files.

Run from the repository with backend/.venv-cloud-check/bin/python and provide
the deployment's exact HTTPS --origin. This script never writes the source
database, creates a model master key, or copies source users/business data.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
from pathlib import Path
import secrets
import stat
import sys
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[2]
DEPLOYMENT_PARENT = Path("/Users/kelii/.suxiaoyou-deployments")
DEPLOYMENT_NAME = "192.168.1.211"
SEED_NAME = "bootstrap-seed.json"
CREDENTIALS_NAME = "credentials.json"
ROUTE_COPY_FIELDS = (
    "id", "name", "provider", "kind", "model", "enabled", "base_url",
    "capabilities", "strict_quota", "image_price_microusd",
    "input_price_microusd_per_million", "output_price_microusd_per_million",
)


class ExportRefused(Exception):
    pass


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ExportRefused
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def origin_argument():
    if len(sys.argv) != 3 or sys.argv[1] != "--origin":
        raise ExportRefused
    origin = sys.argv[2].rstrip("/")
    parsed = urlsplit(origin)
    if (parsed.scheme != "https" or not parsed.hostname or not parsed.netloc
            or parsed.username or parsed.password or parsed.path
            or parsed.query or parsed.fragment
            or any(character.isspace() for character in origin)):
        raise ExportRefused
    # Validate the port without ever displaying a potentially sensitive input.
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ExportRefused
    return origin


def check_owned(info, *, directory, private=True):
    correct_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not correct_type(info.st_mode) or info.st_uid != os.getuid():
        raise ExportRefused
    mode = stat.S_IMODE(info.st_mode)
    if private:
        if mode != (0o700 if directory else 0o600):
            raise ExportRefused
    elif mode & 0o022:
        raise ExportRefused


def existing_master_key(data_dir):
    """Read an already existing private key via descriptors; never auto-create."""
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    data_fd = os.open(data_dir, directory_flags)
    try:
        check_owned(os.fstat(data_fd), directory=True, private=False)
        secrets_fd = os.open("secrets", directory_flags, dir_fd=data_fd)
        try:
            check_owned(os.fstat(secrets_fd), directory=True)
            key_fd = os.open(
                "model-master.key", os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                dir_fd=secrets_fd,
            )
            with os.fdopen(key_fd, "rb") as handle:
                check_owned(os.fstat(handle.fileno()), directory=False)
                key = handle.read(128)
        finally:
            os.close(secrets_fd)
    finally:
        os.close(data_fd)
    return key.decode("ascii")


async def prepare_documents(origin):
    dev = load_module("suxiaoyou_export_cloud_dev", ROOT / "scripts/cloud-dev.py")
    bootstrap = load_module(
        "suxiaoyou_export_bootstrap_seed", ROOT / "deploy/cloud/bootstrap-seed.py")

    from pydantic import SecretStr
    from sqlalchemy import select, text

    from app.cloud.config import CloudSettings
    from app.cloud.database import Database
    from app.cloud.models import ModelRoute
    from app.cloud.providers import route_api_key
    from app.cloud.secrets import ModelSecretVault

    config = dev.configuration()
    data_dir = Path(config["data_dir"])
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    # Passing the existing key explicitly prevents the vault's fallback from
    # creating a replacement key even if the source file disappears afterward.
    settings = CloudSettings(
        environment="development", database_url=config["database_url"],
        data_dir=data_dir, model_master_key=SecretStr(existing_master_key(data_dir)),
        auto_migrate=False, allow_mock_provider=False,
    )
    ModelSecretVault(settings)
    database = Database(settings)
    try:
        await database.initialize()
        async with database.sessions() as db:
            if settings.database_url.startswith("postgresql+asyncpg://"):
                await db.execute(text("SET TRANSACTION READ ONLY"))
            elif settings.database_url.startswith("sqlite+aiosqlite://"):
                await db.execute(text("PRAGMA query_only=ON"))
            else:
                raise ExportRefused
            routes = (await db.scalars(select(ModelRoute).where(
                ModelRoute.enabled.is_(True), ModelRoute.provider == "deepseek",
                ModelRoute.kind == "chat", ModelRoute.owner_user_id.is_(None),
            ).order_by(ModelRoute.id))).all()
            if not routes or len(routes) > bootstrap.MAX_ROUTES:
                raise ExportRefused
            # Fail before resolving any credential if unsupported state exists.
            if any(route.custom_headers_encrypted is not None for route in routes):
                raise ExportRefused

            private_environment = dev.model_secrets()
            exported = []
            for route in routes:
                source_name = route.api_key_env
                saved = os.environ.get(source_name) if source_name else None
                try:
                    if source_name and source_name in private_environment:
                        os.environ[source_name] = private_environment[source_name]
                    credential = route_api_key(route, settings)
                finally:
                    if source_name:
                        if saved is None:
                            os.environ.pop(source_name, None)
                        else:
                            os.environ[source_name] = saved
                item = {field: getattr(route, field) for field in ROUTE_COPY_FIELDS}
                item.update(api_key=credential, api_key_env="", allow_unauthenticated=False)
                exported.append(item)
    finally:
        await database.close()

    password = secrets.token_urlsafe(30)
    email = config.get("email") or "admin@suxiaoyou.test"
    seed = {
        "schema_version": 1,
        "admin": {
            "email": email, "password": password, "display_name": "管理员",
            "token_limit": 2_000_000, "image_limit": 100, "max_concurrent_jobs": 2,
        },
        "routes": exported,
    }
    validated_admin, _ = bootstrap.validate_document(seed)
    seed["admin"]["email"] = validated_admin["email"]
    credentials = {
        "origin": origin, "email": validated_admin["email"], "password": password,
    }
    seed_bytes = (json.dumps(seed, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    if len(seed_bytes) > bootstrap.MAX_INPUT_BYTES:
        raise ExportRefused
    credential_bytes = (json.dumps(credentials, ensure_ascii=False, indent=2) + "\n").encode()
    return seed_bytes, credential_bytes, len(exported)


def private_output_directory():
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    home_fd = os.open(DEPLOYMENT_PARENT.parent, flags)
    try:
        check_owned(os.fstat(home_fd), directory=True, private=False)
        try:
            os.mkdir(DEPLOYMENT_PARENT.name, 0o700, dir_fd=home_fd)
        except FileExistsError:
            pass
        parent_fd = os.open(DEPLOYMENT_PARENT.name, flags, dir_fd=home_fd)
    finally:
        os.close(home_fd)
    try:
        check_owned(os.fstat(parent_fd), directory=True)
        try:
            os.mkdir(DEPLOYMENT_NAME, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        target_fd = os.open(DEPLOYMENT_NAME, flags, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)
    try:
        check_owned(os.fstat(target_fd), directory=True)
    except BaseException:
        os.close(target_fd)
        raise
    return target_fd


def write_documents(seed_bytes, credential_bytes):
    directory_fd = private_output_directory()
    descriptors, created = [], []
    try:
        # Reserve both paths before writing. O_EXCL also refuses existing
        # symlinks; existing files are never opened, replaced or chmodded.
        for name in (SEED_NAME, CREDENTIALS_NAME):
            descriptor = os.open(
                name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600, dir_fd=directory_fd,
            )
            descriptors.append(descriptor)
            created.append(name)
            os.fchmod(descriptor, 0o600)
        for descriptor, content in zip(descriptors, (seed_bytes, credential_bytes)):
            remaining = memoryview(content)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise ExportRefused
                remaining = remaining[written:]
            os.fsync(descriptor)
        os.fsync(directory_fd)
    except BaseException:
        for name in created:
            try:
                os.unlink(name, dir_fd=directory_fd)
            except OSError:
                pass
        raise
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
        os.close(directory_fd)


def main():
    logging.disable(logging.CRITICAL)
    try:
        origin = origin_argument()
        seed_bytes, credential_bytes, count = asyncio.run(prepare_documents(origin))
        write_documents(seed_bytes, credential_bytes)
    except BaseException:
        # Fixed output only: no exception text, arguments, source config,
        # Pydantic input, credentials, SQL parameters or traceback.
        sys.stderr.write("导出失败：请检查参数、源配置、已存在的私有主密钥及目标文件权限；不会覆盖现有文件。\n")
        return 1
    sys.stdout.write(json.dumps({
        "status": "exported", "admin_count": 1, "route_count": count,
    }, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
