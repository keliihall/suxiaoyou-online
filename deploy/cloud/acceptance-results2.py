#!/usr/bin/env python3
"""Once-only, real result-delivery acceptance v2 inside the owning tenant.

This maintenance helper is not a production runtime feature. Importing it does
nothing. An operator must supply a fresh 32-hex run ID. The durable private
O_EXCL guard is never removed, including after an unknown POST outcome. There
are no model retries, task replays, aborts, approval responses or cleanup.

One prompt creates three exact files and explicitly presents one. A second
non-model POST creates an empty peer session in that same new workspace. The
Online content-binary POST is a read endpoint. No SSE connection is ever made.
Ordinary server-owned title/memory services may incur additional model usage;
this helper neither disables them nor infers total billing from task messages.
Only IDs, fixed error codes, counts and hashes are printed. Original helpers
and their guards are untouched. Transcript reads use SQLite mode=ro.
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

HEX32 = re.compile(r"[a-f0-9]{32}\Z")
ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
ROUTE = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
MAX_BYTES = 4 * 1024 * 1024
MAX_WAIT_SECONDS = 240
REPORTS = Path("/tenant/state/result-integrity-acceptance-v2")
DATABASE = Path("/tenant/state/data/suxiaoyou.db")
TOKEN_FIELDS = ("input", "output", "reasoning", "cache_read", "cache_write", "total")
SAFE_CODES = frozenset({
    "model_context_too_long", "model_request_rejected", "model_authentication_failed",
    "model_payment_required", "model_rate_limited", "model_timeout",
    "model_connection_interrupted", "model_usage_unconfirmed", "model_response_incomplete",
    "model_output_exhausted", "model_service_error",
})


class Failure(Exception):
    """Only fixed, locally defined failure codes may leave the helper."""


def require(value, code):
    if not value:
        raise Failure(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def ensure_acceptance_directory(folder):
    require(folder.is_absolute(), "invalid_acceptance_directory")
    require(not any(item.is_symlink() for item in (folder, *folder.parents)), "linked_acceptance_directory")
    folder.mkdir(mode=0o700, exist_ok=True)
    info = folder.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and not stat.S_IMODE(info.st_mode) & 0o077, "unsafe_acceptance_directory")
    parent_fd = os.open(folder.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parent = os.fstat(parent_fd)
        require(parent.st_uid == os.getuid() and not stat.S_IMODE(parent.st_mode) & 0o022,
                "unsafe_acceptance_parent")
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def private_write(path, value, *, exclusive=False):
    target = path if exclusive else path.with_name(path.name + ".next")
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf8") as out:
            os.fchmod(out.fileno(), 0o600)
            json.dump(value, out, separators=(",", ":"), allow_nan=False)
            out.flush()
            os.fsync(out.fileno())
        if not exclusive:
            os.replace(target, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(directory_fd)
            require(info.st_uid == os.getuid() and not stat.S_IMODE(info.st_mode) & 0o077,
                    "unsafe_acceptance_directory")
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if not exclusive:
            target.unlink(missing_ok=True)


def expected_values(run_id):
    require(isinstance(run_id, str) and HEX32.fullmatch(run_id), "invalid_run_id")
    return {
        "process-notes.json": json.dumps({"acceptance": run_id, "stage": "working"}) + "\n",
        "draft.md": "Draft " + run_id + "\n",
        "result.md": "SUXIAOYOU_RESULT_INTEGRITY " + run_id + "\n",
    }


def prompt_body(run_id, workspace, route):
    values = expected_values(run_id)
    require(isinstance(route, str) and ROUTE.fullmatch(route), "invalid_route")
    paths = {name: str(workspace / name) for name in values}
    instructions = [
        "This is a deployment acceptance task in a new empty workspace.",
        "Use only write and present_file. Call write exactly three times, once per file below.",
        "Create each file with exactly its JSON-decoded UTF-8 content:",
    ]
    instructions.extend(json.dumps({"path": paths[name], "content": value}) for name, value in values.items())
    instructions += [
        "After all writes succeed, call present_file exactly once for " + json.dumps(paths["result.md"]) + " with title Deployment acceptance.",
        "The process-notes.json and draft.md are working files; do not present them.",
        "Finally return only the JSON-decoded string on the next line as plain text, without quotes, punctuation or explanation:",
        json.dumps("OK " + run_id),
        "Do not inspect old files, run commands, delegate, use the network or create todos.",
    ]
    return {
        "client_request_id": "results2-" + run_id,
        "text": "\n".join(instructions), "provider_id": "custom_cloud", "model": route,
        "agent": "build", "reasoning": True, "workspace": str(workspace), "attachments": [],
        "permission_presets": {"file_changes": True, "run_commands": False},
        "permission_rules": [{"action": "deny", "permission": "*", "pattern": "*"}]
        + [{"action": "allow", "permission": "write", "pattern": path} for path in paths.values()]
        + [{"action": "allow", "permission": "present_file", "pattern": paths["result.md"]}],
    }, values


def remaining(deadline):
    seconds = deadline - time.monotonic()
    require(seconds > 0, "acceptance_timeout_unknown")
    return seconds


def request_json(client, method, path, *, body=None, deadline):
    # No HTTP or application retry. Redirects are also disabled on the client.
    with client.stream(method, path, json=body, timeout=min(25, remaining(deadline))) as response:
        require(response.status_code in {200, 201}, "runtime_http_failed")
        raw = bytearray()
        for chunk in response.iter_bytes():
            require(len(raw) + len(chunk) <= MAX_BYTES, "runtime_response_too_large")
            raw.extend(chunk)
            remaining(deadline)
    return json.loads(raw)


def history_rows(value):
    require(isinstance(value, dict), "unexpected_history_shape")
    rows = value.get("messages")
    require(isinstance(rows, list) and type(value.get("total")) is int
            and value["total"] == len(rows) <= 200 and value.get("offset") == 0,
            "incomplete_history_page")
    ids = set()
    for row in rows:
        require(isinstance(row, dict) and isinstance(row.get("id"), str) and ID.fullmatch(row["id"])
                and row["id"] not in ids and isinstance(row.get("data"), dict)
                and isinstance(row.get("parts"), list), "unexpected_history_shape")
        ids.add(row["id"])
        require(all(isinstance(p, dict) and isinstance(p.get("data"), dict) for p in row["parts"]),
                "unexpected_part_shape")
    return rows


def hidden_message(data):
    # These messages are hidden in the actual conversation rendering. Do not
    # mistake a later memory accounting shell for the task's final assistant.
    return bool(data.get("system")) or data.get("agent") == "memory"


def visible_history_hash(value):
    return digest([row for row in history_rows(value) if not hidden_message(row["data"])])


def safe_tokens(value):
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in TOKEN_FIELDS if type(value.get(key)) is int
            and 0 <= value[key] <= 10**12}


def verify_task(value, run_id, workspace):
    rows = history_rows(value)
    assistants = [row for row in rows if row["data"].get("role") == "assistant"
                  and not hidden_message(row["data"])]
    require(assistants, "assistant_missing")
    parts = [part["data"] for row in assistants for part in row["parts"]]
    errors = [part for part in parts if part.get("type") == "step-error"
              or (part.get("type") == "step-finish" and part.get("reason") == "error")]
    require(not errors and all(not row["data"].get("error")
            and row["data"].get("finish") not in {"error", "failed", "interrupted", "usage_limited"}
            for row in assistants), "task_terminal_error")
    final = assistants[-1]
    text = "".join(p["data"].get("text", "") for p in final["parts"] if p["data"].get("type") == "text")
    tools = [part for part in parts if part.get("type") == "tool"]
    require(len(tools) == 4 and Counter(p.get("tool") for p in tools) == {"write": 3, "present_file": 1},
            "unexpected_tool_set")
    require(all(isinstance(p.get("state"), dict) and p["state"].get("status") == "completed" for p in tools),
            "tool_not_completed")
    values = expected_values(run_id)
    writes = [p["state"].get("input", {}) for p in tools if p["tool"] == "write"]
    require(all(isinstance(item, dict) for item in writes), "unexpected_write_input")
    require(Counter(item.get("file_path") for item in writes) == {str(workspace / name): 1 for name in values},
            "write_target_mismatch")
    require(all(item.get("content") == values[Path(item["file_path"]).name] for item in writes), "write_input_mismatch")
    presentation = next(p["state"].get("input", {}) for p in tools if p["tool"] == "present_file")
    require(isinstance(presentation, dict) and presentation.get("file_path") == str(workspace / "result.md"),
            "presentation_target_mismatch")
    require(text.strip() == "OK " + run_id, "final_reply_mismatch")
    # Step usage is a task-only diagnostic, never a total tenant/billing claim.
    usage = Counter()
    for part in parts:
        if part.get("type") == "step-finish":
            usage.update(safe_tokens(part.get("tokens")))
    return {"successful_writes": 3, "successful_presentations": 1,
            "tool_names": ["present_file", "write"], "final_reply_exact": True,
            "final_assistant_id": final["id"], "terminal_safe_codes": [],
            "task_step_count": sum(p.get("type") == "step-finish" for p in parts),
            "task_step_usage": dict(usage), "visible_history_sha256": visible_history_hash(value)}


def terminal_evidence(value):
    """Safe diagnostics even when verify_task rejects the terminal response."""
    codes, counts = set(), Counter()
    for row in history_rows(value):
        if hidden_message(row["data"]):
            continue
        for part in row["parts"]:
            data = part["data"]
            if data.get("type") in {"step-start", "step-finish", "step-error", "text", "reasoning", "tool"}:
                counts[data["type"]] += 1
            if data.get("error_code") in SAFE_CODES:
                codes.add(data["error_code"])
            if data.get("type") == "tool" and data.get("state", {}).get("status") == "error":
                codes.add("tool_execution_failed")
    return {"terminal_safe_codes": sorted(codes), "part_type_counts": dict(counts)}


def allowed_accounting(data, *, has_parts):
    # Exact marker contract from WorkspaceMemoryQueue._persist_usage. A system
    # flag alone is insufficient: visible errors or new content must fail.
    allowed = {"role", "agent", "system", "cost", "tokens", "model_id", "provider_id"}
    return (not has_parts and isinstance(data, dict) and set(data) <= allowed
            and data.get("role") == "assistant" and data.get("agent") == "memory"
            and data.get("system") is True and isinstance(data.get("tokens"), dict)
            and isinstance(data.get("cost"), (int, float)) and not isinstance(data.get("cost"), bool)
            and math.isfinite(data["cost"]) and data["cost"] >= 0
            and all(type(value) in {int, float} and math.isfinite(value) and value >= 0
                    for value in data["tokens"].values()))


def transcript_snapshot(path):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid(), "unsafe_transcript_database")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        result, metadata = {}, {}
        for table in ("message", "part"):
            cursor = connection.execute("SELECT * FROM " + table + " ORDER BY id")
            columns = [item[0] for item in cursor.description]
            rows = cursor.fetchall()
            require(len(rows) <= 100000, "transcript_too_large")
            result[table] = {}
            for row in rows:
                fields = dict(zip(columns, row))
                identity = fields["id"]
                require(isinstance(identity, str) and ID.fullmatch(identity), "unexpected_transcript_id")
                result[table][identity] = digest(row)  # all raw columns, including time_updated
                if table == "message":
                    metadata[identity] = json.loads(fields["data"])
            if table == "part":
                with_parts = {dict(zip(columns, row))["message_id"] for row in rows}
        result["allowed_accounting_ids"] = {identity for identity, data in metadata.items()
                                             if allowed_accounting(data, has_parts=identity in with_parts)}
        return result
    finally:
        connection.close()


def snapshot_evidence(snapshot):
    return {table: {"count": len(snapshot[table]), "sha256": digest(snapshot[table]),
                    "row_sha256_by_id": snapshot[table]} for table in ("message", "part")}


def require_preserved(before, after):
    for table in ("message", "part"):
        require(all(after[table].get(identity) == sha for identity, sha in before[table].items()),
                "original_" + table + "_changed_or_missing")


def stable_snapshot(before, after):
    require_preserved(before, after)
    require(set(after["part"]) == set(before["part"]), "unexpected_new_part_after_finish")
    added = set(after["message"]) - set(before["message"])
    require(added <= after["allowed_accounting_ids"], "unexpected_new_message_after_finish")
    return {"original_message_count": len(before["message"]), "original_part_count": len(before["part"]),
            "original_rows_unchanged": True, "added_memory_accounting_count": len(added),
            "added_memory_accounting_ids": sorted(added), "message_count": len(after["message"]),
            "part_count": len(after["part"])}


def file_rows(value):
    rows = value.get("files") if isinstance(value, dict) else None
    require(isinstance(rows, list) and all(isinstance(row, dict) and isinstance(row.get("name"), str)
            and isinstance(row.get("path"), str) for row in rows), "unexpected_files_shape")
    return rows


def require_files(value, expected, workspace):
    rows = file_rows(value)
    require(Counter((row["name"], row["path"]) for row in rows)
            == {(name, str(workspace / name)): 1 for name in expected}, "file_scope_or_path_mismatch")
    return sorted(row["name"] for row in rows)


def verify_download(client, workspace, run_id, *, deadline):
    expected = expected_values(run_id)["result.md"].encode()
    path = workspace / "result.md"
    value = request_json(client, "POST", "/api/files/content-binary",
                         body={"path": str(path), "workspace": str(workspace)}, deadline=deadline)
    require(isinstance(value, dict) and value.get("name") == "result.md" and value.get("path") == str(path)
            and type(value.get("size")) is int and value["size"] == len(expected)
            and isinstance(value.get("content_base64"), str), "download_metadata_mismatch")
    try:
        actual = base64.b64decode(value["content_base64"], validate=True)
    except (ValueError, TypeError):
        raise Failure("invalid_download_encoding") from None
    require(actual == expected, "download_content_mismatch")
    return {"endpoint": "/api/files/content-binary", "requests": 1, "exact_content": True,
            "bytes": len(actual), "sha256": hashlib.sha256(actual).hexdigest()}


def exact_local_files(workspace, values):
    result = {}
    for name, content in values.items():
        fd = os.open(workspace / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= 4096,
                    "unexpected_file_shape")
            actual = source.read(4097)
        require(actual == content.encode(), "file_content_mismatch")
        result[name] = hashlib.sha256(actual).hexdigest()
    return result


def runtime_token():
    token = os.environ.get("CLOUD_ENGINE_AUTH_TOKEN", "")
    if not token:
        fd = os.open("/tenant/state/session_token.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                    and not stat.S_IMODE(info.st_mode) & 0o077 and info.st_size <= 65536,
                    "unsafe_runtime_auth_file")
            token = json.load(source).get("token", "")
    require(isinstance(token, str) and token.startswith("suxiaoyou_st_") and len(token) >= 50
            and all(33 <= ord(c) <= 126 for c in token), "runtime_auth_missing")
    return token


def run(args, state_path, report):
    import httpx
    workspace_root = Path(os.environ.get("CLOUD_ENGINE_WORKSPACE_ROOT", "/tenant/workspaces"))
    require(workspace_root.is_absolute() and workspace_root.is_dir() and ".." not in workspace_root.parts
            and not any(p.is_symlink() for p in (workspace_root, *workspace_root.parents)), "workspace_missing")
    workspace = workspace_root / ("acceptance-results2-" + args.run_id)
    workspace.mkdir(mode=0o700, exist_ok=False)
    body, values = prompt_body(args.run_id, workspace, args.route)
    token = runtime_token()
    port = int(os.environ.get("CLOUD_ENGINE_PORT", "8080"))
    require(1 <= port <= 65535, "invalid_port")
    deadline = time.monotonic() + MAX_WAIT_SECONDS
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, follow_redirects=False,
                      transport=httpx.HTTPTransport(retries=0),
                      headers={"Authorization": "Bearer " + token, "Accept-Language": "en"}) as client:
        def get(path):
            return request_json(client, "GET", path, deadline=deadline)
        require(get("/api/chat/active") == [], "runtime_not_idle")
        before_task = transcript_snapshot(DATABASE)
        report["transcript_before_task"] = snapshot_evidence(before_task)
        report.update(phase="prompt_dispatched_unknown", prompt_dispatches=1)
        private_write(state_path, report)  # fsync before the only inference POST
        accepted = request_json(client, "POST", "/api/chat/prompt", body=body, deadline=deadline)
        session_id, stream_id = accepted.get("session_id"), accepted.get("stream_id")
        require(isinstance(session_id, str) and ID.fullmatch(session_id)
                and isinstance(stream_id, str) and ID.fullmatch(stream_id), "invalid_prompt_response_unknown")
        report.update(session_id=session_id, stream_id=stream_id, phase="running_without_sse", sse_subscriptions=0)
        private_write(state_path, report)
        while True:
            active = get("/api/chat/active")
            require(isinstance(active, list) and all(isinstance(row, dict) for row in active), "unexpected_active_shape")
            if not any(row.get("stream_id") == stream_id for row in active):
                break
            time.sleep(min(2, remaining(deadline)))
        messages = get(f"/api/messages/{session_id}?limit=200&offset=0")
        report.update(terminal_evidence(messages))
        private_write(state_path, report)
        report.update(verify_task(messages, args.run_id, workspace))
        report["file_sha256"] = exact_local_files(workspace, values)
        report["deliverables"] = require_files(get(f"/api/sessions/{session_id}/files"), ["result.md"], workspace)
        report["all_files"] = require_files(get(f"/api/sessions/{session_id}/files?scope=all"), values, workspace)
        report["download"] = verify_download(client, workspace, args.run_id, deadline=deadline)
        require(report["download"]["sha256"] == report["file_sha256"]["result.md"], "download_hash_mismatch")
        report.update(phase="empty_peer_session_dispatched_unknown", peer_session_dispatches=1)
        private_write(state_path, report)
        peer = request_json(client, "POST", "/api/sessions", deadline=deadline,
                            body={"directory": str(workspace), "title": "Deployment acceptance: shared workspace isolation"})
        peer_id = peer.get("id")
        require(isinstance(peer_id, str) and ID.fullmatch(peer_id), "peer_session_response_unknown")
        report["peer_session_id"] = peer_id
        require_files(get(f"/api/sessions/{peer_id}/files"), [], workspace)
        require_files(get(f"/api/sessions/{peer_id}/files?scope=all"), [], workspace)
        require(history_rows(get(f"/api/messages/{peer_id}?limit=200&offset=0")) == [], "peer_session_not_empty")
        baseline = transcript_snapshot(DATABASE)
        require_preserved(before_task, baseline)
        report.update(phase="checking_stability", peer_session_isolated=True, stability_reads=0,
                      transcript_stability_baseline=snapshot_evidence(baseline), stability=[])
        private_write(state_path, report)
        previous_snapshot = baseline
        for _ in range(3):
            require(remaining(deadline) > 11, "acceptance_timeout_unknown")
            time.sleep(11)
            after = transcript_snapshot(DATABASE)
            evidence = stable_snapshot(baseline, after)
            require_preserved(previous_snapshot, after)  # appended accounting is immutable once observed
            previous_snapshot = after
            visible = visible_history_hash(get(f"/api/messages/{session_id}?limit=200&offset=0"))
            require(visible == report["visible_history_sha256"], "visible_history_changed_after_finish")
            require_files(get(f"/api/sessions/{session_id}/files"), ["result.md"], workspace)
            require_files(get(f"/api/sessions/{session_id}/files?scope=all"), values, workspace)
            report["stability"].append(evidence)
            report["stability_reads"] += 1
            report["transcript_last"] = snapshot_evidence(after)
            private_write(state_path, report)
        report.update(phase="passed", ok=True, stable_seconds_at_least=33,
                      original_pre_task_rows_unchanged=True, visible_history_unchanged=True)
    return report


def execute(args, folder=REPORTS, *, runner=run):
    require(isinstance(args.run_id, str) and HEX32.fullmatch(args.run_id), "invalid_run_id")
    require(isinstance(args.route, str) and ROUTE.fullmatch(args.route), "invalid_route")
    ensure_acceptance_directory(folder)
    state_path = folder / (args.run_id + ".json")
    report = {"version": 2, "run_id": args.run_id, "phase": "prepared", "ok": False,
              "prompt_dispatches": 0, "peer_session_dispatches": 0, "sse_subscriptions": 0}
    try:
        private_write(state_path, report, exclusive=True)
    except FileExistsError:
        return {"version": 2, "run_id": args.run_id, "phase": "already_claimed_no_dispatch", "ok": False}, 2
    try:
        report = runner(args, state_path, report)
    except BaseException as error:
        report.update(phase="failed_or_unknown", ok=False,
                      failure=error.args[0] if isinstance(error, Failure) else "acceptance_operation_failed")
    private_write(state_path, report)
    return report, 0 if report.get("ok") else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--route", default="deepseek-office")
    args = parser.parse_args()
    require(os.getuid() == os.geteuid() == 10001, "owning_tenant_uid_required")
    os.umask(0o077)
    report, status = execute(args)
    print(json.dumps(report, separators=(",", ":")))
    return status


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Failure as error:
        print(json.dumps({"version": 2, "ok": False, "failure": error.args[0]}, separators=(",", ":")))
        raise SystemExit(1)
