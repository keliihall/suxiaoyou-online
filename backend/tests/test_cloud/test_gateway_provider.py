"""Opaque platform aliases retain upstream model behavior through the real SDK."""
import json
from types import SimpleNamespace

import httpx
import pytest

from app.cloud.gateway_provider import GatewayProvider, install_gateway_provider
from app.cloud.model_metadata import public_model, validated_overrides
from app.cloud.routes import RouteCreate


def route(**values):
    return SimpleNamespace(id="team-assistant", name="团队助手", provider="deepseek", model="deepseek-flash",
                           capabilities={}, input_price_microusd_per_million=1000000,
                           output_price_microusd_per_million=2000000, **values)


def test_catalog_metadata_uses_actual_model_not_opaque_alias():
    model = public_model(route())
    assert model["id"] == "team-assistant" and model["capabilities"]["vision"] is True
    assert model["capabilities"]["reasoning"] and model["capabilities"]["function_calling"]
    assert model["capabilities"]["max_context"] == 1_000_000
    assert model["pricing"] == {"prompt": 1, "completion": 2}
    assert model["metadata"]["upstream_model"] == "deepseek-flash"
    value = route()
    value.model = "deepseek-v4-pro"
    assert public_model(value)["capabilities"]["vision"] is False
    value.capabilities = {"vision": True, "max_context": 64000, "max_output": 2048}
    assert public_model(value)["capabilities"]["vision"] is True
    assert public_model(value)["capabilities"]["max_context"] == 64000
    assert public_model(value)["capabilities"]["max_output"] == 2048


@pytest.mark.parametrize("invalid", [{"api_key": "never"}, {"max_context": -1}, {"max_output": True}, {"vision": "yes"}])
def test_route_capability_override_is_typed_and_bounded(invalid):
    with pytest.raises(ValueError):
        validated_overrides(invalid)
    with pytest.raises(ValueError):
        RouteCreate(id="route", name="Route", provider="deepseek", model="deepseek-flash", capabilities=invalid)


@pytest.mark.parametrize("provider_id,thinking", [("deepseek", False), ("openai", False)])
async def test_real_sdk_preserves_catalog_and_transport_specific_reasoning(provider_id, thinking):
    value = route()
    value.provider = provider_id
    model = public_model(value)
    sent = []
    def response(request):
        if request.method == "GET":
            return httpx.Response(200, json={"object": "list", "data": [model]})
        sent.append(json.loads(request.content))
        headers = {"id": "fixture", "object": "chat.completion.chunk", "created": 1, "model": "team-assistant"}
        rows = [headers | {"choices": [{"index": 0, "delta": {"content": "done"}, "finish_reason": None}]},
                headers | {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}]
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content="".join("data: " + json.dumps(row) + "\n\n" for row in rows) + "data: [DONE]\n\n")
    provider = GatewayProvider(api_key="synthetic-only", provider_id="custom_cloud", base_url="https://gateway.test/v1", kind="openai_compat_custom")
    original = provider._client
    provider._client = original.with_options(http_client=httpx.AsyncClient(transport=httpx.MockTransport(response)), max_retries=0)
    await original.close()
    try:
        models = await provider.list_models()
        assert models[0].name == "团队助手" and models[0].pricing.prompt == 1
        assert models[0].metadata["upstream_provider"] == provider_id
        assert models[0].capabilities.vision == model["capabilities"]["vision"]
        messages = [{"role": "assistant", "content": "previous", "reasoning_content": "prior reasoning"}, {"role": "user", "content": "next"}]
        chunks = [c async for c in provider.stream_chat("team-assistant", messages)]
        assert any(c.type == "text-delta" for c in chunks)
        if thinking:
            assert sent[0]["thinking"] == {"type": "enabled"}
            assert sent[0]["messages"][0]["reasoning_content"] == "prior reasoning"
        else:
            if provider_id == "deepseek":
                assert sent[0]["thinking"] == {"type": "disabled"}
                assert "reasoning_effort" not in sent[0]
            else:
                assert "thinking" not in sent[0] and "reasoning_content" not in sent[0]["messages"][0]
    finally:
        await provider._client.close()


