"""A terminal ordinary Agent error must not start another paid model step."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.session import processor as processor_module
from app.session import prompt as prompt_module
from app.session.processor import SessionProcessor
from app.session.prompt import SessionPrompt
from app.streaming.events import AGENT_ERROR, SSEEvent, STEP_START
from app.streaming.manager import GenerationJob


def _loop_prompt(monkeypatch, *, reason, continuation, result="stop"):
    """Use the real loop/nudge policy; replace only provider/storage work."""
    prompt = SessionPrompt.__new__(SessionPrompt)
    prompt.job = GenerationJob("terminal-stream", "terminal-session", invocation_source="desktop")
    prompt.request = SimpleNamespace(
        language="en",
        attachments=[object(), object(), object()] if continuation == "attachments" else [],
    )
    prompt.step = 0
    prompt.assistant_msg_id = "assistant"
    prompt.total_cost = 0.0
    prompt._length_continuations = 0
    prompt.continuation_attempts = 0
    prompt.current_todos = (
        [{"status": "pending", "content": "finish the requested work"}]
        if continuation == "todo" else []
    )
    prompt._prepare_step_messages = AsyncMock(return_value=([], None))
    prompt._create_assistant_message_shell = AsyncMock()
    prompt._inject_system_message = AsyncMock()
    prompt._apply_pending_steers = AsyncMock(
        side_effect=[1, 0] if continuation == "steer" else None,
        return_value=0,
    )
    prompt._handle_compact_result = AsyncMock(return_value=False)
    prompt._should_break_on_hard_cap = AsyncMock(return_value=True)
    monkeypatch.setattr(prompt_module, "_cfg", lambda: SimpleNamespace(
        max_steps=3, max_continuation_attempts=10,
    ))
    steps = []

    class StepWithoutProvider:
        def __init__(self, session_prompt, **_kwargs):
            assert session_prompt is prompt
            steps.append(self)
            self.finish_reason = reason if len(steps) == 1 else "stop"
            self.has_text = continuation != "empty" or len(steps) > 1
            self.step_cost = 0.0
            self.usage_data = {}

        async def process(self):
            if len(steps) > 1:
                prompt.current_todos = []
            elif result == "stop" and self.finish_reason in {"error", "failed"}:
                prompt.job.publish(SSEEvent(AGENT_ERROR, {"code": "model_service_error"}))
            return result if len(steps) == 1 else "stop"

    monkeypatch.setattr(processor_module, "SessionProcessor", StepWithoutProvider)
    return prompt, steps


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["error", "failed"])
@pytest.mark.parametrize("continuation", ["todo", "attachments", "empty", "steer"])
async def test_terminal_error_does_not_resume_ordinary_agent(
    monkeypatch, reason, continuation,
):
    prompt, steps = _loop_prompt(monkeypatch, reason=reason, continuation=continuation)

    await prompt._loop()

    assert len(steps) == 1
    assert prompt.finish_reason == reason
    prompt._inject_system_message.assert_not_awaited()
    prompt._apply_pending_steers.assert_not_awaited()
    assert [event.event for event in prompt.job.events] == [STEP_START, AGENT_ERROR]


@pytest.mark.asyncio
@pytest.mark.parametrize("continuation", ["todo", "attachments", "empty", "length", "steer"])
async def test_successful_steps_keep_existing_continuation_policy(monkeypatch, continuation):
    prompt, steps = _loop_prompt(
        monkeypatch,
        reason="length" if continuation == "length" else "stop",
        continuation=continuation,
    )

    await prompt._loop()

    assert len(steps) == 2
    assert prompt.finish_reason == "stop"
    assert prompt._inject_system_message.await_count == (
        1 if continuation in {"todo", "attachments", "empty"} else 0
    )


@pytest.mark.asyncio
async def test_explicit_context_overflow_recovery_keeps_running(monkeypatch):
    # Real processor classification must still select compaction, not a
    # terminal error; the loop must honor that explicit recovery result.
    monkeypatch.setattr(processor_module, "_delete_empty_assistant_messages", AsyncMock())
    processor = SessionProcessor(
        SimpleNamespace(job=GenerationJob("overflow", "session"), session_factory=object()),
        [], "assistant",
    )
    processor._stream_error = RuntimeError("maximum context length exceeded")
    assert await processor._handle_stream_error() == "compact"

    prompt, steps = _loop_prompt(
        monkeypatch, reason="error", continuation="none", result="compact",
    )
    await prompt._loop()

    assert len(steps) == 2
    prompt._handle_compact_result.assert_awaited_once()
    assert prompt.finish_reason == "stop"
