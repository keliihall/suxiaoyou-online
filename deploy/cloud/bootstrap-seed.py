#!/usr/bin/env python3
"""Seed one fresh production cloud database from private, bounded stdin JSON.

Run in the API container. The source can be supplied with ``python -c`` while
stdin carries the document; credentials must never appear in command arguments.
No local development configuration, users, sessions or business data are copied.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from types import SimpleNamespace


MAX_INPUT_BYTES = 64 * 1024
MAX_ROUTES = 8
MAX_COUNTER = 2**63 - 1
ADMIN_FIELDS = {
    "email", "password", "display_name", "token_limit", "image_limit",
    "max_concurrent_jobs",
}
ROUTE_FIELDS = {
    "id", "name", "provider", "kind", "model", "enabled", "api_key",
    "api_key_env", "allow_unauthenticated", "base_url", "capabilities",
    "strict_quota", "image_price_microusd",
    "input_price_microusd_per_million", "output_price_microusd_per_million",
}


class InvalidSeed(Exception):
    pass


class ExistingData(Exception):
    pass


class UnsafeEnvironment(Exception):
    pass


def _object(pairs):
    """Reject duplicate keys rather than accepting a hidden last-write value."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidSeed
        result[key] = value
    return result


def _invalid_constant(_value):
    raise InvalidSeed


def read_document():
    if sys.stdin.isatty():
        raise InvalidSeed
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if not raw or len(raw) > MAX_INPUT_BYTES:
        raise InvalidSeed
    return json.loads(
        raw.decode("utf-8"), object_pairs_hook=_object,
        parse_constant=_invalid_constant,
    )


def validate_document(document):
    from app.cloud.identity import normalize_email, validate_password
    from app.cloud.routes import RouteCreate

    if (not isinstance(document, dict)
            or set(document) != {"schema_version", "admin", "routes"}
            or type(document["schema_version"]) is not int
            or document["schema_version"] != 1):
        raise InvalidSeed

    source = document["admin"]
    if (not isinstance(source, dict) or set(source) - ADMIN_FIELDS
            or not {"email", "password"}.issubset(source)
            or not isinstance(source["email"], str)
            or not isinstance(source["password"], str)):
        raise InvalidSeed
    admin = {
        "email": normalize_email(source["email"]),
        "password": validate_password(source["password"]),
        "display_name": source.get("display_name", "管理员"),
        "role": "admin",
        "token_limit": source.get("token_limit", 1_000_000),
        "image_limit": source.get("image_limit", 100),
        "max_concurrent_jobs": source.get("max_concurrent_jobs", 2),
    }
    if (not isinstance(admin["display_name"], str)
            or len(admin["display_name"].strip()) > 100):
        raise InvalidSeed
    for field in ("token_limit", "image_limit"):
        if type(admin[field]) is not int or not 0 <= admin[field] <= MAX_COUNTER:
            raise InvalidSeed
    if (type(admin["max_concurrent_jobs"]) is not int
            or not 1 <= admin["max_concurrent_jobs"] <= 100):
        raise InvalidSeed

    sources = document["routes"]
    if not isinstance(sources, list) or not 1 <= len(sources) <= MAX_ROUTES:
        raise InvalidSeed
    routes, route_ids = [], set()
    for source in sources:
        if (not isinstance(source, dict) or set(source) - ROUTE_FIELDS
                or source.get("provider") != "deepseek"
                or source.get("kind", "chat") != "chat"
                or source.get("enabled", True) is not True
                or source.get("api_key_env", "") != ""
                or source.get("allow_unauthenticated", False) is not False
                or source.get("strict_quota", False) is not False):
            raise InvalidSeed
        key = source.get("api_key")
        if (not isinstance(key, str) or not 1 <= len(key) <= 8192
                or key.startswith("suxiaoyou-credential://")
                or any(char.isspace() or ord(char) < 32 for char in key)):
            raise InvalidSeed
        body = RouteCreate.model_validate(source)
        if body.id in route_ids:
            raise InvalidSeed
        route_ids.add(body.id)
        routes.append(body)
    return admin, routes


async def seed(admin_values, routes):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.cloud.config import CloudSettings
    from app.cloud.database import Database
    from app.cloud.identity import create_user, lock_identity
    from app.cloud.models import ModelRoute, User
    from app.cloud.routes import save_model
    from app.cloud.secrets import ModelSecretVault

    settings = CloudSettings()
    if (settings.environment != "production" or settings.auto_migrate
            or settings.allow_mock_provider
            or not settings.database_url.startswith("postgresql+asyncpg://")):
        raise UnsafeEnvironment
    database = Database(settings)
    try:
        # Migration is a separate deployment step. Never create or migrate a
        # database implicitly while handling credentials.
        await database.initialize()
        async with database.engine.begin() as connection:
            # Existing administration functions commit their own work. Joining
            # an outer transaction with savepoints keeps the complete seed
            # atomic and retains the identity mutex until the final commit.
            async with AsyncSession(
                bind=connection, expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            ) as db:
                await lock_identity(db)
                email_exists = await db.scalar(select(User.id).where(
                    User.email == admin_values["email"]).limit(1))
                route_exists = await db.scalar(select(ModelRoute.id).where(
                    ModelRoute.id.in_([body.id for body in routes])).limit(1))
                any_user = await db.scalar(select(User.id).limit(1))
                any_route = await db.scalar(select(ModelRoute.id).limit(1))
                if (email_exists is not None or route_exists is not None
                        or any_user is not None or any_route is not None):
                    raise ExistingData

                # Check private master-key storage before creating anything.
                # The remote vault is independent from the source installation.
                vault = ModelSecretVault(settings)
                for body in routes:
                    vault.seal(body.api_key.get_secret_value())

                admin = await create_user(db, **admin_values)
                await db.commit()
                request = SimpleNamespace(app=SimpleNamespace(
                    state=SimpleNamespace(settings=settings)))
                for body in routes:
                    await save_model(body, request, admin, db, exists=False)
        return len(routes)
    finally:
        await database.close()


def main():
    # Dependency loggers must never write validation input or SQL parameters.
    logging.disable(logging.CRITICAL)
    try:
        admin, routes = validate_document(read_document())
        count = asyncio.run(seed(admin, routes))
    except ExistingData:
        sys.stderr.write("初始化已拒绝：目标数据库已有账号或模型，未重置或覆盖。\n")
        return 2
    except UnsafeEnvironment:
        sys.stderr.write("初始化已拒绝：需要已迁移的正式 PostgreSQL 环境并禁用模拟供应商。\n")
        return 2
    except BaseException:
        # Never print exception text, repr, Pydantic input, payload or traceback.
        sys.stderr.write("初始化失败：请检查输入格式、数据库迁移及私有密钥存储；未输出凭据。\n")
        return 1
    sys.stdout.write(json.dumps({
        "status": "created", "admin_count": 1, "route_count": count,
    }, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
