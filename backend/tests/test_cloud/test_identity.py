"""Identity/security integration tests, using only the independent cloud app."""

import asyncio
import hashlib
import os
from datetime import timedelta

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import make_url

from app.cloud.config import CloudSettings
from app.cloud.database import Database, migrate
from app.cloud.identity import COOKIE_NAME, create_user, hash_password, router, verify_password
from app.cloud.models import AuditEvent, AuthSession, CloudBase, Conversation, QuotaBucket, User, Workspace, new_id, utcnow
from captcha_helpers import captcha_proof

PASSWORD = "test-password-123"
PREFIX = "/api/cloud"


@pytest_asyncio.fixture(params=["sqlite", "postgresql"])
async def identity_env(tmp_path, request):
    url = f"sqlite+aiosqlite:///{tmp_path}/cloud.db"
    administration = None
    test_database = None
    if request.param == "postgresql":
        postgres_url = os.getenv("CLOUD_TEST_POSTGRES_URL")
        if not postgres_url:
            pytest.skip("CLOUD_TEST_POSTGRES_URL is not configured")
        import asyncpg
        parsed = make_url(postgres_url)
        administration = await asyncpg.connect(parsed.set(drivername="postgresql").render_as_string(hide_password=False))
        test_database = "cloud_identity_test_" + new_id()
        await administration.execute(f'CREATE DATABASE "{test_database}"')
        url = parsed.set(drivername="postgresql+asyncpg", database=test_database).render_as_string(hide_password=False)
    settings = CloudSettings(environment="test", database_url=url, auto_migrate=True, cookie_secure=False)
    database = Database(settings)
    await database.initialize()
    async with database.sessions() as db:
        admin = await create_user(db, email="admin@example.com", password=PASSWORD, role="admin")
        user = await create_user(db, email="user@example.com", password=PASSWORD)
        await db.commit()
        ids = {"admin": admin.id, "user": user.id}
    app = FastAPI()
    app.state.database = database
    app.state.settings = settings
    app.include_router(router, prefix=PREFIX)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:3000") as client:
            yield client, database, settings, ids, app
    finally:
        await database.close()
        if administration:
            await administration.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1 AND pid<>pg_backend_pid()", test_database)
            await administration.execute(f'DROP DATABASE "{test_database}"')
            await administration.close()


async def login(client, email="admin@example.com", password=PASSWORD):
    proof = await captcha_proof(client, client._transport.app.state.database)
    response = await client.post(PREFIX + "/auth/login", json={"email": email, "password": password, "captcha_proof": proof})
    assert response.status_code == 200, response.text
    return {"X-CSRF-Token": response.json()["csrf_token"]}


def test_scrypt_password_validation_and_salt():
    first, second = hash_password(PASSWORD), hash_password(PASSWORD)
    assert first != second
    assert verify_password(PASSWORD, first)
    assert not verify_password("wrong-password", first)
    assert not verify_password(PASSWORD, "scrypt$999999999$8$1$00$00")
    assert not verify_password(PASSWORD, "malformed")
    with pytest.raises(ValueError):
        hash_password("short")


def test_production_configuration_is_fail_closed():
    with pytest.raises(ValidationError, match="development/test only"):
        CloudSettings(auto_migrate=True)
    with pytest.raises(ValidationError, match="development/test only"):
        CloudSettings(allow_mock_provider=True)
    with pytest.raises(ValidationError):
        CloudSettings(allowed_origin="https://example.com/path")
    assert CloudSettings(database_url="postgresql:///cloud").database_url == "postgresql+asyncpg:///cloud"


@pytest.mark.asyncio
async def test_migration_required_repeatable_and_desktop_independent(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path}/cloud.db"
    database = Database(CloudSettings(database_url=url))
    with pytest.raises(RuntimeError, match="not migrated"):
        await database.initialize()
    await migrate(url)
    await migrate(url)
    await database.initialize()
    async with database.engine.connect() as connection:
        tables = await connection.run_sync(lambda c: set(inspect(c).get_table_names()))
        assert tables == set(CloudBase.metadata.tables) | {"cloud_alembic_version"}
        assert "session" not in tables
        from alembic.autogenerate import compare_metadata
        from alembic.migration import MigrationContext
        assert not await connection.run_sync(lambda c: compare_metadata(MigrationContext.configure(c, opts={"version_table": "cloud_alembic_version"}), CloudBase.metadata))
    async with database.engine.begin() as connection:
        await connection.execute(text("UPDATE cloud_alembic_version SET version_num='stale'"))
    with pytest.raises(RuntimeError, match="revision mismatch"):
        await database.initialize()
    await database.close()


