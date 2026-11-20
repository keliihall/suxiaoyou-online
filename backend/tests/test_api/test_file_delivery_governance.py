"""Delivery intent survives reload without importing shared workspace debris."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models.message import Message, Part
from app.models.session_file import SessionFile
from app.session.file_delivery import explicit_delivery_paths
from app.session.processor import SessionProcessor, _track_session_file
from app.schemas.agent import AgentInfo
from app.tool.builtin.present_file import PresentFileTool
from app.tool.context import ToolContext


pytestmark = pytest.mark.asyncio


async def make_session(client, workspace):
    response = await client.post("/api/sessions", json={"directory": str(workspace)})
    assert response.status_code == 201
    return response.json()["id"]


async def file_list(client, session_id, scope="deliverables"):
    response = await client.get(f"/api/sessions/{session_id}/files?scope={scope}")
    assert response.status_code == 200
    return response.json()["files"]


def processor_for(session_id, session_factory):
    return SessionProcessor(SimpleNamespace(
        job=SimpleNamespace(session_id=session_id),
        session_factory=session_factory,
        current_todos=[],
    ), [], "assistant-message")


async def apply(processor, tool, metadata, *, success=True):
    await processor._apply_tool_side_effects(
        SimpleNamespace(id=tool), SimpleNamespace(success=success, metadata=metadata),
    )


async def test_mutations_remain_inspectable_and_present_promotes_before_stream_refresh(
    app_client, session_factory, tmp_path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = workspace / "requested_helper.py"
    draft = workspace / "draft.md"
    script.write_text("print('requested deliverable')")
    draft.write_text("work in progress")
    session_id = await make_session(app_client, workspace)
    processor = processor_for(session_id, session_factory)

    await apply(processor, "bash", {"written_files": [str(script), str(draft)]})
    assert await file_list(app_client, session_id) == []
    assert {f["type"] for f in await file_list(app_client, session_id, "all")} == {"working"}

    # No persisted tool part exists yet: the TOOL_RESULT-triggered API refresh
    # must already observe the committed role promotion.
    await apply(processor, "present_file", {"file_path": str(script)})
    files = await file_list(app_client, session_id)
    assert [(f["path"], f["type"], f["tool"]) for f in files] == [(str(script), "deliverable", "present_file")]
    await apply(processor, "edit", {"file_path": str(script)})
    assert await file_list(app_client, session_id) == files
    async with session_factory() as db:
        rows = (await db.execute(select(SessionFile).where(SessionFile.session_id == session_id))).scalars().all()
        assert len(rows) == 2
        assert {r.file_path: r.file_type for r in rows} == {str(script): "deliverable", str(draft): "working"}


async def test_only_explicit_outputs_are_delivered_without_filename_blacklists(
    app_client, session_factory, tmp_path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paths = [workspace / name for name in ("transcript.txt", "requested_script.py", "render.png", "scratch.json")]
    for path in paths:
        path.write_text("output")
    session_id = await make_session(app_client, workspace)
    processor = processor_for(session_id, session_factory)
    await apply(processor, "custom_generator", {
        "artifact_files": [str(paths[0]), {"path": str(paths[1])}],
        "written_files": [str(paths[3])],
    })
    await apply(processor, "image_generate", {"file_path": str(paths[2])})
    await apply(processor, "present_file", {"file_path": str(paths[3])}, success=False)
    assert {f["path"] for f in await file_list(app_client, session_id)} == {str(p) for p in paths[:3]}
    await apply(processor, "plugin", {"artifact_delivery": True, "file_path": str(paths[3])})
    assert len(await file_list(app_client, session_id)) == 4


async def test_real_present_file_result_promotes_legacy_row_without_deleting_process_files(
    app_client, session_factory, tmp_path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = workspace / "transcript.txt"
    path.write_text("requested text output")
    session_id = await make_session(app_client, workspace)
    await _track_session_file(session_factory, session_id, str(path), "write", "generated")
    result = await PresentFileTool().execute({"file_path": str(path)}, ToolContext(
        session_id=session_id, message_id="message", call_id="present-actual",
        agent=AgentInfo(name="test", description="", mode="primary"), workspace=str(workspace),
    ))
    assert result.success
    await processor_for(session_id, session_factory)._apply_tool_side_effects(PresentFileTool(), result)
    assert [(f["path"], f["type"]) for f in await file_list(app_client, session_id)] == [(str(path), "deliverable")]
    assert path.read_text() == "requested text output"


async def test_delivery_persistence_failure_is_not_announced_as_success():
    def broken_factory():
        raise RuntimeError("database unavailable")

    with pytest.raises(RuntimeError, match="database unavailable"):
        await _track_session_file(broken_factory, "session", "/workspace/final.txt", "present_file", "deliverable")


async def test_shared_directory_never_supplies_session_membership_and_unknown_is_retained(
    app_client, session_factory, tmp_path,
):
    workspace = tmp_path / "workspace"
    output = workspace / "suxiaoyou_written"
    output.mkdir(parents=True)
    old = output / "old_report.docx"
    unknown = output / "unknown.json"
    unowned = output / "another_session.txt"
    for path in (old, unknown, unowned):
        path.write_text("retained")
    first = await make_session(app_client, workspace)
    second = await make_session(app_client, workspace)
    await _track_session_file(session_factory, first, str(old), "present_file", "deliverable")
    await _track_session_file(session_factory, first, str(unknown), "bash", "generated")
    assert await file_list(app_client, second) == []
    assert await file_list(app_client, second, "all") == []
    assert [f["path"] for f in await file_list(app_client, first)] == [str(old)]
    assert {f["path"] for f in await file_list(app_client, first, "all")} == {str(old), str(unknown)}
    assert all(p.is_file() for p in (old, unknown, unowned))


async def test_history_recovers_successful_explicit_deliveries_not_failed_or_heuristic_paths(
    app_client, session_factory, tmp_path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    names = ["legacy_script.py", "legacy.wav", "legacy.png", "draft.md", "plugin-work.json", "failed.txt", "pending.txt", "foreign.txt"]
    paths = {name: workspace / name for name in names}
    for path in paths.values():
        path.write_text("old output")
    session_id = await make_session(app_client, workspace)
    other = await make_session(app_client, workspace)
    async with session_factory() as db:
        async with db.begin():
            message = Message(session_id=session_id, data={"role": "assistant"})
            foreign = Message(session_id=other, data={"role": "assistant"})
            db.add_all([message, foreign])
            await db.flush()
            specs = [
                ("present_file", "completed", {}, {"file_path": str(paths["legacy_script.py"])}),
                ("custom", "completed", {"artifact_files": [{"file_path": str(paths["legacy.wav"])}]}, {}),
                ("image_generate", "completed", {"file_path": str(paths["legacy.png"])}, {}),
                ("bash", "completed", {"written_files": [str(paths["draft.md"])]}, {}),
                ("custom_writer", "completed", {"written_files": [{"path": str(paths["plugin-work.json"])}]}, {}),
                ("present_file", "error", {"file_path": str(paths["failed.txt"])}, {}),
                ("present_file", "pending", {"file_path": str(paths["pending.txt"])}, {}),
            ]
            for tool, status, metadata, tool_input in specs:
                db.add(Part(message_id=message.id, session_id=session_id, data={
                    "type": "tool", "tool": tool,
                    "state": {"status": status, "metadata": metadata, "input": tool_input},
                }))
            db.add(Part(message_id=foreign.id, session_id=other, data={
                "type": "tool", "tool": "present_file",
                "state": {"status": "completed", "metadata": {"file_path": str(paths["foreign.txt"])}},
            }))
            # Historical recovery is independent of the frontend's 500-message
            # pagination window, including when the delivery is very old.
            db.add_all([Message(session_id=session_id, data={"role": "assistant"}) for _ in range(505)])
    delivered = await file_list(app_client, session_id)
    assert {f["name"] for f in delivered} == {"legacy_script.py", "legacy.wav", "legacy.png"}
    assert {f["type"] for f in delivered} == {"deliverable"}
    all_files = await file_list(app_client, session_id, "all")
    assert {f["name"] for f in all_files} == {"legacy_script.py", "legacy.wav", "legacy.png", "draft.md", "plugin-work.json"}
    assert next(f for f in all_files if f["name"] == "draft.md")["type"] == "generated"


async def test_explicit_delivery_paths_reject_mutation_only_and_malformed_contracts():
    assert explicit_delivery_paths("write", {"file_path": "/workspace/report.md", "written_files": ["/workspace/report.md"]}) == []
    assert explicit_delivery_paths("custom", {"artifact_files": [None, 1, {}, {"path": ""}, "/x", {"file_path": "/x"}]}) == ["/x"]
    assert explicit_delivery_paths("present_file", None, {"file_path": "/requested.py"}) == ["/requested.py"]


async def test_scope_validation_and_explicit_paths_keep_workspace_guard(app_client, session_factory, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("private")
    inside = workspace / "inside.txt"
    inside.write_text("allowed")
    session_id = await make_session(app_client, workspace)
    await _track_session_file(session_factory, session_id, str(outside), "present_file", "deliverable")
    await _track_session_file(session_factory, session_id, str(inside), "present_file", "deliverable")
    assert [f["path"] for f in await file_list(app_client, session_id)] == [str(inside)]
    assert (await app_client.get(f"/api/sessions/{session_id}/files?scope=global")).status_code == 422
