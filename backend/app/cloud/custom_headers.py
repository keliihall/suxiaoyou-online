"""Write-only, bounded custom endpoint credentials; no values in public DTOs."""
from __future__ import annotations

import json
import re

from pydantic import SecretStr

_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
_RESERVED = {"host", "content-length", "connection", "transfer-encoding", "proxy-authorization",
             "proxy-connection", "te", "trailer", "upgrade", "forwarded"}


def supports_custom_headers(provider: str, kind: str = "chat") -> bool:
    return kind == "chat" and (provider == "openai" or provider.startswith("custom_"))


def validate_headers(value) -> dict[str, str]:
    """Fail with a fixed message, never an input name/value or credential ref."""
    if not isinstance(value, dict) or len(value) > 32:
        raise ValueError("自定义请求头必须为最多32项的对象")
    result, seen = {}, set()
    for name, secret in value.items():
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError("自定义请求头名称格式不正确")
        lowered = name.lower()
        if lowered in seen or lowered in _RESERVED or lowered.startswith("x-forwarded-"):
            raise ValueError("自定义请求头含重复名称或禁止覆盖的传输头")
        seen.add(lowered)
        text = secret.get_secret_value() if isinstance(secret, SecretStr) else secret
        if (not isinstance(text, str) or not text or len(text) > 4096 or text != text.strip()
                or any(ord(char) < 32 or ord(char) > 126 for char in text)
                or text.startswith("suxiaoyou-credential://")):
            raise ValueError("自定义请求头值必须为无首尾空白的可打印ASCII文本")
        # Canonical Authorization avoids duplicate differently-cased SDK auth
        # fields. All values remain literal, never desktop vault references.
        result["Authorization" if lowered == "authorization" else lowered] = text
    if len(encode_headers(result)) > 8192:
        raise ValueError("自定义请求头总大小超过限制")
    return result


def encode_headers(value: dict[str, str]) -> str:
    return json.dumps({"purpose": "cloud-custom-headers-v1", "headers": value},
                      ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def decode_headers(value: str) -> dict[str, str]:
    document = json.loads(value)
    if (not isinstance(document, dict) or set(document) != {"purpose", "headers"}
            or document["purpose"] != "cloud-custom-headers-v1"):
        raise ValueError("Stored custom headers are invalid")
    headers = validate_headers(document["headers"])
    if not headers:
        raise ValueError("Stored custom headers are empty")
    return headers
