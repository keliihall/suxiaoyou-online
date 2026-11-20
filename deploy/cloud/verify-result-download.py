#!/usr/bin/env python3
"""Read-only HTTP download proof for an already passed result-integrity canary.

Run through python stdin as UID 10001 in the SAME owning tenant as acceptance.
Reads its private existing report. Uses only GET session files and the Online
download's POST /api/files/content-binary (a read endpoint, not a mutation).
No model, new task/session/file, local output file, retry or disk write occurs.
Credentials, private paths, response bodies and decoded file bytes stay private.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat

RUN_ID = re.compile(r"[a-f0-9]{32}\Z")
ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
SHA = re.compile(r"[a-f0-9]{64}\Z")
REPORTS = Path("/tenant/state/result-integrity-acceptance")
MAX_JSON = 64 * 1024


class Failure(Exception):
    pass


def require(value, code):
    if not value:
        raise Failure(code)


def expected_content(run_id):
    require(isinstance(run_id, str) and RUN_ID.fullmatch(run_id), "invalid_run_id")
    return ("SUXIAOYOU_RESULT_INTEGRITY " + run_id + "\n").encode()


def validate_report(report, run_id):
    expected = expected_content(run_id)
    require(isinstance(report, dict) and report.get("run_id") == run_id
            and report.get("ok") is True and report.get("phase") == "passed"
            and report.get("prompt_dispatches") == 1 and report.get("peer_session_dispatches") == 1
            and report.get("successful_writes") == 3 and report.get("successful_presentations") == 1
            and report.get("final_reply_exact") is True and report.get("peer_session_isolated") is True
            and type(report.get("stability_reads")) is int and report["stability_reads"] >= 3
            and type(report.get("stable_seconds_at_least")) is int and report["stable_seconds_at_least"] >= 33,
            "acceptance_not_passed")
    require(isinstance(report.get("session_id"), str) and ID.fullmatch(report["session_id"])
            and report.get("deliverables") == ["result.md"]
            and report.get("all_files") == ["draft.md", "process-notes.json", "result.md"]
            and isinstance(report.get("file_sha256"), dict)
            and report["file_sha256"].get("result.md") == hashlib.sha256(expected).hexdigest(),
            "acceptance_identity_mismatch")
    return expected


def read_report(path):
    parent = path.parent.lstat()
    require(stat.S_ISDIR(parent.st_mode) and parent.st_uid == os.getuid()
            and not stat.S_IMODE(parent.st_mode) & 0o077, "unsafe_acceptance_directory")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
                and 0 < info.st_size <= MAX_JSON, "unsafe_acceptance_report")
        raw = source.read(MAX_JSON + 1)
    require(len(raw) == info.st_size, "acceptance_report_changed")
    def unique(pairs):
        result = {}
        for name, value in pairs:
            require(name not in result, "duplicate_report_key")
            result[name] = value
        return result
    return json.loads(raw, object_pairs_hook=unique)


def read_json(client, method, path, *, body=None):
    with client.stream(method, path, json=body, timeout=25) as response:
        require(response.status_code == 200, "file_read_http_failed")
        raw = bytearray()
        for chunk in response.iter_bytes():
            require(len(raw) + len(chunk) <= MAX_JSON, "file_read_too_large")
            raw.extend(chunk)
    return json.loads(raw)


def verify_download(client, report, run_id, workspace):
    expected = validate_report(report, run_id)
    path = workspace / "result.md"
    delivered = read_json(client, "GET", f"/api/sessions/{report['session_id']}/files")
    rows = delivered.get("files")
    require(isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict)
            and rows[0].get("name") == "result.md" and rows[0].get("path") == str(path),
            "delivery_scope_or_path_mismatch")
    # This is the existing Online Save/Download action's exact HTTP read route.
    value = read_json(client, "POST", "/api/files/content-binary",
                      body={"path": str(path), "workspace": str(workspace)})
    require(isinstance(value, dict) and value.get("name") == "result.md"
            and value.get("path") == str(path) and value.get("size") == len(expected)
            and isinstance(value.get("content_base64"), str), "download_metadata_mismatch")
    try:
        actual = base64.b64decode(value["content_base64"], validate=True)
    except (ValueError, TypeError):
        raise Failure("invalid_download_encoding") from None
    identity = hashlib.sha256(actual).hexdigest()
    require(actual == expected and identity == report["file_sha256"]["result.md"], "download_content_mismatch")
    return {"ok": True, "run_id": run_id, "download_endpoint": "/api/files/content-binary",
            "http_reads": 2, "download_requests": 1, "model_requests": 0,
            "bytes": len(actual), "sha256": identity, "exact_content": True, "delivery_scope": True,
            "previous_stability_reads": report["stability_reads"],
            "previous_stable_seconds_at_least": report["stable_seconds_at_least"]}


def main():
    import httpx
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    expected_content(args.run_id)
    require(os.getuid() == os.geteuid() == 10001, "owning_tenant_uid_required")
    report = read_report(REPORTS / (args.run_id + ".json"))
    validate_report(report, args.run_id)
    workspace_root = Path(os.environ.get("CLOUD_ENGINE_WORKSPACE_ROOT", "/tenant/workspaces"))
    require(workspace_root.is_absolute() and ".." not in workspace_root.parts, "invalid_workspace_root")
    workspace = workspace_root / ("acceptance-results-" + args.run_id)
    info = workspace.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and not stat.S_IMODE(info.st_mode) & 0o077, "unsafe_acceptance_workspace")
    workspace = workspace.resolve(strict=True)
    token = os.environ.get("CLOUD_ENGINE_AUTH_TOKEN", "")
    require(token.startswith("suxiaoyou_st_") and len(token) >= 50, "runtime_auth_missing")
    port = int(os.environ.get("CLOUD_ENGINE_PORT", "8080"))
    require(1 <= port <= 65535, "invalid_runtime_port")
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, follow_redirects=False,
                      headers={"Authorization": "Bearer " + token, "Accept-Language": "en"}) as client:
        return verify_download(client, report, args.run_id, workspace)


if __name__ == "__main__":
    try:
        result = main()
        status = 0
    except BaseException as error:
        result = {"ok": False, "error": str(error) if isinstance(error, Failure) else "download_verification_failed"}
        status = 1
    print("SXY_RESULT_DOWNLOAD:" + json.dumps(result, separators=(",", ":")))
    raise SystemExit(status)
