"""Browser callback URLs must never contain a tenant's private runtime port."""
import pytest

from app.api.oauth_redirect import loopback_redirect_uri
from app.config import Settings


def test_desktop_keeps_actual_loopback_port():
    settings = Settings(_env_file=None, port=17321)
    assert loopback_redirect_uri(settings, "/api/google/callback") == "http://localhost:17321/api/google/callback"


def test_cloud_uses_exact_trusted_web_origin():
    settings = Settings(_env_file=None, port=8080, oauth_callback_origin="https://office.example.com/")
    assert loopback_redirect_uri(settings, "/api/connectors/oauth/callback") == "https://office.example.com/api/connectors/oauth/callback"


@pytest.mark.parametrize("origin", ["https://user:pass@example.com", "https://example.com/path", "//example.com", "https://example.com?q=x", "https://example.com#x"])
def test_invalid_callback_origin_fails_closed(origin):
    with pytest.raises(ValueError):
        loopback_redirect_uri(Settings(_env_file=None, oauth_callback_origin=origin), "/api/google/callback")
