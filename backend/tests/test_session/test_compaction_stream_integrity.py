"""Only a complete summary may become the durable history anchor."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.message import Message, Part
from app.models.session import Session
from app.schemas.provider import ModelInfo, StreamChunk
from app.session.compaction import CompactionResult, run_compaction
from app.session.manager import get_message_history_for_llm
from app.session.prompt import SessionPrompt
from app.streaming.events import AGENT_ERROR, COMPACTED, COMPACTION_ERROR
from app.streaming.manager import GenerationJob


def text(value="partial summary"):
    return StreamChunk(type="text-delta", data={"text": value})


def finish(reason="stop"):
    return StreamChunk(type="finish", data={"reason": reason})


class SummaryProvider:
    id = "offline-summary"

    def __init__(self, events):
        self.events, self.calls, self.closed = events, 0, False

    async def stream_chat(self, *_args, **_kwargs):
        self.calls += 1
        try:
            for event in self.events:
                if isinstance(event, Exception):
                    raise event
                yield event
        finally:
            self.closed = True


async def compact(session_factory, events, *, visible_summary=False):
    async with session_factory() as db:
        async with db.begin():
            db.add(Session(id="integrity", directory=".", title="Integrity"))
            db.add(Message(id="original", session_id="integrity", data={"role": "user"}))
            await db.flush()
            db.add(Part(id="original-text", message_id="original", session_id="integrity",
                        data={"type": "text", "text": "original instructions must survive"}))
    provider = SummaryProvider(events)
    model = ModelInfo(id="summary", name="Summary", provider_id=provider.id)
    job = GenerationJob("integrity-stream", "integrity", language="en")
    result = await run_compaction(
        "integrity", job=job, session_factory=session_factory,
        provider_registry=SimpleNamespace(all_models=lambda: [model], resolve_model=lambda _: (provider, model)),
        agent_registry=SimpleNamespace(get=lambda _: SimpleNamespace(system_prompt="Summarize")),
        visible_summary=visible_summary,
    )
    async with session_factory() as db:
        parts = list((await db.scalars(select(Part))).all())
        history = await get_message_history_for_llm(db, "integrity")
    return result, provider, job, parts, history


@pytest.mark.parametrize("events", [
    [text(), StreamChunk(type="error", data={"message": "PRIVATE_DIAGNOSTIC"})],
    [text(), RuntimeError("PRIVATE_DIAGNOSTIC")],
    [text()],
    [text(), finish("length")],
    [text(), finish("content_filter")],
    [text(), finish("length"), finish()],
    [finish()],
    [text(), finish(), StreamChunk(type="error", data={"message": "PRIVATE_DIAGNOSTIC"})],
    [text(), finish(), RuntimeError("PRIVATE_DIAGNOSTIC")],
    [text(), finish(), text("late text")],
    [text(), StreamChunk(type="tool-call", data={"name": "unexpected"})],
    [],
])
async def test_incomplete_summary_never_replaces_history(session_factory, events, caplog):
    result, provider, job, parts, history = await compact(session_factory, events)

    assert result.summary is None
    assert provider.calls == 1 and provider.closed
    assert not any(part.data.get("type") == "compaction" for part in parts)
    assert "original instructions must survive" in str(history)
    assert COMPACTION_ERROR in [event.event for event in job.events]
    assert COMPACTED not in [event.event for event in job.events]
    assert "PRIVATE_DIAGNOSTIC" not in str([event.data for event in job.events])
    assert "PRIVATE_DIAGNOSTIC" not in caplog.text


@pytest.mark.parametrize("visible_summary", [False, True])
@pytest.mark.parametrize("usage_first", [False, True])
async def test_complete_summary_keeps_auto_manual_and_usage_order_compatibility(
    session_factory, visible_summary, usage_first,
):
    usage = StreamChunk(type="usage", data={"input": 5, "output": 3, "total": 8})
    events = [text("  complete summary  ")] + ([usage, finish()] if usage_first else [finish(), usage])
    result, provider, job, parts, history = await compact(
        session_factory, events, visible_summary=visible_summary,
    )

    assert result.summary == "complete summary"
    assert provider.calls == 1 and provider.closed
    assert len([part for part in parts if part.data.get("type") == "compaction"]) == 1
    assert "complete summary" in str(history)
    assert "original instructions must survive" not in str(history)
    assert COMPACTED in [event.event for event in job.events]
    assert COMPACTION_ERROR not in [event.event for event in job.events]


def prompt_for_compaction(*, goal=False):
    prompt = SessionPrompt.__new__(SessionPrompt)
    prompt.job = GenerationJob("boundary-stream", "boundary-session", language="en",
        goal_id="goal" if goal else None, goal_run_id="run" if goal else None)
    prompt.request = SimpleNamespace(language="en")
    prompt.step, prompt.assistant_msg_id, prompt.checkpoint_binding = 2, "assistant", None
    prompt.workspace, prompt.model_id = None, "summary"
    prompt.current_todos = [{"status": "pending", "content": "complete the work"}]
    prompt._context_collapse_exhausted = True
    prompt._consecutive_compact_failures = 0
    prompt.session_factory = prompt.provider_registry = prompt.agent_registry = object()
    prompt._dispatch_required_hook = AsyncMock()
    prompt._inject_system_message = AsyncMock()
    return prompt


@pytest.mark.parametrize("outcome", [None, CompactionResult(), CompactionResult(pruned_parts=4),
                                     CompactionResult(summary="  "), RuntimeError("failed")])
async def test_failed_full_compaction_stops_once_without_replay_or_todo_nudge(monkeypatch, outcome):
    prompt = prompt_for_compaction()
    call = AsyncMock(side_effect=outcome) if isinstance(outcome, Exception) else AsyncMock(return_value=outcome)
    monkeypatch.setattr("app.session.compaction.run_compaction", call)

    assert await prompt._handle_compact_result() is True

    call.assert_awaited_once()
    assert prompt.finish_reason == "error" and prompt._consecutive_compact_failures == 1
    errors = [event for event in prompt.job.events if event.event == AGENT_ERROR]
    assert len(errors) == 1 and errors[0].data["code"] == "compaction_failed"
    prompt._inject_system_message.assert_not_awaited()
    assert [call.args[0] for call in prompt._dispatch_required_hook.await_args_list] == ["PreCompact", "PostCompact"]
    assert prompt._dispatch_required_hook.await_args_list[-1].args[1]["outcome"] == "failed"


async def test_successful_summary_clears_failure_count_and_keeps_todo_recovery(monkeypatch):
    prompt = prompt_for_compaction()
    prompt._consecutive_compact_failures = 1
    call = AsyncMock(return_value=CompactionResult(summary="complete"))
    monkeypatch.setattr("app.session.compaction.run_compaction", call)

    assert await prompt._handle_compact_result() is False

    assert prompt._consecutive_compact_failures == 0
    prompt._inject_system_message.assert_awaited_once()
    assert AGENT_ERROR not in [event.event for event in prompt.job.events]


async def test_zero_cost_collapse_does_not_dispatch_paid_summary(session_factory, monkeypatch):
    prompt = prompt_for_compaction()
    prompt.session_factory = session_factory
    prompt._context_collapse_exhausted = False
    prompt._consecutive_compact_failures = 1
    monkeypatch.setattr("app.session.microcompact.context_collapse", lambda *_a, **_kw: ([], 100))
    saved = AsyncMock(return_value=True)
    monkeypatch.setattr("app.session.prompt._persist_context_collapse", saved)
    paid_summary = AsyncMock()
    monkeypatch.setattr("app.session.compaction.run_compaction", paid_summary)

    assert await prompt._handle_compact_result() is False

    paid_summary.assert_not_awaited()
    saved.assert_awaited_once()
    assert prompt._consecutive_compact_failures == 0
    assert AGENT_ERROR not in [event.event for event in prompt.job.events]


async def test_real_loop_does_not_start_another_step_after_failed_compaction(monkeypatch):
    prompt = prompt_for_compaction()
    prompt.step, prompt.total_cost = 0, 0
    prompt._prepare_step_messages = AsyncMock(return_value=([], None))
    prompt._create_assistant_message_shell = AsyncMock()
    prompt._apply_pending_steers = AsyncMock(return_value=0)
    prompt._should_break_on_hard_cap = AsyncMock(return_value=True)
    attempts = []

    class ContextPressure:
        def __init__(self, **_kwargs):
            attempts.append(self)
            self.finish_reason, self.has_text = "error", False
            self.step_cost, self.usage_data = 0, {}

        async def process(self):
            return "compact"

    monkeypatch.setattr("app.session.processor.SessionProcessor", ContextPressure)
    summary = AsyncMock(return_value=CompactionResult())
    monkeypatch.setattr("app.session.compaction.run_compaction", summary)

    await prompt._loop()

    assert len(attempts) == 1
    summary.assert_awaited_once()
    assert prompt.finish_reason == "error"
    prompt._apply_pending_steers.assert_not_awaited()
    prompt._inject_system_message.assert_not_awaited()


@pytest.mark.parametrize("control", ["abort", "pause", "budget"])
async def test_no_summary_from_control_gate_keeps_control_state(monkeypatch, control):
    prompt = prompt_for_compaction(goal=control != "abort")
    if control == "abort":
        prompt.job.abort_event.set()
    else:
        monkeypatch.setattr("app.session.goal_guard.read_goal_execution_gate", AsyncMock(
            return_value=SimpleNamespace(allowed=control != "pause", status="paused")))
        monkeypatch.setattr("app.session.goal_guard.read_goal_budget_gate", AsyncMock(
            return_value=SimpleNamespace(allowed=False)))
    monkeypatch.setattr("app.session.compaction.run_compaction", AsyncMock(return_value=CompactionResult()))

    assert await prompt._handle_compact_result() is True

    assert prompt.finish_reason == {"abort": "aborted", "pause": "paused", "budget": "budget_limited"}[control]
    assert prompt._consecutive_compact_failures == 0
    assert AGENT_ERROR not in [event.event for event in prompt.job.events]
    assert [call.args[0] for call in prompt._dispatch_required_hook.await_args_list] == ["PreCompact", "PostCompact"]
    prompt._inject_system_message.assert_not_awaited()