@pytest.mark.asyncio
async def test_cookie_login_hash_expiry_and_logout_csrf(identity_env):
    client, database, settings, ids, _ = identity_env
    assert (await client.get(PREFIX + "/auth/me")).status_code == 401
    headers = await login(client, " ADMIN@EXAMPLE.COM ")
    token = client.cookies.get(COOKIE_NAME)
    response = await client.get(PREFIX + "/auth/me")
    assert response.json()["user"]["id"] == ids["admin"]
    assert "password_hash" not in response.text
    async with database.sessions() as db:
        stored = (await db.execute(select(AuthSession))).scalar_one()
        assert stored.id == hashlib.sha256(token.encode()).hexdigest()
        assert stored.id != token
    assert (await client.post(PREFIX + "/auth/logout")).status_code == 403
    assert (await client.post(PREFIX + "/auth/logout", headers={**headers, "Origin": "https://evil.example"})).status_code == 403
    assert (await client.post(PREFIX + "/auth/logout", headers=headers)).status_code == 200
    assert (await client.get(PREFIX + "/auth/me")).status_code == 401
    await login(client)
    async with database.sessions() as db:
        stored = (await db.execute(select(AuthSession))).scalar_one()
        stored.expires_at = utcnow() - timedelta(seconds=1)
        await db.commit()
    assert (await client.get(PREFIX + "/auth/me")).status_code == 401


@pytest.mark.asyncio
async def test_secure_cookie_and_cross_site_login(identity_env):
    client, database, settings, _, _ = identity_env
    denied = await client.post(PREFIX + "/auth/login", json={"email": "admin@example.com", "password": PASSWORD}, headers={"Origin": "https://evil.example"})
    assert denied.status_code == 403
    proof = await captcha_proof(client, database)
    settings.cookie_secure = True
    result = await client.post(PREFIX + "/auth/login", json={"email": "admin@example.com", "password": PASSWORD, "captcha_proof": proof})
    assert result.status_code == 200
    cookie = result.headers["set-cookie"].lower()
    assert "secure" in cookie and "httponly" in cookie and "samesite=lax" in cookie


@pytest.mark.asyncio
async def test_invitation_admin_role_and_last_admin_guard(identity_env):
    client, database, _, ids, _ = identity_env
    user_headers = await login(client, "user@example.com")
    assert (await client.get(PREFIX + "/admin/users")).status_code == 403
    assert (await client.post(PREFIX + "/admin/users", headers=user_headers, json={"email": "bad@example.com", "password": PASSWORD})).status_code == 403
    headers = await login(client)
    result = await client.post(PREFIX + "/admin/users", headers=headers, json={"email": "Invited@example.com", "display_name": "Invited", "password": PASSWORD, "token_limit": 100})
    assert result.status_code == 201, result.text
    assert result.json()["email"] == "invited@example.com"
    assert (await client.post(PREFIX + "/admin/users", headers=headers, json={"email": "INVITED@example.com", "password": PASSWORD})).status_code == 409
    for patch in ({"role": "user"}, {"is_active": False}):
        result = await client.patch(PREFIX + f"/admin/users/{ids['admin']}", headers=headers, json={**patch, "reason": "test"})
        assert result.status_code == 409, result.text
        assert result.json()["detail"]["code"] == "last_admin"
    async with database.sessions() as db:
        count = await db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "user.created"))
        assert count == 3


@pytest.mark.asyncio
async def test_disable_revokes_sessions_and_quota_change_keeps_usage(identity_env):
    client, database, _, ids, app = identity_env
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:3000") as user_client:
        await login(user_client, "user@example.com")
        headers = await login(client)
        from zoneinfo import ZoneInfo
        period = utcnow().astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m")
        async with database.sessions() as db:
            db.add(QuotaBucket(user_id=ids["user"], period=period, limit_tokens=1000, used_tokens=900, reserved_tokens=50))
            await db.commit()
        result = await client.patch(PREFIX + f"/admin/users/{ids['user']}", headers=headers, json={"token_limit": 10, "is_active": False, "reason": "policy"})
        assert result.status_code == 200, result.text
        assert (await user_client.get(PREFIX + "/auth/me")).status_code == 401
        async with database.sessions() as db:
            bucket = (await db.execute(select(QuotaBucket))).scalar_one()
            assert (bucket.limit_tokens, bucket.used_tokens, bucket.reserved_tokens) == (10, 900, 50)
            sessions = await db.scalar(select(func.count()).select_from(AuthSession).where(AuthSession.user_id == ids["user"]))
            assert sessions == 0
            audit = (await db.execute(select(AuditEvent).where(AuditEvent.action == "user.updated"))).scalar_one()
            assert audit.details["reason"] == "policy"


