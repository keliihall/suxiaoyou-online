"""Helpers for browser OAuth callbacks served by the local backend."""

from __future__ import annotations

from app.config import Settings
from urllib.parse import urlsplit


def loopback_redirect_uri(settings: Settings, path: str) -> str:
    """Return a stable localhost callback using the backend's actual port."""
    normalized_path = path if path.startswith("/") else f"/{path}"
    origin = settings.oauth_callback_origin.rstrip("/")
    if origin:
        parsed = urlsplit(origin)
        if (parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path
                or parsed.query or parsed.fragment or parsed.username or parsed.password):
            raise ValueError("OAuth callback origin must be one trusted HTTP(S) origin")
        return origin + normalized_path
    return f"http://localhost:{settings.port}{normalized_path}"
