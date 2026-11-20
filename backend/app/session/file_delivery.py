"""Explicit file delivery intent shared by persistence and history projection.

Workspace mutations (``written_files`` or a write tool's ``file_path``) are
audit evidence, not a promise that the file is a final user-facing output.
Delivery has no filename/extension blacklist: a requested script is valid.
"""

from __future__ import annotations

from typing import Any


def explicit_delivery_paths(
    tool_id: str,
    metadata: dict[str, Any] | None,
    tool_input: dict[str, Any] | None = None,
) -> list[str]:
    metadata = metadata if isinstance(metadata, dict) else {}
    files: list[str] = []
    declared = metadata.get("artifact_files")
    if isinstance(declared, (list, tuple)):
        for value in declared:
            path = (
                value.get("path") or value.get("file_path")
                if isinstance(value, dict)
                else value
            )
            if isinstance(path, str) and path:
                files.append(path)

    primary = metadata.get("file_path")
    if tool_id == "present_file" and not primary and isinstance(tool_input, dict):
        primary = tool_input.get("file_path")
    if (
        isinstance(primary, str)
        and primary
        and (
            tool_id in {"present_file", "image_generate"}
            or metadata.get("artifact_delivery") is True
        )
    ):
        files.append(primary)
    return list(dict.fromkeys(files))
