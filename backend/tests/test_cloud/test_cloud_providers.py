from types import SimpleNamespace

import pytest

from app.cloud.providers import (
    CloudProvider, MockProvider, ProviderConfigurationError,
    build_provider, input_reservation, normalize_usage,
)
from app.schemas.provider import StreamChunk


def route(provider="mock", **changes):
    values = dict(provider=provider, enabled=True, strict_quota=False,
                  api_key_env="CLOUD_TEST_MODEL_KEY", base_url=None)
    values.update(changes)
    return SimpleNamespace(**values)


def test_google_cache_is_subset_and_anthropic_creation_is_added():
    google = normalize_usage("google", {"input": 100, "output": 20, "reasoning": 5,
                                         "cache_read": 40, "total": 125})
    assert (google["input_total"], google["input_uncached"], google["total_tokens"], google["usage_status"]) == (100, 60, 125, "complete")
    anthropic = normalize_usage("anthropic", {"input": 10, "output": 7,
                                              "cache_read": 3, "cache_write": 2, "total": 20})
    assert (anthropic["input_total"], anthropic["total_tokens"]) == (15, 22)
    assert anthropic["provider_total"] == 20
    assert anthropic["raw_usage"]["cache_write"] == 2


def test_reasoning_and_compatible_cache_are_not_added_twice():
    normalized = normalize_usage("openai", {"input": 76, "output": 22, "reasoning": 8,
                                            "cache_read": 24, "cache_write": 10, "total": 130})
    assert (normalized["input_total"], normalized["input_uncached"], normalized["output_total"], normalized["total_tokens"]) == (100, 66, 30, 130)
    subscription = normalize_usage("openai-subscription", {"input": 100, "output": 30, "reasoning": 8, "total": 130})
    assert (subscription["output_total"], subscription["output_visible"], subscription["total_tokens"]) == (30, 22, 130)


def test_unknown_extra_native_tool_usage_never_silently_settles_as_complete():
    normalized = normalize_usage("google", {"input": 100, "output": 20, "total": 150})
    assert normalized["usage_status"] == "partial"
    assert normalized["provider_total"] == 150
    with pytest.raises(ValueError):
        normalize_usage("google", {"input": 2, "cache_read": 3})
    with pytest.raises(ValueError):
        normalize_usage("openai", {"input": -1})


def test_input_estimate_includes_final_system_and_tools_and_output():
    messages = [{"role": "user", "content": "你好"}]
    plain = input_reservation(messages, max_output_tokens=10)
    enriched = input_reservation(messages, system="private system", tools=[{"name": "read"}], max_output_tokens=20)
    assert enriched > plain + 10
    with pytest.raises(ValueError):
        input_reservation(messages, max_output_tokens=0)


def test_mock_requires_explicit_enablement_and_real_strict_is_rejected():
    with pytest.raises(ProviderConfigurationError):
        build_provider(route(), SimpleNamespace(allow_mock_provider=False))
    assert isinstance(build_provider(route(), SimpleNamespace(allow_mock_provider=True)), MockProvider)
    with pytest.raises(ProviderConfigurationError, match="verified input-bound"):
        build_provider(route("openai", strict_quota=True), SimpleNamespace(allow_mock_provider=True))


async def test_mock_is_deterministic_bounded_and_streams_without_network():
    messages = [{"role": "user", "content": "你好，mock测试"}]
    provider = MockProvider(delay_seconds=0, chunk_size=2)
    first = [chunk async for chunk in provider.stream_chat("mock", messages, max_tokens=20)]
    second = [chunk async for chunk in provider.stream_chat("mock", messages, max_tokens=20)]
    assert first == second
    output = "".join(c.data["text"] for c in first if c.type == "text-delta")
    receipt = next(c.data for c in first if c.type == "usage")
    assert receipt["output_total"] == len(output.encode("utf-8")) <= 20
    assert receipt["total_tokens"] <= input_reservation(messages, max_output_tokens=20)
    assert len([c for c in first if c.type == "text-delta"]) > 1


async def test_cloud_wrapper_closes_stream_and_does_not_retry_errors():
    closed, calls = [], []
    class Delegate:
        async def stream_chat(self, *args, **kwargs):
            calls.append(1)
            try:
                yield StreamChunk(type="text-delta", data={"text": "first"})
                yield StreamChunk(type="error", data={"message": "transient"})
            finally:
                closed.append(True)
    provider = CloudProvider(Delegate(), "openai")
    chunks = [c async for c in provider.stream_chat("model", [])]
    assert len(calls) == 1 and closed == [True]
    assert chunks[-1].type == "error"
    stream = provider.stream_chat("model", [])
    await anext(stream)
    await stream.aclose()
    assert closed == [True, True]


@pytest.mark.parametrize("provider_id", ["openai", "anthropic", "google"])
async def test_factory_uses_zero_sdk_retries_without_api_calls(provider_id, monkeypatch):
    # SDK construction is network-free and must not depend on workstation proxy
    # extras; no outbound request is made in this test.
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLOUD_TEST_MODEL_KEY", "test-key-not-real")
    provider = build_provider(route(provider_id), SimpleNamespace(allow_mock_provider=False))
    client = provider.delegate._client
    if provider_id == "google":
        assert client._api_client._http_options.retry_options.attempts == 1
    else:
        assert client.max_retries == 0
    await provider.aclose()
