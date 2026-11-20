"""Model context is disposable; the original transcript is not."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Base, Message, Part, Session, SessionContextProjection
from app.session.compaction import CompactionResult, _phase1_prune, run_compaction
from app.session.context_projection import SOURCE_MESSAGE_ID
from app.session.manager import (
    delete_messages_after, get_message_history_for_llm, get_messages, update_message_text,
)
from app.session.microcompact import context_collapse
from app.session.prompt import SessionPrompt, _persist_context_collapse
from app.streaming.manager import GenerationJob


async def seed(factory, *, case="plain", count=12):
    origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
    async with factory() as db, db.begin():
        db.add(Session(id="projection", title="Projection"))
        await db.flush()
        for index in range(count):
            message_id = f"message-{index:02d}"
            role = "user" if index % 2 == 0 else "assistant"
            reasoning_only = case == "reasoning" and index >= 8
            if reasoning_only:
                role = "assistant"
            db.add(Message(id=message_id, session_id="projection", data={"role": role},
                           time_created=origin + timedelta(seconds=index)))
            await db.flush()
            parts = [{"type": "reasoning" if reasoning_only else "text",
                      "text": f"original {message_id} " + "x" * 400}]
            if case == "tools" and role == "assistant":
                parts += [{"type": "tool", "tool": "read", "call_id": f"{message_id}-{tool_index}",
                           "state": {"status": "completed", "input": {},
                                     "output": f"full original tool {index}-{tool_index} " + "y" * 400}}
                          for tool_index in range(3)]
            for part_index, data in enumerate(parts):
                db.add(Part(id=f"{message_id}-part-{part_index}", message_id=message_id,
                            session_id="projection", data=data,
                            time_created=origin + timedelta(seconds=index, microseconds=part_index)))


async def transcript(factory):
    async with factory() as db:
        messages = await get_messages(db, "projection")
        values = [{"id": message.id, "data": message.data,
                   "parts": [{"id": part.id, "data": part.data} for part in message.parts]}
                  for message in messages]
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest(), values


async def history(factory, **kwargs):
    async with factory() as db:
        return await get_message_history_for_llm(db, "projection", **kwargs)


@pytest.mark.parametrize("case", ["plain", "tools", "reasoning"])
async def test_collapse_advances_real_id_without_mutating_transcript(session_factory, case):
    await seed(session_factory, case=case)
    before = await transcript(session_factory)
    original = await history(session_factory, include_source_ids=True)
    collapsed, saved = context_collapse(original)

    assert saved > 0
    assert await _persist_context_collapse("projection", collapsed, session_factory=session_factory)

    assert await transcript(session_factory) == before
    projected = await history(session_factory)
    assert len(projected) < len(original)
    assert projected[0]["content"] == collapsed[0]["content"]
    assert all(SOURCE_MESSAGE_ID not in frame for frame in projected)
    async with session_factory() as db:
        projection = await db.get(SessionContextProjection, "projection")
        assert projection.anchor_message_id == collapsed[1][SOURCE_MESSAGE_ID]
        assert len(await get_messages(db, "projection")) == 12
    if case == "reasoning":
        # These previously retained source messages were deleted by count math.
        assert {row["id"] for row in before[1]} >= {f"message-{i:02d}" for i in range(2, 8)}
    if case == "tools":
        calls = {call["id"] for frame in projected for call in frame.get("tool_calls", [])}
        results = {frame["tool_call_id"] for frame in projected if frame["role"] == "tool"}
        assert calls == results and calls


async def test_projection_survives_database_reopen(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'history.sqlite3'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await seed(factory)
    before = await transcript(factory)
    collapsed, _ = context_collapse(await history(factory, include_source_ids=True))
    assert await _persist_context_collapse("projection", collapsed, session_factory=factory)
    expected = await history(factory)
    await engine.dispose()

    reopened = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'history.sqlite3'}")
    reopened_factory = async_sessionmaker(reopened, class_=AsyncSession, expire_on_commit=False)
    assert await history(reopened_factory) == expected
    assert await transcript(reopened_factory) == before
    await reopened.dispose()


@pytest.mark.parametrize("kind", ["missing_id", "foreign_id", "mid_tool", "same_anchor"])
async def test_invalid_collapse_never_reports_progress_or_changes_history(session_factory, kind):
    await seed(session_factory, case="tools")
    before = await transcript(session_factory)
    frames = await history(session_factory, include_source_ids=True)
    if kind == "missing_id":
        retained = [{"role": "user", "content": "missing"}]
    elif kind == "foreign_id":
        retained = [{"role": "user", "content": "foreign", SOURCE_MESSAGE_ID: "other-session"}]
    elif kind == "mid_tool":
        retained = [next(frame for frame in frames if frame["role"] == "tool")]
    else:
        retained = frames
    assert not await _persist_context_collapse("projection", [{"role": "user", "content": "boundary"}] + retained,
                                               session_factory=session_factory)
    assert await transcript(session_factory) == before
    async with session_factory() as db:
        assert await db.get(SessionContextProjection, "projection") is None


def test_collapse_does_not_split_one_current_execution_tool_frame():
    frames = [{"role": "user", "content": "instruction", SOURCE_MESSAGE_ID: "user"},
              {"role": "assistant", "content": "work", SOURCE_MESSAGE_ID: "assistant"}]
    frames += [{"role": "tool", "tool_call_id": f"tool-{i}", "content": "tool output" * 40,
                SOURCE_MESSAGE_ID: "assistant"} for i in range(20)]
    collapsed, saved = context_collapse(frames)
    assert collapsed == frames and saved == 0


async def test_existing_summary_and_newer_collapse_choose_newest_anchor(session_factory):
    await seed(session_factory, count=20)
    async with session_factory() as db, db.begin():
        part = await db.get(Part, "message-04-part-0")
        part.data = {"type": "text", "text": "[Context Summary]\nlegacy summary"}
        db.add(Part(id="summary-marker", message_id="message-04", session_id="projection",
                    data={"type": "compaction", "auto": True}))
    original = await history(session_factory, include_source_ids=True)
    assert original[0][SOURCE_MESSAGE_ID] == "message-04"
    before = await transcript(session_factory)
    collapsed, saved = context_collapse(original)
    assert saved > 0
    assert await _persist_context_collapse("projection", collapsed, session_factory=session_factory)
    assert await transcript(session_factory) == before

    # A newer full summary supersedes the older model-only collapse boundary.
    async with session_factory() as db, db.begin():
        part = await db.get(Part, "message-18-part-0")
        part.data = {"type": "text", "text": "[Context Summary]\nnewest summary"}
        db.add(Part(id="new-summary-marker", message_id="message-18", session_id="projection",
                    data={"type": "compaction", "auto": True}))
    projected = await history(session_factory)
    assert projected[0]["content"] == "[Context Summary]\nnewest summary"
    assert len(projected) == 2


async def test_removed_or_foreign_projection_anchor_fails_open_to_original_history(session_factory):
    await seed(session_factory)
    async with session_factory() as db, db.begin():
        db.add(SessionContextProjection(session_id="projection", anchor_message_id="missing",
                                       boundary_text="must not appear", pruned_tool_part_ids=[]))
    frames = await history(session_factory)
    assert len(frames) == 12
    assert "must not appear" not in str(frames)


async def test_prune_is_durable_model_only_and_failed_summary_preserves_originals(session_factory, monkeypatch):
    await seed(session_factory, case="tools")
    before = await transcript(session_factory)
    monkeypatch.setattr("app.session.compaction.PROTECTED_TOKEN_BUDGET", 0)
    monkeypatch.setattr("app.session.compaction._phase2_summarize", AsyncMock(return_value=None))
    result = await run_compaction(
        "projection", job=GenerationJob("projection-stream", "projection"),
        session_factory=session_factory, provider_registry=object(), agent_registry=object(),
    )
    assert result.summary is None and result.pruned_parts > 0
    assert await transcript(session_factory) == before
    frames = await history(session_factory)
    tool_outputs = [frame["content"] for frame in frames if frame["role"] == "tool"]
    assert "[truncated]" in tool_outputs
    assert any("full original tool" in output for output in tool_outputs)
    assert await _phase1_prune("projection", session_factory=session_factory) == 0
    async with session_factory() as db:
        projection = await db.get(SessionContextProjection, "projection")
        assert len(projection.pruned_tool_part_ids) == result.pruned_parts
    # Default Provider-bound history has no planning identifiers.
    assert all(SOURCE_MESSAGE_ID not in frame for frame in frames)


async def test_new_summary_commit_clears_collapse_anchor_without_deleting_history(session_factory, monkeypatch):
    await seed(session_factory)
    collapsed, _ = context_collapse(await history(session_factory, include_source_ids=True))
    assert await _persist_context_collapse("projection", collapsed, session_factory=session_factory)
    before = await transcript(session_factory)
    monkeypatch.setattr("app.session.compaction._phase2_summarize", AsyncMock(return_value="complete summary"))
    await run_compaction("projection", job=GenerationJob("projection-stream", "projection"),
                         session_factory=session_factory, provider_registry=object(), agent_registry=object())
    after = await transcript(session_factory)
    assert after[1][:12] == before[1]
    async with session_factory() as db:
        projection = await db.get(SessionContextProjection, "projection")
        assert projection.anchor_message_id is None and not projection.boundary_text
    assert len(await history(session_factory)) == 1


async def enable_foreign_keys(factory):
    async with factory() as db:
        await db.execute(text("PRAGMA foreign_keys=ON"))
        await db.commit()


async def test_session_delete_cascades_disposable_projection(session_factory):
    await enable_foreign_keys(session_factory)
    await seed(session_factory)
    collapsed, _ = context_collapse(await history(session_factory, include_source_ids=True))
    assert await _persist_context_collapse("projection", collapsed, session_factory=session_factory)
    async with session_factory() as db, db.begin():
        await db.delete(await db.get(Session, "projection"))
    async with session_factory() as db:
        assert await db.get(SessionContextProjection, "projection") is None
        assert await get_messages(db, "projection") == []


async def test_edit_deleting_anchor_does_not_hide_rewritten_history(session_factory):
    await enable_foreign_keys(session_factory)
    await seed(session_factory)
    collapsed, _ = context_collapse(await history(session_factory, include_source_ids=True))
    assert await _persist_context_collapse("projection", collapsed, session_factory=session_factory)
    async with session_factory() as db, db.begin():
        assert await delete_messages_after(db, "projection", "message-00") == 11
        await update_message_text(db, "message-00", "edited original instruction")
    assert await history(session_factory) == [{"role": "user", "content": "edited original instruction"}]
    async with session_factory() as db:
        projection = await db.get(SessionContextProjection, "projection")
        assert projection.anchor_message_id is None


async def test_unpersisted_collapse_falls_back_once_without_claiming_success(session_factory, monkeypatch):
    await seed(session_factory)
    prompt = SessionPrompt.__new__(SessionPrompt)
    prompt.job = GenerationJob("projection-stream", "projection", language="en")
    prompt.request = SimpleNamespace(language="en")
    prompt.step, prompt.assistant_msg_id, prompt.checkpoint_binding = 2, "message-11", None
    prompt.workspace, prompt.provider, prompt.model_id, prompt.current_todos = None, None, "summary", []
    prompt._context_collapse_exhausted, prompt._consecutive_compact_failures = False, 0
    prompt.session_factory = session_factory
    prompt.provider_registry = prompt.agent_registry = object()
    prompt._dispatch_required_hook = AsyncMock()
    prompt._inject_system_message = AsyncMock()
    monkeypatch.setattr("app.session.microcompact.context_collapse", lambda *_args, **_kwargs: (
        [{"role": "user", "content": "boundary"}, {"role": "user", "content": "missing source ID"}], 100,
    ))
    summary = AsyncMock(return_value=CompactionResult(summary="complete"))
    monkeypatch.setattr("app.session.compaction.run_compaction", summary)

    assert await prompt._handle_compact_result() is False
    summary.assert_awaited_once()
    assert prompt._context_collapse_exhausted is True
    assert prompt._dispatch_required_hook.await_args_list[-1].args[1]["mode"] == "full"
    async with session_factory() as db:
        assert await db.get(SessionContextProjection, "projection") is None
