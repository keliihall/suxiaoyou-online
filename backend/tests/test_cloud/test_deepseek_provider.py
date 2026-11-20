"""Exercise the real OpenAI SDK against an in-memory HTTP transport, never APIs."""

import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import update

from app.cloud.models import ModelRoute
from app.cloud.providers import DeepSeekCloudProvider, build_provider, normalize_deepseek_usage
from app.cloud import worker as worker_module
from tests.test_cloud.test_metering_worker import worker_env, state


def native_usage(*, reasoning=8):
    return {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130,
            "prompt_cache_hit_tokens": 40, "prompt_cache_miss_tokens": 60,
            "completion_tokens_details": {"reasoning_tokens": reasoning}}


class SSEBody(httpx.AsyncByteStream):
    def __init__(self, rows):
        self.rows, self.closed = rows, False

    async def __aiter__(self):
        for row in self.rows:
            yield ("data: " + json.dumps(row, ensure_ascii=False) + "\n\n").encode()
        yield b"data: [DONE]\n\n"

    async def aclose(self):
        self.closed = True


async def sdk_provider(monkeypatch, *, final_text="你好，云端已接入", finish_reason="stop", usage=None, tool_deltas=None):
    monkeypatch.setenv("CLOUD_TEST_MODEL_KEY", "synthetic-test-key")
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    route = SimpleNamespace(provider="deepseek", enabled=True, strict_quota=False,
                            api_key_env="CLOUD_TEST_MODEL_KEY", base_url=None)
    provider = build_provider(route, SimpleNamespace(allow_mock_provider=False))
    assert isinstance(provider, DeepSeekCloudProvider)
    headers = {"id": "in-memory", "object": "chat.completion.chunk", "created": 1, "model": "deepseek-flash"}
    rows = [headers | {"choices": [{"index": 0, "delta": {"reasoning_content": "test reasoning"}, "finish_reason": None}]}]
    if final_text is not None:
        rows.append(headers | {"choices": [{"index": 0, "delta": {"content": final_text}, "finish_reason": None}]})
    for delta in tool_deltas or []:
        rows.append(headers | {"choices": [{"index": 0, "delta": {"tool_calls": delta}, "finish_reason": None}]})
    # DeepSeek's documented final chunk has one choice with no fresh content,
    # its finish_reason and usage together (rather than a usage-only chunk).
    rows.append(headers | {"choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
                           "usage": usage if usage is not None else native_usage()})
    body, sent = SSEBody(rows), []
    def respond(request):
        sent.append((str(request.url), json.loads(request.content)))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body)
    http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    original = provider.delegate._client
    provider.delegate._client = original.with_options(http_client=http)
    await original.close()
    return provider, sent, body


async def test_deepseek_actual_sdk_body_defaults_to_non_thinking_and_preserves_cache(monkeypatch):
    provider, sent, body = await sdk_provider(monkeypatch)
    try:
        chunks = [c async for c in provider.stream_chat("deepseek-flash", [{"role": "user", "content": "你好"}],
                                                       system="测试系统", max_tokens=256)]
        url, payload = sent[0]
        assert len(sent) == 1 and url == "https://api.deepseek.com/v1/chat/completions"
        assert payload["model"] == "deepseek-flash" and payload["max_tokens"] == 256
        assert payload["thinking"] == {"type": "disabled"}
        assert payload["stream"] and payload["stream_options"] == {"include_usage": True}
        assert payload["messages"][0] == {"role": "system", "content": "测试系统"}
        receipt = next(c.data for c in chunks if c.type == "usage")
        assert (receipt["input_total"], receipt["input_uncached"], receipt["output_total"], receipt["cache_read"],
                receipt["reasoning"], receipt["total_tokens"], receipt["usage_status"]) == (100, 60, 30, 40, 8, 130, "complete")
        assert receipt["raw_usage"]["prompt_cache_hit_tokens"] == 40
        assert receipt["raw_usage"]["prompt_cache_miss_tokens"] == 60
        assert chunks[-1].type == "finish" and body.closed
    finally:
        await provider.aclose()


async def test_deepseek_trusted_caller_can_override_thinking_default(monkeypatch):
    provider, sent, _ = await sdk_provider(monkeypatch)
    try:
        _ = [c async for c in provider.stream_chat("deepseek-flash", [{"role": "user", "content": "你好"}],
                extra_body={"thinking": {"type": "enabled"}, "reasoning_effort": "low"})]
        assert sent[0][1]["thinking"] == {"type": "enabled"}
        assert sent[0][1]["reasoning_effort"] == "low"
    finally:
        await provider.aclose()


