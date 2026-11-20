"""Offline vendor SDK -> cloud SSE -> tenant SDK error contract checks."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.cloud import inference
from app.cloud.gateway_errors import admission_chunk
from app.cloud.gateway_provider import GatewayProvider
from app.cloud.providers import (
    DeepSeekCloudProvider, ProviderRequestError, _SafeSDKNode,
    _safe_provider_exception,
)
from app.provider.generic_openai import GenericOpenAIProvider
from app.schemas.provider import safe_provider_error_data, safe_provider_error_metadata
from app.session.retry import is_context_overflow, is_retryable

RID = "17aaf027e6154d11b89b3ec439ad6cc9"
PRIVATE = "DO_NOT_EXPOSE_VENDOR_KEY_OR_BODY"


def vendor_provider(handler):
    delegate = GenericOpenAIProvider("synthetic-key", provider_id="deepseek",
                                    base_url="https://vendor.invalid/v1")
    original = delegate._client
    delegate._client = _SafeSDKNode(original.with_options(max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))))
    return DeepSeekCloudProvider(delegate, "deepseek"), original


@pytest.mark.parametrize(("body", "expected"), [
    ({"code": "context_length_exceeded", "message": PRIVATE}, "model_context_too_long"),
    ({"message": "Input is too long " + PRIVATE}, "model_context_too_long"),
    ({"message": "maximum context length exceeded " + PRIVATE}, "model_context_too_long"),
    ({"message": "Unsupported parameter max_tokens " + PRIVATE}, "model_request_rejected"),
    ({"code": [PRIVATE], "message": PRIVATE}, "model_request_rejected"),
])
async def test_actual_vendor_sdk_context_is_classified_before_sanitization(body, expected):
    sent = []
    def reject(request):
        sent.append(1)
        return httpx.Response(400, json={"error": body})
    provider, original = vendor_provider(reject)
    try:
        with pytest.raises(ProviderRequestError) as caught:
            _ = [row async for row in provider.stream_chat("deepseek-flash", [{"role": "user", "content": "test"}])]
        error = caught.value
        assert error.code == expected and error.upstream_status == 400
        assert error.retryable is False and is_retryable(error) is None
        assert is_context_overflow(error) == (expected == "model_context_too_long")
        assert PRIVATE not in str(error)
        assert sent == [1]
    finally:
        await provider.aclose()
        await original.close()


@pytest.mark.parametrize("error", [TimeoutError(PRIVATE), ConnectionError(PRIVATE), RuntimeError(PRIVATE)])
def test_uncertain_vendor_errors_remain_nonretryable(error):
    safe = _safe_provider_exception(error)
    assert safe.code in {"model_timeout", "model_connection_interrupted", "model_service_error"}
    assert is_retryable(safe) is None and not is_context_overflow(safe)
    assert PRIVATE not in str(safe)


async def test_context_category_and_platform_reference_survive_both_real_sdks(caplog):
    def reject(_request):
        return httpx.Response(400, json={"error": {"code": "context_length_exceeded", "message": PRIVATE}})
    upstream, original = vendor_provider(reject)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(database=None)))
    body = inference.ChatCompletionRequest(model="test-route", messages=[{"role": "user", "content": "test"}], stream=True)
    attempt = inference.InferenceAttempt(request, body, None,
        SimpleNamespace(provider="deepseek", model="deepseek-flash"), upstream)
    attempt.id = RID
    attempt._dispatch = AsyncMock()
    attempt._renew = AsyncMock()
    attempt.finalize = AsyncMock()
    try:
        wire = "".join([frame async for frame in attempt.sse()])
        assert PRIVATE not in wire + caplog.text
        assert RID in caplog.text and "model_context_too_long" in caplog.text
        sent = []
        def gateway_reply(request):
            sent.append(1)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=wire)
        tenant = GatewayProvider(api_key="synthetic-capability", provider_id="custom_cloud",
                                 base_url="https://gateway.invalid/v1")
        previous = tenant._client
        tenant._client = previous.with_options(http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(gateway_reply)))
        await previous.close()
        try:
            chunks = [row async for row in tenant.stream_chat("test-route", body.messages)]
            error = chunks[-1].data
            assert error == safe_provider_error_data("model_context_too_long", request_id=RID, upstream_status=400)
            assert sent == [1]
        finally:
            await tenant._client.close()
    finally:
        await upstream.aclose()
        await original.close()


@pytest.mark.parametrize(("request_id", "status", "expected"), [
    (RID, 400, {"request_id": RID, "upstream_status": 400}),
    (PRIVATE, True, {}),
    (RID.upper(), "400", {}),
    ("x" * 33, 600, {}),
])
def test_only_safe_correlation_fields_are_forwarded(request_id, status, expected):
    data = {"code": "model_context_too_long", "message": PRIVATE,
            "request_id": request_id, "upstream_status": status, "other": PRIVATE}
    assert safe_provider_error_metadata(data) == expected
    forwarded = admission_chunk(SimpleNamespace(body={"error": data}))
    assert forwarded == safe_provider_error_data("model_context_too_long", **expected)
    assert PRIVATE not in json.dumps(forwarded)


@pytest.mark.parametrize(("reason", "code"), [("stop", "model_response_incomplete"), ("length", "model_output_exhausted")])
async def test_incomplete_response_keeps_receipt_before_error_without_replay(reason, code):
    usage = {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
    frame = {"id": "synthetic", "object": "chat.completion.chunk", "created": 1,
             "model": "deepseek-flash", "choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
             "usage": usage}
    sent = []
    def no_final(request):
        sent.append(1)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
            content="data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n")
    provider, original = vendor_provider(no_final)
    try:
        chunks = [row async for row in provider.stream_chat("deepseek-flash", [{"role": "user", "content": "test"}])]
        assert [row.type for row in chunks] == ["usage", "error"]
        assert chunks[0].data["total_tokens"] == 30
        assert chunks[-1].data == safe_provider_error_data(code)
        assert sent == [1]
    finally:
        await provider.aclose()
        await original.close()


def test_plain_max_tokens_parameter_failure_is_not_context_overflow():
    assert not is_context_overflow(RuntimeError("Unsupported parameter max_tokens"))


@pytest.mark.parametrize(("code", "receipt_status", "expected_types"), [
    ("model_output_exhausted", "complete", ["usage", "error"]),
    ("model_response_incomplete", "complete", ["usage", "error"]),
    ("model_connection_interrupted", "partial", ["error"]),
])
async def test_gateway_preserves_confirmed_receipt_before_terminal_error(code, receipt_status, expected_types):
    from app.schemas.provider import StreamChunk
    class Provider:
        async def stream_chat(self, *_args, **_kwargs):
            yield StreamChunk(type="usage", data={"input_total": 100, "output_total": 8192,
                "reasoning": 8192, "total_tokens": 8292, "usage_status": receipt_status})
            yield StreamChunk(type="error", data=safe_provider_error_data(code))
    body = inference.ChatCompletionRequest(model="test-route", messages=[{"role": "user", "content": "test"}],
        stream=True, stream_options={"include_usage": True})
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(database=None)))
    attempt = inference.InferenceAttempt(request, body, None,
        SimpleNamespace(provider="deepseek", model="deepseek-flash"), Provider())
    attempt.id = RID
    attempt._dispatch = AsyncMock()
    attempt._renew = AsyncMock()
    attempt.finalize = AsyncMock()
    wire = "".join([frame async for frame in attempt.sse()])
    def receive(_request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=wire)
    tenant = GatewayProvider(api_key="synthetic-capability", provider_id="custom_cloud", base_url="https://gateway.invalid/v1")
    original = tenant._client
    tenant._client = original.with_options(http_client=httpx.AsyncClient(transport=httpx.MockTransport(receive)))
    await original.close()
    try:
        chunks = [chunk async for chunk in tenant.stream_chat("test-route", body.messages)]
        assert [row.type for row in chunks] == expected_types
        assert chunks[-1].data["code"] == code and chunks[-1].data["request_id"] == RID
        if receipt_status == "complete":
            assert chunks[0].data["reasoning"] == 8192
            assert chunks[0].data["output"] == 0
            assert chunks[0].data["total"] == 8292
        assert attempt.finalize.await_count >= 1
    finally:
        await tenant._client.close()
