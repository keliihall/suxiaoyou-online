"""Write-only model credentials through the administrator API."""
import json

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from app.cloud.config import CloudSettings
from app.cloud.identity import create_user
from app.cloud.main import create_app
from app.cloud.models import AuditEvent, ModelRoute
from app.cloud.providers import build_provider
from captcha_helpers import captcha_proof

P = "/api/cloud"
KEY = "sk-isolated-managed-credential-test-only"


@pytest_asyncio.fixture
async def managed(tmp_path):
    settings = CloudSettings(environment="test", database_url=f"sqlite+aiosqlite:///{tmp_path}/db.sqlite",
                             data_dir=tmp_path / "private", auto_migrate=True, cookie_secure=False)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with app.state.database.sessions() as db:
            for email, role in (("admin@example.com", "admin"), ("user@example.com", "user")):
                await create_user(db, email=email, password="isolated-password-2026", role=role)
            await db.commit()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:3000") as admin, \
                   httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:3000") as user:
            for client, email in ((admin, "admin@example.com"), (user, "user@example.com")):
                proof = await captcha_proof(client, app.state.database)
                response = await client.post(P + "/auth/login", json={"email": email, "password": "isolated-password-2026", "captcha_proof": proof})
                client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
            yield app, settings, admin, user


def payload(**values):
    return {"id": "deepseek-office", "name": "DeepSeek", "provider": "deepseek", "model": "deepseek-flash",
            "api_key_env": "", "base_url": "https://api.deepseek.com/v1", "api_key": KEY, **values}


@pytest.mark.asyncio
async def test_managed_credentials_are_encrypted_write_only_and_work_without_restart(managed):
    app, settings, admin, user = managed
    response = await admin.post(P + "/admin/models", json=payload())
    assert response.status_code == 201, response.text
    assert response.json()["key_configured"] and response.json()["credential_source"] == "managed"
    assert KEY not in response.text and "api_key_encrypted" not in response.text
    async with app.state.database.sessions() as db:
        route = await db.get(ModelRoute, "deepseek-office")
        encrypted = route.api_key_encrypted
        assert encrypted and KEY not in encrypted and route.credential_version == 1
        provider = build_provider(route, settings)
        assert provider.delegate._client.api_key == KEY
        await provider.aclose()
    for client, path in ((admin, "/admin/models"), (user, "/models"), (admin, "/admin/audit")):
        response = await client.get(P + path)
        assert response.status_code == 200
        assert KEY not in response.text and encrypted not in response.text
    public = (await user.get(P + "/models")).json()[0]
    assert "api_key_env" not in public and "credential_source" not in public


@pytest.mark.asyncio
async def test_rotation_blank_preservation_and_explicit_removal_are_audited(managed):
    app, settings, admin, _ = managed
    assert (await admin.post(P + "/admin/models", json=payload())).status_code == 201
    async with app.state.database.sessions() as db:
        initial = await db.get(ModelRoute, "deepseek-office")
        old_cipher = initial.api_key_encrypted
    assert (await admin.put(P + "/admin/models/deepseek-office", json=payload(api_key=""))).status_code == 200
    async with app.state.database.sessions() as db:
        route = await db.get(ModelRoute, "deepseek-office")
        assert route.api_key_encrypted == old_cipher and route.credential_version == 1
    replacement = KEY + "-rotated"
    assert (await admin.put(P + "/admin/models/deepseek-office", json=payload(api_key=replacement))).status_code == 200
    async with app.state.database.sessions() as db:
        route = await db.get(ModelRoute, "deepseek-office")
        assert route.credential_version == 2 and route.api_key_encrypted != old_cipher
        from app.cloud.secrets import ModelSecretVault
        assert ModelSecretVault(settings).unseal(route.api_key_encrypted) == replacement
    # The only usable credential cannot be removed while the route is enabled.
    clear = payload(api_key="", clear_api_key=True)
    assert (await admin.put(P + "/admin/models/deepseek-office", json=clear)).status_code == 400
    clear["enabled"] = False
    response = await admin.put(P + "/admin/models/deepseek-office", json=clear)
    assert response.status_code == 200 and not response.json()["key_configured"]
    async with app.state.database.sessions() as db:
        route = await db.get(ModelRoute, "deepseek-office")
        assert route.api_key_encrypted is None and route.credential_version == 3
        events = (await db.scalars(select(AuditEvent))).all()
        details = json.dumps([event.details for event in events])
        assert KEY not in details and old_cipher not in details
        assert [e.action for e in events].count("model.credential_replaced") == 2
        assert any(e.action == "model.credential_cleared" for e in events)


@pytest.mark.asyncio
async def test_credential_authorization_and_validation_never_echo_submitted_keys(managed):
    app, settings, admin, user = managed
    assert (await user.post(P + "/admin/models", json=payload())).status_code == 403
    assert not (settings.data_dir / "secrets" / "model-master.key").exists()
    for bad in (payload(api_key=KEY + " white space"), payload(api_key=KEY, clear_api_key=True), payload(api_key=[KEY])):
        response = await admin.post(P + "/admin/models", json=bad)
        assert response.status_code in {400, 422}
        assert KEY not in response.text
    async with app.state.database.sessions() as db:
        assert await db.get(ModelRoute, "deepseek-office") is None


@pytest.mark.asyncio
async def test_unconfigured_model_can_be_saved_as_disabled_draft(managed):
    _, _, admin, _ = managed
    response = await admin.post(P + "/admin/models", json=payload(api_key=None, enabled=False))
    assert response.status_code == 201 and response.json()["credential_source"] == "none"
    assert not response.json()["key_configured"]
