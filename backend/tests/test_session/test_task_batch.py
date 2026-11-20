from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent.permission import evaluate, serialize_permission_snapshot
from app.models.session import Session
from app.schemas.agent import PermissionRule, Ruleset
from app.schemas.chat import TaskBatchRequest
from app.session.manager import create_session
from app.session.processor import SessionProcessor
from app.session import processor as processor_module
from app.session.task_batch import run_task_batch
from app.streaming.events import (
    DONE,
    SSEEvent,
    TASK_BATCH_FINISH,
    TASK_BATCH_START,
    TASK_BATCH_UPDATE,
    TEXT_DELTA,
)
from app.streaming.manager import GenerationJob


pytestmark = pytest.mark.asyncio


def _task(title: str, prompt: str, *, model: str | None = None) -> dict:
    return {
        "title": title,
        "prompt": prompt,
        "agent": "explore",
        "model": model,
    }


async def test_parallel_task_batch_streams_progress_and_persists_children(
    session_factory,
    monkeypatch,
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    calls: list[
        tuple[str, str | None, str, str | None, object, Ruleset, bool, bool, str, str | None]
    ] = []

    async def fake_run_generation(job, request, **_kwargs):
        calls.append((
            request.text,
            request.model,
            request.language,
            request.workspace,
            request.permission_presets,
            Ruleset.model_validate({"rules": request.permission_rules}),
            request._permission_rules_authoritative,
            job.abort_event is parent_job.abort_event,
            job.invocation_source,
            job.invocation_source_id,
        ))
        job.publish(SSEEvent(TEXT_DELTA, {"text": f"done {request.text}"}))

    monkeypatch.setattr("app.session.task_batch.run_generation", fake_run_generation)

    job = GenerationJob(
        "stream-1",
        "parent-1",
        invocation_source="channel",
        invocation_source_id="telegram",
    )
    parent_job = job
    body = TaskBatchRequest(
        session_id="parent-1",
        mode="parallel",
        tasks=[
            _task("One", "first", model="model-a"),
            _task("Two", "second", model="model-b"),
        ],
        workspace=str(workspace),
        permission_presets={"run_commands": False},
        permission_rules=[
            {"action": "deny", "permission": "bash", "pattern": "*"},
        ],
        language="en",
    )

    await run_task_batch(
        job,
        body,
        session_factory=session_factory,
        provider_registry=MagicMock(),
        agent_registry=MagicMock(),
        tool_registry=MagicMock(),
    )

    assert [(call[0], call[1], call[2], call[3]) for call in calls] == [
        ("first", "model-a", "en", str(workspace)),
        ("second", "model-b", "en", str(workspace)),
    ]
    assert all(call[4] is None for call in calls)
    assert all(evaluate("bash", "*", call[5]) == "deny" for call in calls)
    assert all(call[6:8] == (True, True) for call in calls)
    assert all(call[8:] == ("channel", "telegram") for call in calls)
    assert [event.event for event in job.events].count(TASK_BATCH_START) == 1
    assert [event.event for event in job.events].count(TASK_BATCH_FINISH) == 1
    assert job.events[-1].event == DONE

    finish_event = next(event for event in job.events if event.event == TASK_BATCH_FINISH)
    assert [task["status"] for task in finish_event.data["tasks"]] == ["completed", "completed"]

    async with session_factory() as db:
        sessions = (await db.execute(Session.__table__.select())).mappings().all()

    parent_rows = [row for row in sessions if row["id"] == "parent-1"]
    child_rows = [row for row in sessions if row["parent_id"] == "parent-1"]
    assert len(parent_rows) == 1
    assert len(child_rows) == 2


async def test_sequential_task_batch_cancels_pending_after_failure(
    session_factory,
    monkeypatch,
    tmp_path,
) -> None:
    (tmp_path / "workspace").mkdir()
    calls: list[str] = []

    async def fake_run_generation(job, request, **_kwargs):
        calls.append(request.text)
        if request.text == "fail":
            job.publish(SSEEvent("agent-error", {"error_message": "child failed"}))
        else:
            job.publish(SSEEvent(TEXT_DELTA, {"text": "ok"}))

    monkeypatch.setattr("app.session.task_batch.run_generation", fake_run_generation)

    job = GenerationJob("stream-1", "parent-1")
    body = TaskBatchRequest(
        session_id="parent-1",
        mode="sequential",
        tasks=[
            _task("One", "ok"),
            _task("Two", "fail"),
            _task("Three", "never"),
        ],
        workspace=str(tmp_path / "workspace"),
    )

    await run_task_batch(
        job,
        body,
        session_factory=session_factory,
        provider_registry=MagicMock(),
        agent_registry=MagicMock(),
        tool_registry=MagicMock(),
    )

    assert calls == ["ok", "fail"]
    finish_event = next(event for event in job.events if event.event == TASK_BATCH_FINISH)
    assert [task["status"] for task in finish_event.data["tasks"]] == [
        "completed",
        "failed",
        "cancelled",
    ]

    updates = [event for event in job.events if event.event == TASK_BATCH_UPDATE]
    assert updates[-1].data["tasks"][1]["error"] == "子任务执行失败。"


async def test_task_batch_without_workspace_fails_before_creating_children(
    session_factory,
    monkeypatch,
) -> None:
    run_generation = MagicMock()
    monkeypatch.setattr("app.session.task_batch.run_generation", run_generation)
    job = GenerationJob("stream-1", "parent-1")
    body = TaskBatchRequest(
        session_id="parent-1",
        tasks=[_task("One", "must not run")],
    )

    await run_task_batch(
        job,
        body,
        session_factory=session_factory,
        provider_registry=MagicMock(),
        agent_registry=MagicMock(),
        tool_registry=MagicMock(),
    )

    run_generation.assert_not_called()
    assert job.events[-1].event == "agent-error"
    assert job.events[-1].data == {
        "error_type": "task_batch_workspace_rejected",
        "code": "task_batch_workspace_rejected",
        "error_message": "多任务工作区不符合安全要求。",
    }
    async with session_factory() as db:
        rows = (await db.execute(Session.__table__.select())).mappings().all()
    assert [row for row in rows if row["id"] == "parent-1"] == []


async def test_existing_parent_workspace_is_canonical_for_every_child(
    session_factory,
    monkeypatch,
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    async with session_factory() as db:
        async with db.begin():
            await create_session(
                db,
                id="parent-1",
                directory=str(workspace),
            )

    captured: list[tuple[str | None, bool]] = []

    async def fake_run_generation(job, request, **_kwargs):
        captured.append((request.workspace, request._permission_rules_authoritative))
        job.publish(SSEEvent(TEXT_DELTA, {"text": "ok"}))

    monkeypatch.setattr("app.session.task_batch.run_generation", fake_run_generation)
    job = GenerationJob("stream-1", "parent-1")
    body = TaskBatchRequest(
        session_id="parent-1",
        mode="parallel",
        tasks=[_task("One", "first"), _task("Two", "second")],
        workspace=str(workspace / "."),
    )

    await run_task_batch(
        job,
        body,
        session_factory=session_factory,
        provider_registry=MagicMock(),
        agent_registry=MagicMock(),
        tool_registry=MagicMock(),
    )

    assert captured == [(str(workspace.resolve()), True)] * 2
    async with session_factory() as db:
        rows = (await db.execute(Session.__table__.select())).mappings().all()
    assert {
        row["directory"] for row in rows if row["parent_id"] == "parent-1"
    } == {str(workspace.resolve())}


async def test_existing_parent_rejects_conflicting_batch_workspace(
    session_factory,
    monkeypatch,
    tmp_path,
) -> None:
    parent_workspace = tmp_path / "parent-workspace"
    conflicting_workspace = tmp_path / "other-workspace"
    parent_workspace.mkdir()
    conflicting_workspace.mkdir()
    async with session_factory() as db:
        async with db.begin():
            await create_session(
                db,
                id="parent-1",
                directory=str(parent_workspace),
            )

    run_generation = MagicMock()
    monkeypatch.setattr("app.session.task_batch.run_generation", run_generation)
    job = GenerationJob("stream-1", "parent-1", language="en")
    body = TaskBatchRequest(
        session_id="parent-1",
        tasks=[_task("One", "must not run")],
        workspace=str(conflicting_workspace),
    )

    await run_task_batch(
        job,
        body,
        session_factory=session_factory,
        provider_registry=MagicMock(),
        agent_registry=MagicMock(),
        tool_registry=MagicMock(),
    )

    run_generation.assert_not_called()
    assert job.events[-1].event == "agent-error"
    assert job.events[-1].data == {
        "error_type": "task_batch_workspace_rejected",
        "code": "task_batch_workspace_rejected",
        "error_message": (
            "The task batch workspace does not satisfy the safety requirements."
        ),
    }
    async with session_factory() as db:
        rows = (await db.execute(Session.__table__.select())).mappings().all()
    assert [row for row in rows if row["parent_id"] == "parent-1"] == []


@pytest.mark.parametrize(
    "untrusted_permissions",
    [
        {},
        {
            "permission_presets": {"run_commands": True, "file_changes": True},
            "permission_rules": [
                {"action": "allow", "permission": "bash", "pattern": "*"},
            ],
        },
    ],
    ids=["omitted", "forged-allow"],
)
async def test_batch_body_cannot_bypass_parent_deny_snapshot(
    session_factory,
    monkeypatch,
    tmp_path,
    untrusted_permissions,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    parent_rules = Ruleset(rules=[
        PermissionRule(action="allow", permission="*", pattern="*"),
        PermissionRule(action="deny", permission="bash", pattern="*"),
    ])
    async with session_factory() as db:
        async with db.begin():
            parent = await create_session(
                db,
                id="parent-deny",
                directory=str(workspace),
            )
            parent.permission_snapshot = serialize_permission_snapshot(parent_rules)

    captured: list[Ruleset] = []

    async def fake_run_generation(job, request, **_kwargs):
        captured.append(Ruleset.model_validate({"rules": request.permission_rules}))
        job.publish(SSEEvent(TEXT_DELTA, {"text": "ok"}))

    monkeypatch.setattr("app.session.task_batch.run_generation", fake_run_generation)
    body = TaskBatchRequest(
        session_id="parent-deny",
        tasks=[_task("One", "must remain denied")],
        workspace=str(workspace),
        **untrusted_permissions,
    )

    await run_task_batch(
        GenerationJob("stream-deny", "parent-deny"),
        body,
        session_factory=session_factory,
        provider_registry=MagicMock(),
        agent_registry=MagicMock(),
        tool_registry=MagicMock(),
    )

    assert len(captured) == 1
    assert evaluate("bash", "*", captured[0]) == "deny"


async def test_batch_body_can_tighten_parent_allow_snapshot(
    session_factory,
    monkeypatch,
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    parent_rules = Ruleset(rules=[
        PermissionRule(action="allow", permission="*", pattern="*"),
        PermissionRule(action="allow", permission="bash", pattern="*"),
    ])
    async with session_factory() as db:
        async with db.begin():
            parent = await create_session(
                db,
                id="parent-allow",
                directory=str(workspace),
            )
            parent.permission_snapshot = serialize_permission_snapshot(parent_rules)

    captured: list[Ruleset] = []

    async def fake_run_generation(job, request, **_kwargs):
        captured.append(Ruleset.model_validate({"rules": request.permission_rules}))
        job.publish(SSEEvent(TEXT_DELTA, {"text": "ok"}))

    monkeypatch.setattr("app.session.task_batch.run_generation", fake_run_generation)
    body = TaskBatchRequest(
        session_id="parent-allow",
        tasks=[_task("One", "run with a tighter ceiling")],
        workspace=str(workspace),
        permission_rules=[
            {"action": "deny", "permission": "bash", "pattern": "*"},
        ],
    )

    await run_task_batch(
        GenerationJob("stream-tighten", "parent-allow"),
        body,
        session_factory=session_factory,
        provider_registry=MagicMock(),
        agent_registry=MagicMock(),
        tool_registry=MagicMock(),
    )

    assert len(captured) == 1
    assert evaluate("bash", "*", captured[0]) == "deny"
    assert evaluate("read", "*", captured[0]) == "allow"


async def test_legacy_permission_and_forged_allow_fall_back_to_headless_ask(
    session_factory,
    monkeypatch,
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    async with session_factory() as db:
        async with db.begin():
            parent = await create_session(
                db,
                id="legacy-parent",
                directory=str(workspace),
            )
            # This legacy field was publicly writable.  Even an allow-shaped
            # value must not become a delegation authority after upgrading.
            parent.permission = [
                {"action": "allow", "permission": "bash", "pattern": "*"},
            ]

    observed: dict[str, object] = {}

    class BashTool:
        id = "bash"

    class BashRegistry:
        def get(self, name):
            return BashTool() if name == "bash" else None

    monkeypatch.setattr(processor_module, "_persist_tool_error", AsyncMock())

    async def exercise_permission_gate(job, request, **_kwargs):
        ceiling = Ruleset.model_validate({"rules": request.permission_rules})
        observed["action"] = evaluate("bash", "*", ceiling)
        observed["interactive"] = job.interactive
        prompt = SimpleNamespace(
            job=job,
            session_factory=session_factory,
            tool_registry=BashRegistry(),
            merged_permissions=ceiling,
        )
        processor = SessionProcessor(prompt, [], "assistant-message")
        processor._init_step_state()
        await processor._handle_tool_call_chunk(SimpleNamespace(data={
            "id": "bash-call",
            "name": "bash",
            "arguments": {"command": "must-not-run"},
        }))

    monkeypatch.setattr(
        "app.session.task_batch.run_generation",
        exercise_permission_gate,
    )
    body = TaskBatchRequest(
        session_id="legacy-parent",
        tasks=[_task("One", "attempt forged command")],
        workspace=str(workspace),
        permission_presets={"run_commands": True},
        permission_rules=[
            {"action": "allow", "permission": "bash", "pattern": "*"},
        ],
    )
    job = GenerationJob(
        "stream-legacy",
        "legacy-parent",
        invocation_source="desktop",
    )

    await run_task_batch(
        job,
        body,
        session_factory=session_factory,
        provider_registry=MagicMock(),
        agent_registry=MagicMock(),
        tool_registry=MagicMock(),
    )

    assert observed == {"action": "ask", "interactive": False}
    finish = next(event for event in job.events if event.event == TASK_BATCH_FINISH)
    assert finish.data["tasks"][0]["status"] == "failed"
    assert finish.data["tasks"][0]["error"] == "子任务执行失败。"


@pytest.mark.parametrize("mode", ["parallel", "sequential"])
@pytest.mark.parametrize("project_id", [None, "batch-project"])
async def test_batch_admits_durable_parent_before_actual_child_checkpoints(
    session_factory, monkeypatch, tmp_path, mode, project_id,
) -> None:
    """Exercise real SQLite admission, not a mocked inherited job contract."""
    import asyncio
    from sqlalchemy import select
    from app.models.session_checkpoint import SessionCheckpoint
    from app.models.turn_run import TurnRun
    from app.models.todo import Todo
    from app.models.project import Project
    from app.runtime.checkpoint_runtime import admit_turn_checkpoint, finish_turn_checkpoint

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    parent = GenerationJob("batch-checkpoints", "batch-parent")
    async with session_factory() as db, db.begin():
        if project_id:
            db.add(Project(id=project_id, name="batch project", worktree=str(workspace)))
            await db.flush()
        await create_session(db, id=parent.session_id, directory=str(workspace), project_id=project_id)
        db.add(Todo(id="parent-todo", session_id=parent.session_id, content="retain task", position=0))
    child_ids = []
    # SQLite's in-memory test engine shares a connection; serialize the fake
    # generations' DB boundaries while keeping the orchestration parallel.
    admission_lock = asyncio.Lock()

    async def child_generation(job, request, **_kwargs):
        async with admission_lock:
            async with session_factory() as db:
                owner = await db.get(TurnRun, parent.turn_run_id)
                assert owner is not None and owner.status == "running"
                assert (await db.get(Session, job.session_id)).project_id == project_id
                checkpoint = await db.scalar(select(SessionCheckpoint).where(
                    SessionCheckpoint.turn_run_id == owner.id))
                assert checkpoint.state == "committing"
                assert owner.request_message_id is not None
                assert checkpoint.todo_snapshot[0]["content"] == "retain task"
            binding = await admit_turn_checkpoint(session_factory, job=job,
                workspace=request.workspace, request_message_id=None, todo_snapshot=[])
            child_ids.append(binding.turn_run_id)
            assert binding.root_turn_id == parent.turn_run_id
            assert binding.workspace_instance_id == parent.workspace_instance_id
            await finish_turn_checkpoint(session_factory, job=job, binding=binding,
                status="completed", response_message_id=None)
        job.publish(SSEEvent(TEXT_DELTA, {"text": "checked"}))

    monkeypatch.setattr("app.session.task_batch.run_generation", child_generation)
    await run_task_batch(parent, TaskBatchRequest(session_id=parent.session_id, mode=mode,
        workspace=str(workspace), tasks=[_task("one", "one"), _task("two", "two")]),
        session_factory=session_factory, provider_registry=MagicMock(),
        agent_registry=MagicMock(), tool_registry=MagicMock())
    assert len(child_ids) == 2
    assert parent.events[-1].event == DONE
    async with session_factory() as db:
        owner = await db.get(TurnRun, parent.turn_run_id)
        assert owner.status == "completed" and owner.response_message_id is not None
        rows = list((await db.scalars(select(TurnRun))).all())
        assert len(rows) == 3 and all(row.status == "completed" for row in rows)
        assert all(row.parent_turn_id == owner.id for row in rows if row.id != owner.id)
        assert all(row.state == "finalized" for row in (await db.scalars(select(SessionCheckpoint))).all())


async def test_batch_cancellation_settles_parallel_children_before_parent_terminalization(
    session_factory, monkeypatch, tmp_path,
) -> None:
    import asyncio
    from app.models.turn_run import TurnRun

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    parent = GenerationJob("cancel-batch", "cancel-parent")
    started, sibling_settled = asyncio.Event(), asyncio.Event()

    async def child_generation(job, request, **_kwargs):
        if request.text == "cancel":
            await started.wait()
            raise asyncio.CancelledError
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            async with session_factory() as db:
                owner = await db.get(TurnRun, parent.turn_run_id)
                assert owner.status == "running"
            sibling_settled.set()

    monkeypatch.setattr("app.session.task_batch.run_generation", child_generation)
    with pytest.raises(asyncio.CancelledError):
        await run_task_batch(parent, TaskBatchRequest(session_id=parent.session_id, mode="parallel",
            workspace=str(workspace), tasks=[_task("one", "cancel"), _task("two", "wait")]),
            session_factory=session_factory, provider_registry=MagicMock(),
            agent_registry=MagicMock(), tool_registry=MagicMock())
    assert sibling_settled.is_set()
    async with session_factory() as db:
        assert (await db.get(TurnRun, parent.turn_run_id)).status == "cancelled"
    assert not any(event.event == DONE for event in parent.events)


async def test_batch_failed_child_closes_parent_turn_as_failed(session_factory, monkeypatch, tmp_path):
    from sqlalchemy import select
    from app.models.session_checkpoint import SessionCheckpoint
    from app.models.turn_run import TurnRun
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    parent = GenerationJob("failed-batch", "failed-parent")

    async def child_generation(job, request, **_kwargs):
        raise RuntimeError("fixture child failure")

    monkeypatch.setattr("app.session.task_batch.run_generation", child_generation)
    await run_task_batch(parent, TaskBatchRequest(session_id=parent.session_id,
        workspace=str(workspace), tasks=[_task("one", "fail")]),
        session_factory=session_factory, provider_registry=MagicMock(),
        agent_registry=MagicMock(), tool_registry=MagicMock())
    async with session_factory() as db:
        assert (await db.get(TurnRun, parent.turn_run_id)).status == "failed"
        checkpoint = await db.scalar(select(SessionCheckpoint).where(
            SessionCheckpoint.turn_run_id == parent.turn_run_id))
        assert checkpoint.state == "finalized"
    assert parent.events[-1].event == DONE


async def test_batch_checkpoint_admission_failure_never_dispatches_children(
    session_factory, monkeypatch, tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    child_generation = AsyncMock()
    monkeypatch.setattr("app.session.task_batch.run_generation", child_generation)
    monkeypatch.setattr("app.session.task_batch.admit_turn_checkpoint",
        AsyncMock(side_effect=RuntimeError("fixture ledger unavailable")))
    parent = GenerationJob("failed-admit", "failed-admit-parent")
    await run_task_batch(parent, TaskBatchRequest(session_id=parent.session_id,
        workspace=str(workspace), tasks=[_task("one", "must not run")]),
        session_factory=session_factory, provider_registry=MagicMock(),
        agent_registry=MagicMock(), tool_registry=MagicMock())
    child_generation.assert_not_called()
    assert parent.events[-1].data["code"] == "task_batch_failed"
    async with session_factory() as db:
        rows = (await db.execute(Session.__table__.select())).mappings().all()
    assert not any(row["parent_id"] == parent.session_id for row in rows)
