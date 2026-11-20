"""Exercise narrow presentation grants through the real tool handler/executor."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.agent.permission import evaluate, parse_session_permissions, presets_to_ruleset
from app.models.session_file import SessionFile
from app.schemas.agent import Ruleset
from app.schemas.chat import PromptRequest
from app.session.manager import create_message, create_session, get_messages
from app.session.processor import SessionProcessor
from app.session.prompt import _merge_prompt_permission_layers
from app.streaming.events import TOOL_ERROR, TOOL_RESULT
from app.streaming.manager import GenerationJob
from app.tool.builtin.present_file import PresentFileTool


@pytest.mark.asyncio
@pytest.mark.parametrize("target_name", ["result.md", "draft.md", "result.md.backup"])
async def test_present_file_exact_path_grant_is_enforced_by_handler(
    target_name: str,
    tmp_path: Path,
    session_factory,
    agent_registry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    allowed = workspace / "result.md"
    files = {
        workspace / "process-notes.json": b'{"stage":"working"}\n',
        workspace / "draft.md": b"draft\n",
        allowed: b"final result\n",
        workspace / "result.md.backup": b"another file\n",
    }
    for path, content in files.items():
        path.write_bytes(content)
    rules = [{"action": "deny", "permission": "*", "pattern": "*"}]
    rules.extend(
        {"action": "allow", "permission": "write", "pattern": str(path)}
        for path in (workspace / "process-notes.json", workspace / "draft.md", allowed)
    )
    rules.append({"action": "allow", "permission": "present_file", "pattern": str(allowed)})
    request = PromptRequest(
        text="Present only the final file",
        workspace=str(workspace),
        permission_presets={"file_changes": True, "run_commands": False},
        permission_rules=rules,
        language="en",
    )
    agent = agent_registry.get("build")
    permissions = _merge_prompt_permission_layers(
        agent.permissions,
        presets_to_ruleset(request.permission_presets),
        parse_session_permissions(request.permission_rules),
        Ruleset(),
        request_is_authoritative=False,
    )
    original_rules = permissions.model_dump(mode="json")
    async with session_factory() as db:
        async with db.begin():
            session = await create_session(db, title="Exact presentation grant", directory=str(workspace))
            message = await create_message(db, session_id=session.id, data={"role": "assistant"})

    tool = PresentFileTool()
    execute = AsyncMock(wraps=tool.execute)
    monkeypatch.setattr(tool, "execute", execute)
    job = GenerationJob("present-permission-stream", session.id, invocation_source="desktop")
    job.interactive = False
    checkpoint = AsyncMock()
    prompt = SimpleNamespace(
        job=job,
        session_factory=session_factory,
        tool_registry=SimpleNamespace(get=lambda name: tool if name == "present_file" else None),
        merged_permissions=permissions,
        agent=agent,
        request=request,
        workspace=str(workspace),
        discovered_tools=set(),
        provider_registry=None,
        agent_registry=agent_registry,
        model_id="offline-no-model",
        _record_tool_checkpoint_effects=checkpoint,
    )
    processor = SessionProcessor(prompt, [], message.id)
    processor._init_step_state()
    processor._has_tool_calls = True
    target = workspace / target_name
    await processor._handle_tool_call_chunk(SimpleNamespace(data={
        "id": "present-exact-grant-call",
        "name": "present_file",
        "arguments": {"file_path": str(target), "title": "Final result"},
    }))
    await processor._dispatch_tool_calls()

    async with session_factory() as db:
        history = await get_messages(db, session.id)
        tracked = list((await db.scalars(select(SessionFile).where(SessionFile.session_id == session.id))).all())
    tools = [part.data for row in history for part in row.parts if part.data.get("type") == "tool"]
    assert len(tools) == 1
    state = tools[0]["state"]
    if target_name == "result.md":
        execute.assert_awaited_once()
        checkpoint.assert_awaited_once()
        assert state["status"] == "completed"
        assert state["metadata"]["file_path"] == str(allowed)
        assert [(row.file_path, row.file_type) for row in tracked] == [(str(allowed), "deliverable")]
        assert any(event.event == TOOL_RESULT for event in job.events)
    else:
        execute.assert_not_awaited()
        checkpoint.assert_not_awaited()
        assert state["status"] == "error"
        assert state["output"] == "Permission denied"
        assert not tracked
        assert not processor._streaming_executor.has_submissions
        assert any(event.event == TOOL_ERROR for event in job.events)
        assert not any(event.event == TOOL_RESULT for event in job.events)

    # Presentation neither edits files nor widens the inherited permission scope.
    assert {path: path.read_bytes() for path in files} == files
    assert permissions.model_dump(mode="json") == original_rules
    assert evaluate("present_file", str(allowed), permissions) == "allow"
    assert evaluate("present_file", str(workspace / "draft.md"), permissions) == "deny"
    assert evaluate("read", str(allowed), permissions) == "deny"
    assert evaluate("write", str(workspace / "ungranted.md"), permissions) == "deny"
    assert evaluate("bash", "*", permissions) == "deny"
