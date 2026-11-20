"""A trusted transport rejection remains actionable across the Agent boundary."""

import pytest

from app.session.processor import (
    _ProviderReportedStreamError,
    _localized_provider_stream_error,
)
from app.session.retry import is_context_overflow, is_retryable


@pytest.mark.parametrize("language", ["zh", "en"])
def test_payload_rejection_preserves_safe_code_without_retry_or_compaction(language):
    error = _ProviderReportedStreamError(
        "private diagnostic must never appear",
        code="model_request_too_large",
        retryable=False,
    )
    code, message = _localized_provider_stream_error(error, language)
    assert code == "model_request_too_large"
    assert "private diagnostic" not in message
    assert is_retryable(error) is None
    assert not is_context_overflow(error)
    assert ("图片" in message) if language == "zh" else ("images" in message)
