"""Managed credential selection and vendor-error sanitization, without network."""

import traceback
from types import SimpleNamespace

from cryptography.fernet import Fernet
import httpx
from pydantic import SecretStr
import pytest

from app.cloud.config import CloudSettings
from app.cloud.providers import ProviderConfigurationError, ProviderRequestError, build_provider
from app.cloud.secrets import ModelSecretVault
from tests.test_cloud.test_metering_worker import worker_env, state
from app.cloud import worker as worker_module

SYNTHETIC_KEY = "synthetic-managed-key-no-live-credential"


def settings_for(tmp_path):
    return CloudSettings(environment="test", database_url="sqlite+aiosqlite:///:memory:",
                         data_dir=tmp_path / "data", cookie_secure=False,
                         model_master_key=SecretStr(Fernet.generate_key().decode()))


def model_route(ciphertext, *, provider="deepseek", api_key_env=""):
    return SimpleNamespace(provider=provider, enabled=True, strict_quota=False,
                           api_key_env=api_key_env, api_key_encrypted=ciphertext, base_url=None)


def clear_proxy(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)


async def test_managed_key_unseals_with_empty_env_and_takes_priority_when_env_exists(tmp_path, monkeypatch):
    clear_proxy(monkeypatch)
    settings = settings_for(tmp_path)
    ciphertext = ModelSecretVault(settings).seal(SYNTHETIC_KEY)
    monkeypatch.setenv("CLOUD_TEST_FALLBACK_KEY", "synthetic-other-account-key")
    for env_name in ("", "CLOUD_TEST_FALLBACK_KEY"):
        provider = build_provider(model_route(ciphertext, api_key_env=env_name), settings)
        try:
            # The cloud safety proxy remains transparent to SDK configuration.
            assert provider.delegate._client.api_key == SYNTHETIC_KEY
            assert provider.delegate._client.max_retries == 0
        finally:
            await provider.aclose()


@pytest.mark.parametrize("damaged", ["", "damaged-ciphertext", None])
def test_unreadable_managed_key_never_falls_back_to_another_env_key(tmp_path, monkeypatch, damaged):
    settings = settings_for(tmp_path)
    ciphertext = damaged
    if damaged is None:
        # Valid encrypted data under the wrong master key is equally fatal.
        other_settings = settings_for(tmp_path / "other")
        ciphertext = ModelSecretVault(other_settings).seal(SYNTHETIC_KEY)
    monkeypatch.setenv("CLOUD_TEST_FALLBACK_KEY", "synthetic-other-account-key")
    def forbidden_factory(*_args, **_kwargs):
        pytest.fail("Unsealing failure must not call the model factory or fallback account")
    monkeypatch.setattr("app.provider.factory.create_provider", forbidden_factory)
    with pytest.raises(ProviderConfigurationError) as error:
        build_provider(model_route(ciphertext, api_key_env="CLOUD_TEST_FALLBACK_KEY"), settings)
    displayed = "".join(traceback.format_exception(error.value))
    assert SYNTHETIC_KEY not in displayed and "synthetic-other-account-key" not in displayed
    assert ciphertext not in displayed if ciphertext else True
    assert error.value.__suppress_context__


def test_sdk_constructor_error_cannot_echo_managed_plaintext_or_ciphertext(tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    ciphertext = ModelSecretVault(settings).seal(SYNTHETIC_KEY)
    def invalid_sdk(*_args, **_kwargs):
        raise ValueError("Invalid Authorization: " + SYNTHETIC_KEY)
    monkeypatch.setattr("app.provider.factory.create_provider", invalid_sdk)
    with pytest.raises(ProviderConfigurationError) as error:
        build_provider(model_route(ciphertext), settings)
    displayed = "".join(traceback.format_exception(error.value))
    assert SYNTHETIC_KEY not in displayed and ciphertext not in displayed


async def http_error_provider(tmp_path, monkeypatch, *, provider_name="deepseek", status=401):
    clear_proxy(monkeypatch)
    settings = settings_for(tmp_path)
    ciphertext = ModelSecretVault(settings).seal(SYNTHETIC_KEY)
    provider = build_provider(model_route(ciphertext, provider=provider_name), settings)
    # Anthropic 2.x uses httpx2; the compatible SDK still uses httpx 0.28.
    if provider_name == "anthropic":
        import httpx2 as transport_httpx
    else:
        transport_httpx = httpx
    calls = []
    def reject(request):
        calls.append(1)
        return transport_httpx.Response(status, json={"error": {"type": "authentication_error", "message":
                              "Vendor accidentally echoes " + SYNTHETIC_KEY}})
    client = transport_httpx.AsyncClient(transport=transport_httpx.MockTransport(reject))
    original = provider.delegate._client
    provider.delegate._client = original.with_options(http_client=client)
    await original.close()
    return provider, ciphertext, calls


@pytest.mark.parametrize("status", [401, 402, 429])
async def test_deepseek_http_error_retains_safe_status_without_key_echo(tmp_path, monkeypatch, caplog, status):
    provider, ciphertext, calls = await http_error_provider(tmp_path, monkeypatch, status=status)
    try:
        with pytest.raises(ProviderRequestError) as error:
            _ = [chunk async for chunk in provider.stream_chat("deepseek-flash", [{"role": "user", "content": "test"}])]
        assert str(status) in str(error.value) and error.value.status_code == status
        displayed = "".join(traceback.format_exception(error.value)) + caplog.text
        assert SYNTHETIC_KEY not in displayed and ciphertext not in displayed
        assert calls == [1]  # SDK retries remain disabled even for 429.
    finally:
        await provider.aclose()


@pytest.mark.parametrize("provider_name", ["qwen", "anthropic"])
async def test_legacy_adapter_logs_only_safe_sdk_summary(tmp_path, monkeypatch, caplog, provider_name):
    provider, ciphertext, calls = await http_error_provider(tmp_path, monkeypatch, provider_name=provider_name)
    try:
        chunks = [chunk async for chunk in provider.stream_chat("test-model", [{"role": "user", "content": "test"}])]
        assert chunks[-1].type == "error" and chunks[-1].data["status_code"] == 401
        assert "HTTP 401" in chunks[-1].data["message"]
        assert SYNTHETIC_KEY not in caplog.text and ciphertext not in caplog.text
        assert calls == [1]
    finally:
        await provider.aclose()


async def test_worker_keeps_safe_auth_failure_without_vendor_key_in_job_or_logs(worker_env, tmp_path, monkeypatch, caplog):
    database, _, worker = worker_env
    provider, ciphertext, calls = await http_error_provider(tmp_path, monkeypatch)
    monkeypatch.setattr(worker_module, "build_provider", lambda *_args: provider)
    assert await worker.run_once()
    job, requests, events, _ = await state(database)
    assert job.state == "needs_review" and requests[0].state == "unknown" and events == []
    assert "HTTP 401" in job.error
    assert SYNTHETIC_KEY not in job.error + caplog.text and ciphertext not in job.error + caplog.text
    assert calls == [1]