@pytest.mark.asyncio
async def test_password_change_revokes_every_session(identity_env):
    client, database, _, ids, app = identity_env
    headers = await login(client, "user@example.com")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:3000") as other:
        await login(other, "user@example.com")
        denied = await client.post(PREFIX + "/auth/password", headers=headers, json={"current_password": "wrong", "new_password": "new-password-456"})
        assert denied.status_code == 403
        result = await client.post(PREFIX + "/auth/password", headers=headers, json={"current_password": PASSWORD, "new_password": "new-password-456"})
        assert result.status_code == 200, result.text
        assert (await other.get(PREFIX + "/auth/me")).status_code == 401
    proof = await captcha_proof(client, database)
    assert (await client.post(PREFIX + "/auth/login", json={"email": "user@example.com", "password": PASSWORD, "captcha_proof": proof})).status_code == 401
    await login(client, "user@example.com", "new-password-456")


@pytest.mark.asyncio
async def test_login_rate_limit_is_shared_between_app_instances(identity_env):
    client, database, settings, _, app = identity_env
    settings.login_max_attempts = 2
    second_app = FastAPI()
    second_app.state.database = database
    second_app.state.settings = settings
    second_app.include_router(router, prefix=PREFIX)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=second_app), base_url="http://localhost:3000") as other:
        for peer in (client, other):
            proof = await captcha_proof(peer, database)
            result = await peer.post(PREFIX + "/auth/login", json={"email": "admin@example.com", "password": "wrong", "captcha_proof": proof})
            assert result.status_code == 401
        proof = await captcha_proof(client, database)
        result = await client.post(PREFIX + "/auth/login", json={"email": "admin@example.com", "password": PASSWORD, "captcha_proof": proof})
        assert result.status_code == 429
        assert result.headers["retry-after"]


@pytest.mark.asyncio
async def test_relational_tenant_boundary_is_database_enforced(identity_env):
    _, database, _, ids, _ = identity_env
    async with database.sessions() as db:
        workspace = Workspace(user_id=ids["admin"], name="Private")
        db.add(workspace)
        await db.commit()
        workspace_id = workspace.id
    async with database.sessions() as db:
        db.add(Conversation(user_id=ids["user"], workspace_id=workspace_id, title="Forged"))
        with pytest.raises(IntegrityError):
            await db.commit()


@pytest.mark.asyncio
async def test_two_admins_cannot_concurrently_remove_last_admin(identity_env):
    client, database, _, ids, app = identity_env
    async with database.sessions() as db:
        other_admin = await create_user(db, email="admin2@example.com", password=PASSWORD, role="admin")
        await db.commit()
        second_id = other_admin.id
    first_headers = await login(client)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:3000") as other:
        second_headers = await login(other, "admin2@example.com")
        results = await asyncio.gather(
            client.patch(PREFIX + f"/admin/users/{ids['admin']}", headers=first_headers, json={"role": "user", "reason": "concurrent"}),
            other.patch(PREFIX + f"/admin/users/{second_id}", headers=second_headers, json={"role": "user", "reason": "concurrent"}),
        )
        assert sorted(result.status_code for result in results) == [200, 409], [r.text for r in results]
    async with database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(User).where(User.role == "admin", User.is_active.is_(True))) == 1


@pytest.mark.asyncio
async def test_cli_migrate_and_create_admin_are_repeatable(tmp_path, monkeypatch):
    from argparse import Namespace
    from app.cloud.manage import run
    monkeypatch.setenv("SUXIAOYOU_CLOUD_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/cli.db")
    monkeypatch.setenv("SUXIAOYOU_CLOUD_ENVIRONMENT", "test")
    monkeypatch.setenv("CLOUD_TEST_ADMIN_PASSWORD", PASSWORD)
    assert "latest revision" in await run(Namespace(command="migrate"))
    assert "latest revision" in await run(Namespace(command="migrate"))
    args = Namespace(command="create-admin", email="Bootstrap@example.com", display_name="Bootstrap", password_env="CLOUD_TEST_ADMIN_PASSWORD")
    assert await run(args) == "Administrator created."
    monkeypatch.setenv("CLOUD_TEST_ADMIN_PASSWORD", "different-password-999")
    assert "already exists" in await run(args)
    database = Database(CloudSettings())
    try:
        async with database.sessions() as db:
            user = (await db.execute(select(User))).scalar_one()
            assert verify_password(PASSWORD, user.password_hash)
            assert user.role == "admin"
            assert await db.scalar(select(func.count()).select_from(AuditEvent)) == 1
    finally:
        await database.close()
