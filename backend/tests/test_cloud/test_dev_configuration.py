"""Offline local provisioning checks. No real credentials or provider calls."""
from argparse import Namespace
import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app.cloud.config import CloudSettings
from app.cloud.database import Database
from app.cloud.models import AuditEvent, ModelRoute


@pytest.fixture
def local_dev(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[3] / "scripts" / "cloud-dev.py"
    spec = importlib.util.spec_from_file_location("isolated_cloud_dev", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "CONFIG", tmp_path / ".cloud-dev.json")
    monkeypatch.setattr(module, "MODEL_SECRETS", tmp_path / ".env.cloud.local")
    monkeypatch.setattr(module, "BACKEND", tmp_path / "backend")
    return module


def init_args(**changes):
    return Namespace(**{"database_url": None, "api_port": None, "web_port": None,
                        "allow_mock": False, **changes})


def model_args(**changes):
    return Namespace(**{"preset": "deepseek", "provider": None, "id": None, "name": None,
                        "model": None, "api_key_env": None, "base_url": None,
                        "input_price": 0, "output_price": 0, "key_file": None,
                        "key_source_env": None, "no_prompt": True, **changes})


def database_for(module):
    data = module.configuration()
    return Database(CloudSettings(environment="test", database_url=data["database_url"]))


@pytest.mark.asyncio
async def test_default_init_and_legacy_environment_fail_closed(local_dev, monkeypatch):
    await local_dev.initialize(init_args())
    data = local_dev.configuration()
    assert data["allow_mock_provider"] is False
    monkeypatch.setenv("SUXIAOYOU_CLOUD_ALLOW_MOCK_PROVIDER", "true")
    legacy = {name: value for name, value in data.items() if name != "allow_mock_provider"}
    assert local_dev.environment(legacy)["SUXIAOYOU_CLOUD_ALLOW_MOCK_PROVIDER"] == "false"
    database = database_for(local_dev)
    try:
        await database.initialize()
        async with database.sessions() as session:
            assert list(await session.scalars(select(ModelRoute))) == []
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_reinitialization_preserves_accounts_ports_and_disables_audited_mock(local_dev):
    await local_dev.initialize(init_args(allow_mock=True, api_port=8111, web_port=3111))
    data = local_dev.configuration()
    data["test_user"] = {"email": "kept@example.com", "password": "kept-local-password"}
    local_dev.write_configuration(data)
    await local_dev.initialize(init_args())
    updated = local_dev.configuration()
    assert updated["test_user"] == data["test_user"]
    assert updated["password"] == data["password"]
    assert (updated["api_port"], updated["web_port"]) == (8111, 3111)
    assert updated["allow_mock_provider"] is False
    database = database_for(local_dev)
    try:
        await database.initialize()
        async with database.sessions() as session:
            assert (await session.get(ModelRoute, "mock-office")).enabled is False
            assert "dev.mock.disable" in list(await session.scalars(select(AuditEvent.action)))
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_real_route_key_stays_private_frontend_has_no_key_and_audit_has_no_secret(local_dev, monkeypatch, capsys):
    await local_dev.initialize(init_args(allow_mock=True))
    fake_key = "offline-fake-key-never-sent-to-provider"
    monkeypatch.setenv("PRIVATE_TEST_INPUT", fake_key)
    await local_dev.configure_model(model_args(key_source_env="PRIVATE_TEST_INPUT"))
    data = local_dev.configuration()
    assert fake_key not in local_dev.CONFIG.read_text()
    assert data["model_route"]["api_key_env"] == "SUXIAOYOU_MODEL_DEEPSEEK_KEY"
    assert data["allow_mock_provider"] is False
    assert local_dev.MODEL_SECRETS.stat().st_mode & 0o777 == 0o600
    assert local_dev.model_secrets() == {"SUXIAOYOU_MODEL_DEEPSEEK_KEY": fake_key}
    assert "SUXIAOYOU_MODEL_DEEPSEEK_KEY" not in local_dev.environment(data)
    monkeypatch.setenv("SUXIAOYOU_CLOUD_MODEL_MASTER_KEY", "offline-inherited-master")
    assert "SUXIAOYOU_CLOUD_MODEL_MASTER_KEY" not in local_dev.environment(data)
    assert local_dev.environment(data, include_model_secrets=True)["SUXIAOYOU_MODEL_DEEPSEEK_KEY"] == fake_key
    assert fake_key not in capsys.readouterr().out
    database = database_for(local_dev)
    try:
        await database.initialize()
        async with database.sessions() as session:
            route = await session.get(ModelRoute, "deepseek-office")
            assert route.enabled and not route.strict_quota
            assert route.api_key_encrypted is None and route.credential_version == 1
            assert (route.provider, route.model, route.base_url) == ("deepseek", "deepseek-flash", "https://api.deepseek.com/v1")
            assert not (await session.get(ModelRoute, "mock-office")).enabled
            events = list(await session.scalars(select(AuditEvent)))
            assert any(event.action == "model.create" for event in events)
            assert fake_key not in json.dumps([event.details for event in events])
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_cli_explicitly_replaces_previous_managed_key_source(local_dev, monkeypatch):
    await local_dev.initialize(init_args())
    monkeypatch.setenv("PRIVATE_TEST_INPUT", "offline-cli-replacement-key")
    database = database_for(local_dev)
    try:
        await database.initialize()
        async with database.sessions() as session:
            session.add(ModelRoute(id="deepseek-office", name="Managed route", provider="deepseek",
                                   model="deepseek-flash", api_key_encrypted="previous-encrypted-value",
                                   credential_version=7))
            await session.commit()
    finally:
        await database.close()
    await local_dev.configure_model(model_args(key_source_env="PRIVATE_TEST_INPUT"))
    database = database_for(local_dev)
    try:
        async with database.sessions() as session:
            route = await session.get(ModelRoute, "deepseek-office")
            assert route.api_key_encrypted is None
            assert route.credential_version == 8
            assert route.api_key_env == "SUXIAOYOU_MODEL_DEEPSEEK_KEY"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_private_key_file_and_hidden_prompt_are_supported_without_plaintext_arguments(local_dev, tmp_path, monkeypatch):
    await local_dev.initialize(init_args())
    key_file = tmp_path / "private-key"
    local_dev.write_private(key_file, "offline-file-key\n")
    await local_dev.configure_model(model_args(key_file=key_file, model="explicit-model"))
    assert local_dev.model_secrets()["SUXIAOYOU_MODEL_DEEPSEEK_KEY"] == "offline-file-key"
    local_dev.MODEL_SECRETS.unlink()
    monkeypatch.setattr(local_dev.getpass, "getpass", lambda prompt: "offline-hidden-key")
    monkeypatch.delenv("SUXIAOYOU_MODEL_DEEPSEEK_KEY", raising=False)
    await local_dev.configure_model(model_args(no_prompt=False))
    assert local_dev.model_secrets()["SUXIAOYOU_MODEL_DEEPSEEK_KEY"] == "offline-hidden-key"


def test_private_model_file_rejects_non_model_variables_public_files_and_symlinks(local_dev, tmp_path):
    local_dev.write_private(local_dev.MODEL_SECRETS, 'SUXIAOYOU_MODEL_DEEPSEEK_KEY=\n')
    assert local_dev.model_secrets() == {}
    local_dev.write_private(local_dev.MODEL_SECRETS, 'NEXT_PUBLIC_API_KEY="offline-key"\n')
    with pytest.raises(ValueError, match="invalid or duplicate"):
        local_dev.model_secrets()
    local_dev.write_private(local_dev.MODEL_SECRETS, 'SUXIAOYOU_MODEL_TEST="offline-key"\n')
    local_dev.MODEL_SECRETS.chmod(0o644)
    with pytest.raises(ValueError, match="chmod 600"):
        local_dev.model_secrets()
    local_dev.MODEL_SECRETS.unlink()
    key_file = tmp_path / "target-key"
    local_dev.write_private(key_file, 'SUXIAOYOU_MODEL_TEST="offline-key"\n')
    local_dev.MODEL_SECRETS.symlink_to(key_file)
    with pytest.raises((OSError, ValueError)):
        local_dev.model_secrets()


@pytest.mark.asyncio
async def test_missing_key_never_enables_a_real_route(local_dev, monkeypatch):
    await local_dev.initialize(init_args())
    monkeypatch.delenv("SUXIAOYOU_MODEL_DEEPSEEK_KEY", raising=False)
    with pytest.raises(ValueError, match="non-empty"):
        await local_dev.configure_model(model_args())
    assert not local_dev.MODEL_SECRETS.exists()
    assert "model_route" not in local_dev.configuration()