@pytest.mark.parametrize("finish_reason", ["content_filter", "aborted", "insufficient_system_resource", None])
async def test_deepseek_non_success_finish_keeps_usage_before_error(monkeypatch, finish_reason):
    provider, _, body = await sdk_provider(monkeypatch, finish_reason=finish_reason)
    try:
        chunks = [c async for c in provider.stream_chat("deepseek-flash", [{"role": "user", "content": "你好"}])]
        assert chunks[-2].type == "usage" and chunks[-1].type == "error"
        assert body.closed
    finally:
        await provider.aclose()


async def test_deepseek_closes_sdk_sse_on_consumer_cancellation(monkeypatch):
    provider, _, body = await sdk_provider(monkeypatch)
    try:
        stream = provider.stream_chat("deepseek-flash", [{"role": "user", "content": "你好"}])
        await anext(stream)
        await stream.aclose()
        assert body.closed
    finally:
        await provider.aclose()


def test_deepseek_inconsistent_cache_receipt_is_partial_without_double_counting():
    receipt = normalize_deepseek_usage(native_usage() | {"prompt_cache_miss_tokens": 61})
    assert receipt["usage_status"] == "partial" and receipt["total_tokens"] == 130
    receipt = normalize_deepseek_usage(native_usage() | {"prompt_tokens_details": {"cached_tokens": 41}})
    assert receipt["usage_status"] == "partial" and receipt["input_total"] == 100


async def test_deepseek_only_reasoning_fails_job_but_settles_actual_tokens_and_cost(worker_env, monkeypatch):
    database, _, worker = worker_env
    async with database.sessions() as db:
        await db.execute(update(ModelRoute).where(ModelRoute.id == "model").values(provider="deepseek", model="deepseek-flash"))
        await db.commit()
    provider, sent, body = await sdk_provider(monkeypatch, final_text=None, finish_reason="length", usage=native_usage(reasoning=30))
    monkeypatch.setattr(worker_module, "build_provider", lambda *_args: provider)
    assert await worker.run_once()
    job, requests, events, summary = await state(database)
    assert job.state == "failed" and job.answer == ""
    assert len(requests) == len(events) == len(sent) == 1 and requests[0].state == "settled"
    assert (summary["used_tokens"], summary["reserved_tokens"], summary["total_cost_microusd"]) == (130, 0, 160)
    assert events[0].reasoning == 30 and events[0].cache_read == 40
    assert body.closed


async def test_deepseek_function_tools_use_real_sdk_protocol_and_preserve_native_receipt(monkeypatch):
    deltas = [[{"index": 0, "id": "call_real", "type": "function", "function": {"name": "read", "arguments": '{"path":'}}],
              [{"index": 0, "function": {"arguments": '"data.csv"}'}}]]
    provider, sent, body = await sdk_provider(monkeypatch, final_text=None, finish_reason="tool_calls", tool_deltas=deltas)
    tools = [{"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}]
    try:
        chunks = [chunk async for chunk in provider.stream_chat("deepseek-flash", [{"role": "user", "content": "Read file"}], tools=tools)]
        assert sent[0][1]["tools"] == tools and len(sent) == 1
        call = next(chunk.data for chunk in chunks if chunk.type == "tool-call")
        assert call["id"] == "call_real" and call["name"] == "read"
        assert call["arguments"] == {"path": "data.csv"} and json.loads(call["arguments_json"]) == {"path": "data.csv"}
        assert next(c.data for c in chunks if c.type == "usage")["total_tokens"] == 130
        assert chunks[-1].type == "finish" and chunks[-1].data["reason"] == "tool_calls" and body.closed
    finally:
        await provider.aclose()


async def test_deepseek_empty_function_finish_is_not_success(monkeypatch):
    provider, _, _ = await sdk_provider(monkeypatch, final_text=None, finish_reason="tool_calls")
    try:
        chunks = [chunk async for chunk in provider.stream_chat("deepseek-flash", [{"role": "user", "content": "Read file"}])]
        assert chunks[-1].type == "error"
        assert any(chunk.type == "usage" for chunk in chunks)
    finally:
        await provider.aclose()
