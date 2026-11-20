from types import SimpleNamespace

import pytest

from app.cloud.gateway_errors import admission_chunk
from app.session.retry import is_retryable


@pytest.mark.parametrize("code", ["quota_exceeded", "concurrency_exceeded", "account_disabled"])
def test_platform_rejection_has_safe_code_and_never_retries(code):
    data = admission_chunk(SimpleNamespace(body={"error": {"code": code, "message": "PRIVATE_BODY_MUST_NOT_ESCAPE"}}))
    assert data["code"] == code and data["retryable"] is False
    assert "PRIVATE_BODY" not in data["message"]
    error = RuntimeError("429 Rate limit exceeded")
    error.retryable = data["retryable"]
    assert is_retryable(error) is None


def test_vendor_rate_limits_keep_existing_retry_policy():
    assert admission_chunk(SimpleNamespace(body={"error": {"code": "vendor-rate-limit"}})) is None
    assert is_retryable(RuntimeError("429 Rate limit exceeded")) == "Rate limited"
