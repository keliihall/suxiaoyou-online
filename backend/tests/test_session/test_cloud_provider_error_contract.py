"""Safe cloud categories choose recovery without replaying uncertain attempts."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.cloud.gateway_errors import admission_chunk
from app.schemas.provider import SAFE_PROVIDER_ERRORS, safe_provider_error_data
from app.session.processor import (
    SessionProcessor, _ProviderReportedStreamError, _localized_provider_stream_error,
    _safe_stream_error_metadata,
)
from app.session.retry import is_context_overflow, is_retryable

RID = "17aaf027e6154d11b89b3ec439ad6cc9"


@pytest.mark.parametrize("code", list(SAFE_PROVIDER_ERRORS))
@pytest.mark.parametrize("language", ["zh", "en"])
def test_cloud_code_and_safe_reference_survive_engine_boundary(code, language):
    payload = admission_chunk(SimpleNamespace(body={"error": {
        **safe_provider_error_data(code, request_id=RID, upstream_status=400),
        "message": "PRIVATE max_tokens timeout authentication diagnostic",
    }}))
    error = _ProviderReportedStreamError(**payload)
    actual_code, message = _localized_provider_stream_error(error, language)
    assert actual_code == code
    assert "PRIVATE" not in message
    assert _safe_stream_error_metadata(error) == {"request_id": RID, "upstream_status": 400}
    assert is_retryable(error) is None
    assert is_context_overflow(error) == (code == "model_context_too_long")


async def test_known_context_rejection_enters_compaction_before_terminal_error(monkeypatch):
    from app.session import processor as module
    delete_empty = AsyncMock()
    monkeypatch.setattr(module, "_delete_empty_assistant_messages", delete_empty)
    processor = object.__new__(SessionProcessor)
    processor._sp = SimpleNamespace(session_factory=object(), job=SimpleNamespace(session_id="test"))
    processor._stream_error = _ProviderReportedStreamError("safe fixed text",
        code="model_context_too_long", retryable=False, request_id=RID, upstream_status=400)
    assert await processor._handle_stream_error() == "compact"
    delete_empty.assert_awaited_once()


def test_invalid_metadata_cannot_be_forwarded_from_provider():
    error = _ProviderReportedStreamError("safe", code="model_timeout", retryable=False,
        request_id="PRIVATE", upstream_status=True)
    assert _safe_stream_error_metadata(error) == {}


@pytest.mark.parametrize(("reasoning", "provider_id", "upstream", "expected"), [
    (None, "custom_cloud", "deepseek", None),
    (False, "custom_cloud", "deepseek", {"reasoning": {"enabled": False}}),
    (True, "custom_cloud", "deepseek", {"reasoning": {"enabled": True}}),
    (True, "openai", "deepseek", None),
    (True, "custom_cloud", "openai", None),
    (True, "custom_cloud", None, None),
])
async def test_reasoning_intent_is_explicitly_preserved_in_stream_arguments(monkeypatch, reasoning, provider_id, upstream, expected):
    from app.session import processor as module
    from app.streaming.manager import GenerationJob
    from app.schemas.provider import ModelInfo
    monkeypatch.setattr(module, "_search_quota", SimpleNamespace(get_quota=AsyncMock(return_value=(0, False))))
    monkeypatch.setattr(module, "_native_web_search_allowed", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(module, "evaluate", lambda *_args: "deny")
    info = ModelInfo(id="test-route", name="Test", provider_id=provider_id, metadata={"upstream_provider": upstream})
    prompt = SimpleNamespace(request=SimpleNamespace(reasoning=reasoning), model_info=info,
        job=GenerationJob("test-stream", "test-session"), tool_registry=SimpleNamespace(),
        merged_permissions=object(), provider=SimpleNamespace(id=provider_id))
    processor = SessionProcessor(prompt, [], "test-assistant")
    extra, *_ = await processor._build_stream_args()
    assert extra == expected


async def test_confirmed_usage_is_persisted_in_error_step_with_correlation(session_factory, monkeypatch):
    from app.session import processor as module
    from app.models.session import Session
    from app.models.message import Message, Part
    from sqlalchemy import select
    from app.streaming.manager import GenerationJob
    async with session_factory() as db:
        async with db.begin():
            db.add(Session(id="error-session", directory="."))
            db.add(Message(id="error-message", session_id="error-session", data={"role": "assistant"}))
    job = GenerationJob("error-stream", "error-session")
    prompt = SimpleNamespace(job=job, session_factory=session_factory, total_cost=0,
        model_info=SimpleNamespace(id="test-route", provider_id="custom_cloud",
            pricing=SimpleNamespace(prompt=1, completion=2)))
    processor = SessionProcessor(prompt, [], "error-message")
    processor._init_step_state()
    processor._accumulated_reasoning = "synthetic reasoning"
    processor.usage_data = {"input": 100, "output": 0, "reasoning": 8192, "cache_read": 0, "total": 8292}
    processor._stream_error = _ProviderReportedStreamError("fixed safe diagnostic", code="model_output_exhausted",
        retryable=False, request_id=RID)
    assert await processor._handle_stream_error() == "stop"
    async with session_factory() as db:
        parts = (await db.scalars(select(Part).where(Part.message_id == "error-message"))).all()
    finish = next(part.data for part in parts if part.data["type"] == "step-finish")
    assert finish["error_code"] == "model_output_exhausted" and finish["request_id"] == RID
    assert finish["tokens"]["reasoning"] == 8192 and finish["cost"] > 0
    event = next(event for event in job.events if event.event == "agent-error")
    assert event.data["code"] == "model_output_exhausted" and event.data["request_id"] == RID


def test_error_step_usage_rolls_into_prompt_and_goal_once():
    from app.session.prompt import SessionPrompt
    from app.streaming.manager import GenerationJob
    prompt = object.__new__(SessionPrompt)
    prompt.job = GenerationJob("test-stream", "test-session", goal_id="test-goal", goal_run_id="test-run")
    prompt.total_cost = 0
    prompt._has_any_text = False
    prompt._length_continuations = 0
    prompt._goal_usage_recorded_tokens = 0
    prompt._goal_usage_recorded_cost_microusd = 0
    prompt.total_tokens_accumulated = dict.fromkeys(("input", "output", "reasoning", "cache_read", "cache_write"), 0)
    processor = SimpleNamespace(has_text=False, finish_reason="error", step_cost=0.016484,
        usage_data={"input": 100, "output": 0, "reasoning": 8192, "cache_read": 0, "cache_write": 0, "total": 8292})
    # _loop invokes this once for every process() result, including stop/error.
    prompt._accumulate_step_metrics(processor)
    assert prompt.total_tokens_accumulated["reasoning"] == 8192
    assert prompt.total_cost == processor.step_cost
    assert prompt.job.goal_usage == (8292, 16484)
    # _post_loop repeats only the idempotent delta flush, never the step sum.
    prompt._record_incremental_goal_usage()
    prompt._record_incremental_goal_usage()
    assert prompt.job.goal_usage == (8292, 16484)


@pytest.mark.parametrize(("error_name", "expected_code"), [
    ("ReadTimeout", "model_timeout"), ("RemoteProtocolError", "model_connection_interrupted"),
    ("empty-eof", "model_usage_unconfirmed"),
    ("usage-only-eof", "model_response_incomplete"),
    ("finish-only-eof", "model_usage_unconfirmed"),
    ("partial-text-eof", "model_usage_unconfirmed"),
])
async def test_tenant_gateway_transport_failure_never_replays_in_engine(monkeypatch, error_name, expected_code):
    import httpx
    import json
    from app.cloud.gateway_provider import GatewayProvider
    from app.session import processor as module
    from app.streaming.manager import GenerationJob
    calls = []
    def fail(request):
        calls.append(1)
        if not error_name.endswith("-eof"):
            raise getattr(httpx, error_name)("PRIVATE transport diagnostic", request=request)
        frame = {"id": "synthetic", "object": "chat.completion.chunk", "created": 1, "model": "test-route", "choices": []}
        if error_name == "usage-only-eof":
            frame["usage"] = {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
        if error_name == "finish-only-eof":
            frame["choices"] = [{"index": 0, "delta": {}, "finish_reason": "stop"}]
        if error_name == "partial-text-eof":
            frame["choices"] = [{"index": 0, "delta": {"content": "synthetic partial"}, "finish_reason": None}]
        wire = "" if error_name == "empty-eof" else "data: " + json.dumps(frame) + "\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=wire)
    provider = GatewayProvider(api_key="synthetic-capability", provider_id="custom_cloud", base_url="https://gateway.invalid/v1")
    original = provider._client
    provider._client = original.with_options(http_client=httpx.AsyncClient(transport=httpx.MockTransport(fail)))
    await original.close()
    prompt = SimpleNamespace(job=GenerationJob("gateway-stream", "gateway-session", invocation_source="desktop"), provider=provider,
        model_id="test-route", system_prompt="", agent=object(), tool_registry=object(), discovered_tools=set(),
        request=SimpleNamespace(format=None), session_factory=object(), step=1)
    processor = SessionProcessor(prompt, [{"role": "user", "content": "test"}], "test-message")
    processor._init_step_state()
    processor._build_stream_args = AsyncMock(return_value=(None, 128, None, False))
    processor._check_vision_blocked = AsyncMock(return_value=None)
    processor._goal_provider_admission_allowed = AsyncMock(return_value=True)
    monkeypatch.setattr(module, "_audit_provider_event", AsyncMock())
    monkeypatch.setattr(module, "denied_invocation_capabilities", lambda *_args: ())
    monkeypatch.setattr(module, "stream_llm", lambda current, model, messages, **_kwargs: current.stream_chat(model, messages))
    try:
        assert await processor._stream_llm_with_retry() is None
        assert calls == [1]
        assert processor._stream_error.code == expected_code
        assert processor._stream_error.retryable is False
        assert "PRIVATE" not in str(processor._stream_error)
        assert not any(event.event == "retry" for event in prompt.job.events)
    finally:
        await provider._client.close()