@pytest.mark.parametrize("mode,expected", [
    ("empty", "model_usage_unconfirmed"),
    ("role", "model_usage_unconfirmed"),
    ("partial", "model_usage_unconfirmed"),
    ("finish_only", "model_usage_unconfirmed"),
    ("usage_only", "model_response_incomplete"),
    ("complete", None),
])
async def test_gateway_clean_eof_requires_finish_and_confirmed_usage(mode, expected):
    from app.session.retry import is_retryable
    from app.session.processor import _ProviderReportedStreamError
    sent = []
    def response(request):
        sent.append(request)
        base = {"id": "fixture", "created": 1, "model": "team-assistant"}
        rows = []
        if mode in {"role", "partial", "complete"}:
            delta = {"role": "assistant"} if mode == "role" else {"content": "partial"}
            rows.append(base | {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]})
        if mode in {"finish_only", "complete"}:
            rows.append(base | {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        if mode in {"usage_only", "complete"}:
            rows.append(base | {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}})
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
            content="".join("data: " + json.dumps(row) + "\n\n" for row in rows))
    provider = GatewayProvider(api_key="synthetic-only", provider_id="custom_cloud", base_url="https://gateway.test/v1")
    original = provider._client
    provider._client = original.with_options(http_client=httpx.AsyncClient(transport=httpx.MockTransport(response)))
    await original.close()
    try:
        chunks = [c async for c in provider.stream_chat("team-assistant", [{"role": "user", "content": "next"}])]
        errors = [c.data for c in chunks if c.type == "error"]
        assert len(sent) == 1
        if expected:
            assert len(errors) == 1 and errors[0]["code"] == expected
            assert errors[0]["retryable"] is False
            assert is_retryable(_ProviderReportedStreamError(**{k: errors[0][k] for k in ("message", "code", "retryable")})) is None
        else:
            assert not errors
    finally:
        await provider._client.close()


async def test_gateway_capability_does_not_get_stuck_in_deferred_empty_catalog(monkeypatch):
    from app.provider import factory
    original = factory.create_provider
    monkeypatch.setattr(factory, "_cloud_gateway_installed", False, raising=False)
    monkeypatch.setattr(factory, "create_provider", original)
    monkeypatch.setenv("CLOUD_ENGINE_CAPABILITY", "synthetic-runtime-capability")
    monkeypatch.setenv("CLOUD_ENGINE_GATEWAY_URL", "https://gateway.test/v1")
    install_gateway_provider()
    provider = factory.create_provider("custom_cloud", "suxiaoyou-credential://opaque/reference", base_url="https://wrong.test")
    assert isinstance(provider, GatewayProvider)
    assert str(provider._client.base_url) == "https://gateway.test/v1/"
    assert provider._client.max_retries == 0
    await provider._client.close()
    # No HTTP call is made and no desktop credential reference is resolved.


@pytest.mark.parametrize("extra,expected_mode,effort", [
    (None, "disabled", None),
    ({"reasoning": {"enabled": False}}, "disabled", None),
    ({"reasoning": {"enabled": True}}, "enabled", "low"),
    ({"thinking": {"type": "enabled"}, "reasoning_effort": "high"}, "enabled", "high"),
    ({"thinking": {"type": "disabled"}, "reasoning": {"enabled": True}}, "disabled", None),
])
async def test_deepseek_thinking_switch_and_budget_reach_real_sdk(extra, expected_mode, effort):
    from app.schemas.provider import ModelInfo
    provider = GatewayProvider(api_key="synthetic-only", provider_id="custom_cloud", base_url="https://gateway.test/v1")
    provider._models_cache = [ModelInfo(**public_model(route()), provider_id="custom_cloud")]
    sent = []
    def response(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content='data: {"id":"fixture","created":1,"model":"team-assistant","choices":[{"index":0,"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
    original = provider._client
    provider._client = original.with_options(http_client=httpx.AsyncClient(transport=httpx.MockTransport(response)))
    await original.close()
    try:
        chunks = [c async for c in provider.stream_chat("team-assistant", [{"role": "user", "content": "next"}], max_tokens=65536, extra_body=extra)]
        assert any(c.type == "text-delta" for c in chunks)
        assert len(sent) == 1
        assert sent[0]["thinking"] == {"type": expected_mode}
        assert sent[0].get("reasoning_effort") == effort
        assert sent[0]["max_tokens"] == 65536
        assert "reasoning" not in sent[0]
    finally:
        await provider._client.close()
