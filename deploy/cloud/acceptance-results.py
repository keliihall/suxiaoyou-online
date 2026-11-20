#!/usr/bin/env python3
"""Once-only real DeepSeek result delivery acceptance inside an owning tenant.

Uses a new workspace and new sessions only. A private durable guard is saved
before each POST; an ambiguous response is never retried. No old task, file,
credential, billing record, or user message is replayed/edited/deleted.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

HEX32 = re.compile(r"[a-f0-9]{32}\Z")
ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
MAX_BYTES = 4 * 1024 * 1024

class Failure(Exception):
    pass

def require(value, code):
    if not value:
        raise Failure(code)

def ensure_acceptance_directory(folder):
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
        with os.fdopen(fd, "w") as out:
            os.fchmod(out.fileno(), 0o600)
            json.dump(value, out, separators=(",", ":"))
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

def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()

def prompt_body(run_id, workspace, route):
    require(HEX32.fullmatch(run_id), "invalid_run_id")
    values = {
        "process-notes.json": json.dumps({"acceptance": run_id, "stage": "working"}) + "\n",
        "draft.md": "Draft " + run_id + "\n",
        "result.md": "SUXIAOYOU_RESULT_INTEGRITY " + run_id + "\n",
    }
    paths = {name: str(workspace / name) for name in values}
    instructions = ["This is a deployment acceptance task in a new empty workspace.",
                    "Use only write and present_file. Create the following three files with exactly their JSON-decoded UTF-8 content:"]
    instructions.extend(json.dumps({"path": paths[name], "content": value}) for name, value in values.items())
    instructions += ["After all writes succeed, call present_file exactly once for " + json.dumps(paths["result.md"]) + " with title Deployment acceptance.",
                     "The process-notes.json and draft.md are working files; do not present them.",
                     "Then return exactly OK " + run_id + ". Do not inspect old files, run commands, delegate, use the network or create todos."]
    return {
        "client_request_id": "results-" + run_id,
        "text": "\n".join(instructions), "provider_id": "custom_cloud", "model": route,
        "agent": "build", "reasoning": True, "workspace": str(workspace), "attachments": [],
        "permission_presets": {"file_changes": True, "run_commands": False},
        "permission_rules": [{"action": "deny", "permission": "*", "pattern": "*"}]
          + [{"action": "allow", "permission": "write", "pattern": path} for path in paths.values()]
          + [{"action": "allow", "permission": "present_file", "pattern": paths["result.md"]}],
    }, values

def request_json(client, method, path, *, body=None):
    response = client.request(method, path, json=body, timeout=25)
    require(response.status_code in {200, 201}, "runtime_http_failed")
    require(len(response.content) <= MAX_BYTES, "runtime_response_too_large")
    return response.json()

def read_new_session(client, session_id):
    return request_json(client, "GET", f"/api/messages/{session_id}?limit=200&offset=0")

def session_evidence(messages):
    rows = messages.get("messages", [])
    require(isinstance(rows, list) and len(rows) <= 200, "unexpected_history_shape")
    return {"total": messages.get("total"), "loaded": len(rows), "sha256": digest(rows),
            "part_count": sum(len(row.get("parts", [])) for row in rows)}

def file_names(value):
    rows = value.get("files", [])
    require(isinstance(rows, list) and all(isinstance(row, dict) and isinstance(row.get("name"), str) for row in rows), "unexpected_files_shape")
    return sorted(row["name"] for row in rows)

def run(args, state_path, report):
    import httpx
    root = Path("/tenant")
    workspace_root = Path(os.environ.get("CLOUD_ENGINE_WORKSPACE_ROOT", str(root / "workspaces")))
    require(workspace_root.is_absolute() and workspace_root.is_dir(), "workspace_missing")
    workspace = workspace_root / ("acceptance-results-" + args.run_id)
    workspace.mkdir(mode=0o700, exist_ok=False)
    token = os.environ.get("CLOUD_ENGINE_AUTH_TOKEN", "")
    require(token.startswith("suxiaoyou_st_") and len(token) >= 50, "runtime_auth_missing")
    port = int(os.environ.get("CLOUD_ENGINE_PORT", "8080"))
    require(1 <= port <= 65535, "invalid_port")
    body, values = prompt_body(args.run_id, workspace, args.route)
    deadline = time.monotonic() + 300
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, follow_redirects=False,
                      headers={"Authorization": "Bearer " + token, "Accept-Language": "en"}) as client:
        require(request_json(client, "GET", "/api/chat/active") == [], "runtime_not_idle")
        report.update(phase="prompt_dispatched_unknown", prompt_dispatches=1)
        private_write(state_path, report)
        accepted = request_json(client, "POST", "/api/chat/prompt", body=body)
        session_id, stream_id = accepted.get("session_id"), accepted.get("stream_id")
        require(isinstance(session_id, str) and ID.fullmatch(session_id) and isinstance(stream_id, str) and ID.fullmatch(stream_id), "invalid_prompt_response_unknown")
        report.update(session_id=session_id, stream_id=stream_id, phase="running_without_sse", sse_subscriptions=0)
        private_write(state_path, report)
        while True:
            require(time.monotonic() < deadline, "acceptance_timeout_unknown")
            active = request_json(client, "GET", "/api/chat/active")
            if not any(row.get("stream_id") == stream_id for row in active):
                break
            time.sleep(2)
        messages = read_new_session(client, session_id)
        rows = messages.get("messages", [])
        assistants = [row for row in rows if row.get("data", {}).get("role") == "assistant"]
        require(assistants, "assistant_missing")
        text = "".join(part["data"].get("text", "") for part in assistants[-1].get("parts", []) if part.get("data", {}).get("type") == "text")
        require(text.strip() == "OK " + args.run_id, "final_reply_mismatch")
        tools = [part["data"] for row in assistants for part in row.get("parts", []) if part.get("data", {}).get("type") == "tool"]
        require({part.get("tool") for part in tools} == {"write", "present_file"}, "unexpected_tool_set")
        require(all(part.get("state", {}).get("status") == "completed" for part in tools), "tool_not_completed")
        report["successful_writes"] = sum(part.get("tool") == "write" for part in tools)
        require(report["successful_writes"] == 3, "write_count_mismatch")
        report["successful_presentations"] = sum(part.get("tool") == "present_file" for part in tools)
        require(report["successful_presentations"] == 1, "presentation_count_mismatch")
        report["file_sha256"] = {}
        for name, content in values.items():
            path = workspace / name
            info = path.lstat()
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= 4096, "unexpected_file_shape")
            raw = path.read_bytes()
            require(raw == content.encode(), "file_content_mismatch")
            report["file_sha256"][name] = hashlib.sha256(raw).hexdigest()
        delivered = request_json(client, "GET", f"/api/sessions/{session_id}/files")
        all_files = request_json(client, "GET", f"/api/sessions/{session_id}/files?scope=all")
        report.update(deliverables=file_names(delivered), all_files=file_names(all_files), history_before=session_evidence(messages))
        require(report["deliverables"] == ["result.md"] and report["all_files"] == sorted(values), "file_scope_mismatch")
        report.update(phase="empty_peer_session_dispatched_unknown", peer_session_dispatches=1)
        private_write(state_path, report)
        peer = request_json(client, "POST", "/api/sessions", body={"directory": str(workspace), "title": "Deployment acceptance: shared workspace isolation"})
        peer_id = peer.get("id")
        require(isinstance(peer_id, str) and ID.fullmatch(peer_id), "peer_session_response_unknown")
        report["peer_session_id"] = peer_id
        require(file_names(request_json(client, "GET", f"/api/sessions/{peer_id}/files")) == [], "unrelated_session_deliverables_mixed")
        require(file_names(request_json(client, "GET", f"/api/sessions/{peer_id}/files?scope=all")) == [], "unrelated_session_working_files_mixed")
        report.update(phase="checking_stability", peer_session_isolated=True, stability_reads=0)
        private_write(state_path, report)
        for index in range(3):
            time.sleep(11)
            after = session_evidence(read_new_session(client, session_id))
            require(after == report["history_before"], "history_changed_after_finish")
            require(file_names(request_json(client, "GET", f"/api/sessions/{session_id}/files")) == ["result.md"], "deliverables_changed_after_finish")
            report["stability_reads"] += 1
            private_write(state_path, report)
        report.update(phase="passed", ok=True, final_reply_exact=True, stable_seconds_at_least=33)
    return report

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--route", default="deepseek-office")
    args = parser.parse_args()
    require(HEX32.fullmatch(args.run_id), "invalid_run_id")
    os.umask(0o077)
    folder = Path("/tenant/state/result-integrity-acceptance")
    ensure_acceptance_directory(folder)
    state_path = folder / (args.run_id + ".json")
    report = {"run_id": args.run_id, "phase": "prepared", "ok": False, "prompt_dispatches": 0, "peer_session_dispatches": 0}
    try:
        private_write(state_path, report, exclusive=True)
    except FileExistsError:
        print(json.dumps({"run_id": args.run_id, "phase": "already_claimed_no_dispatch"}))
        return 2
    try:
        report = run(args, state_path, report)
    except BaseException as error:
        report.update(phase="failed_or_unknown", failure=error.args[0] if isinstance(error, Failure) else "acceptance_operation_failed")
    private_write(state_path, report)
    print(json.dumps(report, separators=(",", ":")))
    return 0 if report.get("ok") else 1

if __name__ == "__main__":
    raise SystemExit(main())
