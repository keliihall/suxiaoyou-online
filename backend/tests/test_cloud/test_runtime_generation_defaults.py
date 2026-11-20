"""Reasoning needs an output budget; explicit operator ceilings still win."""
import pytest
from app.cloud.runtime_entry import _generation_defaults
from app.config import Settings, get_settings
from app.session.utils import compute_safe_max_tokens


@pytest.fixture(autouse=True)
def clear_process_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def actual_output_budget():
    return compute_safe_max_tokens([{"role": "user", "content": "next"}],
        model_max_context=1_000_000, model_max_output=131072)


def test_only_cloud_bootstrap_changes_desktop_default(monkeypatch):
    monkeypatch.delenv("SUXIAOYOU_HARD_MAX_OUTPUT_TOKENS", raising=False)
    desktop = Settings(_env_file=None)
    assert desktop.hard_max_output_tokens == 8192
    assert _generation_defaults(Settings(_env_file=None)).hard_max_output_tokens == 65536
    assert actual_output_budget() == 65536
    assert desktop.hard_max_output_tokens == 8192


def test_operator_environment_ceiling_is_preserved(monkeypatch):
    monkeypatch.setenv("SUXIAOYOU_HARD_MAX_OUTPUT_TOKENS", "4096")
    assert _generation_defaults(Settings(_env_file=None)).hard_max_output_tokens == 4096
    assert actual_output_budget() == 4096


def test_tenant_dotenv_and_explicit_ceiling_are_preserved(tmp_path, monkeypatch):
    monkeypatch.delenv("SUXIAOYOU_HARD_MAX_OUTPUT_TOKENS", raising=False)
    dotenv = tmp_path / ".env"
    dotenv.write_text("SUXIAOYOU_HARD_MAX_OUTPUT_TOKENS=16384\n")
    assert _generation_defaults(Settings(_env_file=dotenv)).hard_max_output_tokens == 16384
    assert actual_output_budget() == 16384
    assert _generation_defaults(Settings(_env_file=None, hard_max_output_tokens=2048)).hard_max_output_tokens == 2048
    assert actual_output_budget() == 2048
