"""A failed catalog refresh must not silently change explicit thinking intent.

Both SDK hops use in-memory HTTP transports. No vendor, database, credentials,
runtime container, or paid model is accessed by these contract tests.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json

import httpx
import pytest

from app.cloud import inference
from app.cloud.gateway_provider import GatewayProvider
from app.cloud.model_metadata import public_model
from app.cloud.providers import CloudProvider, DeepSeekCloudProvider, _SafeSDKNode
from app.provider.generic_openai import GenericOpenAIProvider
from app.provider.registry import ProviderRegistry


async def sdk_chain(provider_id, extra_body, *, failed_refresh):
    route = SimpleNamespace(id="fallback-route", name="Fallback", provider=provider_id,
        model="deepseek-flash" if provider_id == "deepseek" else "example-model",
        capabilities={}, input_price_microusd_per_million=0,
        output_price_microusd_per_million=0)
    vendor_requests, gateway_requests, catalogs = [], [], []

    def vendor_reply(request):
        vendor_requests.append(json.loads(request.content))
        header = {"id": "offline", "created": 1, "model": route.model,
                  "object": "chat.completion.chunk"}
        receipt = {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18,
            "prompt_cache_hit_tokens": 4, "prompt_cache_miss_tokens": 6,
            "prompt_tokens_details": {"cached_tokens": 4},
            "completion_tokens_details": {"reasoning_tokens": 2}}
        rows = [header | {"choices": [{"index": 0, "delta": {"content": "OK"}, "finish_reason": None}]},
                header | {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": receipt}]
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
            content="".join("data: " + json.dumps(row) + "\n\n" for row in rows) + "data: [DONE]\n\n")

    delegate = GenericOpenAIProvider("synthetic-only", provider_id=provider_id,
                                    base_url="https://vendor.invalid/v1")
    vendor_original = delegate._client
    delegate._client = _SafeSDKNode(vendor_original.with_options(max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(vendor_reply))))
    wrapper = DeepSeekCloudProvider if provider_id == "deepseek" else CloudProvider
    native = wrapper(delegate, provider_id)

    async def gateway_reply(request):
        if request.method == "GET":
            catalogs.append(1)
            if failed_refresh and len(catalogs) > 1:
                return httpx.Response(503, json={"error": {"message": "Offline catalog unavailable"}})
            return httpx.Response(200, json={"object": "list", "data": [public_model(route)]})
        payload = json.loads(request.content)
        gateway_requests.append(payload)
        body = inference.ChatCompletionRequest(**payload)
        request_context = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(database=None)))
        attempt = inference.InferenceAttempt(request_context, body, None, route, native)
        attempt.id = "6b71b97545a6426e96706214eb55b53f"
        # Only durable admission is replaced: real production conversion,
        # provider selection, SDK serialization and SSE assembly are used.
        attempt._dispatch = AsyncMock()
        attempt._renew = AsyncMock()
        attempt.finalize = AsyncMock()
        wire = "".join([row async for row in attempt.sse()])
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=wire)

    gateway = GatewayProvider(api_key="synthetic-capability", provider_id="custom_cloud",
                              base_url="https://gateway.invalid/v1")
    gateway_original = gateway._client
    gateway._client = gateway_original.with_options(http_client=httpx.AsyncClient(
        transport=httpx.MockTransport(gateway_reply)))
    registry = ProviderRegistry()
    registry.register(gateway)
    try:
        await registry.refresh_models()
        if failed_refresh:
            await registry.refresh_models()
        resolved = registry.resolve_model(route.id, "custom_cloud")
        assert resolved[1].metadata["upstream_provider"] == provider_id
        assert (gateway._models_cache is None) is failed_refresh
        chunks = [row async for row in gateway.stream_chat(route.id,
            [{"role": "user", "content": "Offline contract test"}],
            max_tokens=65536, extra_body=extra_body)]
        assert not [row for row in chunks if row.type == "error"]
        assert len(gateway_requests) == len(vendor_requests) == 1
        assert [row.type for row in chunks] == ["text-delta", "finish", "usage"]
        assert chunks[-1].data["total"] == 18
        return gateway_requests[0], vendor_requests[0]
    finally:
        await registry.shutdown()
        await gateway._client.close()
        await gateway_original.close()
        await native.aclose()
        await vendor_original.close()


@pytest.mark.parametrize("failed_refresh", [False, True])
@pytest.mark.parametrize("provider_id", ["deepseek", "openai"])
@pytest.mark.parametrize("enabled", [True, False, None])
async def test_catalog_refresh_failure_keeps_intent_through_both_sdks_and_authoritative_route(
    failed_refresh, provider_id, enabled,
):
    extra = {"reasoning": {"enabled": enabled}} if enabled is not None else None
    gateway_body, vendor_body = await sdk_chain(provider_id, extra, failed_refresh=failed_refresh)

    assert vendor_body["max_tokens"] == 65536
    if provider_id == "deepseek":
        assert vendor_body["thinking"] == {"type": "enabled" if enabled is True else "disabled"}
        assert vendor_body.get("reasoning_effort") == ("low" if enabled is True else None)
        assert "reasoning" not in vendor_body
        if failed_refresh and enabled is True:
            assert gateway_body["reasoning"] == {"enabled": True}
    else:
        assert "thinking" not in vendor_body and "reasoning_effort" not in vendor_body
        if enabled is False:
            # Preserve the existing explicitly-disabled compatible option.
            assert vendor_body["reasoning"] == {"enabled": False}
        else:
            # An unknown tenant cache cannot leak a generic enable option
            # into a non-DeepSeek vendor's unsupported parameter surface.
            assert "reasoning" not in vendor_body


@pytest.mark.parametrize("extra,thinking,effort", [
    ({"reasoning": {"enabled": True}, "thinking": {"type": "disabled"}}, "disabled", None),
    ({"reasoning": {"enabled": False}, "thinking": {"type": "enabled"}}, "enabled", "low"),
    ({"reasoning": {"enabled": True}, "reasoning_effort": "high"}, "enabled", "high"),
    ({"thinking": {"type": "enabled"}, "reasoning_effort": "medium"}, "enabled", "medium"),
    ({"reasoning": {"enabled": True}, "thinking": {"type": "disabled"}, "reasoning_effort": "high"}, "disabled", "high"),
])
async def test_failed_catalog_preserves_explicit_thinking_and_effort_overrides(extra, thinking, effort):
    _, vendor_body = await sdk_chain("deepseek", extra, failed_refresh=True)
    assert vendor_body["thinking"] == {"type": thinking}
    assert vendor_body.get("reasoning_effort") == effort
    assert "reasoning" not in vendor_body
