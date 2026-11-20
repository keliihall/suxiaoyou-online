#!/usr/bin/env python3
"""Operator-run, once-only real incident acceptance inside an owning tenant.

This file is excluded by the production source packager. It performs no work
when imported. Each mode needs an explicit, stable 32-hex --run-id. A private
O_EXCL guard is written before dispatch; rerunning that ID never dispatches
again, even after an unknown HTTP outcome. There is no retry, approval response,
abort, cleanup, old-history access, or historical billing mutation.

runtime: one idempotent /api/chat/prompt (normally >=2 paid model steps), a new
workspace, and one permitted write target. Server-owned memory/checkpoint/
validation services may create their ordinary records and additional charges.
gateway: one no-tool model request, max_tokens=32, explicit thinking enabled.
An exhausted-output result is expected but cannot be guaranteed by a real model.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import time
from typing import Any

HEX32 = re.compile(r"[a-f0-9]{32}\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
NAME = re.compile(r"[a-zA-Z0-9_-]{1,64}\Z")
TOKEN_FIELDS = ("input", "output", "reasoning", "cache_read", "cache_write", "total")
MAX_WAIT_SECONDS = 240
MAX_HTTP_BYTES = 4 * 1024 * 1024


class AcceptanceFailure(Exception):
    """Only fixed internal codes may leave this helper."""


def checked_id(value: Any, *, hex_only=False) -> str | None:
    return value if isinstance(value, str) and (HEX32 if hex_only else IDENTIFIER).fullmatch(value) else None


def numbers(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {key: item for key in TOKEN_FIELDS if isinstance(item := value.get(key), int)
            and not isinstance(item, bool) and 0 <= item <= 10**12}


def checked_directory(path: Path) -> Path:
    if not path.is_absolute():
        raise AcceptanceFailure("invalid_private_directory")
    for item in (path, *path.parents):
        if item.is_symlink():
            raise AcceptanceFailure("linked_private_directory")
    if not path.is_dir():
        raise AcceptanceFailure("private_directory_missing")
    return path.resolve(strict=True)


def private_json(path: Path, data: dict[str, Any], *, exclusive=False) -> None:
    destination = path if exclusive else path.with_name(path.name + ".next")
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(data, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        if not exclusive:
            os.replace(destination, path)
    finally:
        if not exclusive and destination.exists():
            destination.unlink()


def read_private_json(path: Path) -> dict[str, Any]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
            raise AcceptanceFailure("invalid_private_state")
        value = json.loads(handle.read(65537))
    if not isinstance(value, dict):
        raise AcceptanceFailure("invalid_private_state")
    return value


def claim_once(directory: Path, run_id: str, mode: str) -> tuple[Path, dict[str, Any], bool]:
    if not checked_id(run_id, hex_only=True) or mode not in {"runtime", "gateway"}:
        raise AcceptanceFailure("invalid_run_identity")
    path = directory / f"{run_id}-{mode}.json"
    initial = {"run_id": run_id, "mode": mode, "phase": "prepared", "ok": False,
               "prompt_dispatches": 0, "gateway_model_dispatches": 0}
    try:
        private_json(path, initial, exclusive=True)
    except FileExistsError:
        # No read/modify/request occurs after discovering the once-only guard.
        return path, read_private_json(path), False
    return path, initial, True


def expected_artifact(run_id: str) -> bytes:
    if not checked_id(run_id, hex_only=True):
        raise AcceptanceFailure("invalid_run_identity")
    return f"SUXIAOYOU_INCIDENT_ACCEPTANCE {run_id}\n".encode("ascii")


def artifact_digest(path: Path, expected: bytes) -> dict[str, Any]:
    if not path.exists():
        return {"file_exists": False, "file_exact": False}
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096 or info.st_nlink != 1:
            raise AcceptanceFailure("unexpected_artifact_shape")
        content = handle.read(4097)
    return {"file_exists": True, "file_exact": content == expected,
            "file_sha256": hashlib.sha256(content).hexdigest()}


def build_prompt(run_id: str, workspace: Path, route: str) -> dict[str, Any]:
    target = workspace / "acceptance.txt"
    content = expected_artifact(run_id).decode("ascii")
    return {
        "client_request_id": "incident-" + run_id,
        "text": ("This is an internal deployment acceptance task in a new empty workspace. "
                 "Use only the write tool, exactly once, to create the absolute file "
                 + json.dumps(str(target)) + " with exactly this JSON-decoded UTF-8 content: "
                 + json.dumps(content) + ". Then return only OK. "
                 "Do not inspect existing files, run commands, delegate, access the network, "
                 "make a todo, or call present_file. No explanation or additional work is needed."),
        "provider_id": "custom_cloud", "model": route, "agent": "build", "reasoning": True,
        "workspace": str(workspace), "attachments": [],
        "permission_presets": {"file_changes": True, "run_commands": False},
        # False presets mean ask, not deny. A new session plus this last-match
        # policy denies every tool/resource except the one acceptance file.
        "permission_rules": [{"action": "deny", "permission": "*", "pattern": "*"},
            {"action": "allow", "permission": "write", "pattern": str(target)}],
    }


def runtime_token(root: Path) -> str:
    token = os.environ.get("CLOUD_ENGINE_AUTH_TOKEN", "")
    if not token:
        token = read_private_json(root / "state/session_token.json").get("token", "")
    if not isinstance(token, str) or not token.startswith("suxiaoyou_st_") or len(token) < 50 or any(ord(c) < 33 for c in token):
        raise AcceptanceFailure("runtime_auth_unavailable")
    return token


def remaining(deadline: float) -> float:
    result = deadline - time.monotonic()
    if result <= 0:
        raise AcceptanceFailure("acceptance_timeout_unknown")
    return result


def get_json(client, path: str, deadline: float):
    with client.stream("GET", path, timeout=min(20, remaining(deadline))) as response:
        if response.status_code != 200:
            raise AcceptanceFailure("runtime_read_failed")
        raw = bytearray()
        for piece in response.iter_bytes():
            raw.extend(piece)
            if len(raw) > MAX_HTTP_BYTES:
                raise AcceptanceFailure("runtime_read_too_large")
            remaining(deadline)
    return json.loads(raw)


def apply_event(report: dict[str, Any], event: str, data: dict[str, Any], event_id: str,
                seen_steps: set[str]) -> None:
    from app.schemas.provider import SAFE_PROVIDER_ERRORS, safe_provider_error_metadata
    from app.cloud.gateway_errors import ADMISSION_ERRORS
    if NAME.fullmatch(event):
        report["event_counts"][event] += 1
    if event == "step-finish" and (not event_id or event_id not in seen_steps):
        if event_id:
            seen_steps.add(event_id)
        for key, value in numbers(data.get("tokens")).items():
            report["usage"][key] = report["usage"].get(key, 0) + value
    if event in {"tool-call", "tool-result", "tool-error"}:
        tool = data.get("tool")
        if isinstance(tool, str) and NAME.fullmatch(tool):
            report["tool_names"].add(tool)
        if event == "tool-result" and tool == "write":
            report["write_results"] += 1
    if event in {"agent-error", "error"}:
        code = data.get("code", data.get("error_type"))
        report["terminal_codes"].append(code if isinstance(code, str) and code in {
            *SAFE_PROVIDER_ERRORS, *ADMISSION_ERRORS, "JOB_NOT_FOUND", "server_busy", "session_busy",
            "permission_required", "security_emergency_stop", "security_audit_unavailable",
            "hook_policy_failed", "model_not_found", "MODEL_DOES_NOT_SUPPORT_IMAGES", "compaction_failed",
        } else "unclassified_terminal_error")
        report.update(safe_provider_error_metadata(data))
    if event == "done":
        report["done"] = True
        reason = data.get("finish_reason")
        report["finish_reason"] = reason if reason in {"stop", "length", "error", "failed", "aborted"} else "other"


def plain_report(report: dict[str, Any]) -> dict[str, Any]:
    allowed = {"run_id", "mode", "phase", "ok", "prompt_dispatches", "gateway_model_dispatches", "prompt_http_status",
               "stream_id", "session_id", "unsubscribed_seconds", "background_active_before_subscribe",
               "file_exact_before_subscribe", "event_counts", "usage", "tool_names", "terminal_codes", "write_results",
               "done", "finish_reason", "final_reply_is_ok", "file_exists", "file_exact", "file_sha256",
               "request_id", "upstream_status", "failure_code"}
    return {key: sorted(value) if isinstance(value, set) else dict(value) if isinstance(value, Counter) else value
            for key, value in report.items() if key in allowed}


def run_runtime(args, root: Path, state_path: Path, report: dict[str, Any], deadline: float):
    import httpx
    token = runtime_token(root)
    workspace_root = checked_directory(Path(os.environ.get("CLOUD_ENGINE_WORKSPACE_ROOT", str(root / "workspaces"))))
    workspace = workspace_root / ("acceptance-incident-" + args.run_id)
    workspace.mkdir(mode=0o700, exist_ok=False)
    target = workspace / "acceptance.txt"
    expected = expected_artifact(args.run_id)
    body = build_prompt(args.run_id, workspace, args.route)
    port = int(os.environ.get("CLOUD_ENGINE_PORT", "8080"))
    if not 1 <= port <= 65535:
        raise AcceptanceFailure("invalid_runtime_port")
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, follow_redirects=False,
                      headers={"Authorization": "Bearer " + token, "Accept-Language": "en",
                               "User-Agent": "Suxiaoyou-Incident-Acceptance/1"}) as client:
        active = get_json(client, "/api/chat/active", deadline)
        if not isinstance(active, list) or active:
            raise AcceptanceFailure("runtime_not_idle")
        report.update(phase="prompt_dispatched_unknown", prompt_dispatches=1)
        private_json(state_path, report)
        # Exactly one POST. A lost response never causes a second POST.
        response = client.post("/api/chat/prompt", json=body, timeout=min(25, remaining(deadline)))
        report["prompt_http_status"] = response.status_code
        if response.status_code != 200:
            raise AcceptanceFailure("prompt_not_accepted")
        accepted = response.json()
        stream_id, session_id = checked_id(accepted.get("stream_id")), checked_id(accepted.get("session_id"))
        if not stream_id or not session_id:
            raise AcceptanceFailure("invalid_prompt_response_unknown")
        report.update(stream_id=stream_id, session_id=session_id, phase="accepted_without_sse")
        private_json(state_path, report)
        pause_started = time.monotonic()
        time.sleep(min(2, remaining(deadline)))
        report["unsubscribed_seconds"] = round(time.monotonic() - pause_started, 3)
        active = get_json(client, "/api/chat/active", deadline)
        report["background_active_before_subscribe"] = isinstance(active, list) and any(
            item.get("stream_id") == stream_id and item.get("session_id") == session_id
            for item in active if isinstance(item, dict))
        before = artifact_digest(target, expected)
        report["file_exact_before_subscribe"] = before["file_exact"]
        report.update(event_counts=Counter(), usage={}, tool_names=set(), terminal_codes=[], write_results=0, done=False)
        seen_steps: set[str] = set()
        event, data_lines, event_id = "", [], ""
        with client.stream("GET", "/api/chat/stream/" + stream_id,
                           timeout=min(30, remaining(deadline))) as response:
            if response.status_code != 200:
                raise AcceptanceFailure("stream_observation_failed_unknown")
            for line in response.iter_lines():
                remaining(deadline)
                if len(line) > MAX_HTTP_BYTES:
                    raise AcceptanceFailure("stream_frame_too_large")
                if not line:
                    if event and data_lines:
                        parsed = json.loads("\n".join(data_lines))
                        if not isinstance(parsed, dict):
                            raise AcceptanceFailure("invalid_stream_frame")
                        apply_event(report, event, parsed, event_id, seen_steps)
                        if event in {"done", "agent-error", "error"}:
                            break
                    event, data_lines, event_id = "", [], ""
                elif line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                elif line.startswith("id:"):
                    event_id = line[3:].strip()
        messages = get_json(client, f"/api/messages/{session_id}?limit=50&offset=-1", deadline)
        assistants = [message for message in messages.get("messages", [])
                      if isinstance(message, dict) and message.get("data", {}).get("role") == "assistant"]
        last = assistants[-1] if assistants else {}
        last_text = "".join(str(part.get("data", {}).get("text", "")) for part in last.get("parts", [])
                            if part.get("data", {}).get("type") == "text")
        report["final_reply_is_ok"] = last_text.strip() == "OK"
        report.update(artifact_digest(target, expected))
        report["ok"] = bool(report["done"] and report.get("finish_reason") == "stop"
            and not report["terminal_codes"] and report["file_exact"]
            and report["write_results"] >= 1 and report["tool_names"] == {"write"} and report["final_reply_is_ok"]
            and report["unsubscribed_seconds"] >= 1.99
            and (report["background_active_before_subscribe"] or report["file_exact_before_subscribe"]))
        report["phase"] = "passed" if report["ok"] else "observed_not_passed"
    return plain_report(report)


async def run_gateway(args, report: dict[str, Any], state_path: Path, deadline: float):
    from urllib.parse import urlsplit
    from app.cloud.gateway_provider import GatewayProvider
    from app.schemas.provider import safe_provider_error_metadata
    gateway, capability = os.environ.get("CLOUD_ENGINE_GATEWAY_URL", ""), os.environ.get("CLOUD_ENGINE_CAPABILITY", "")
    parsed = urlsplit(gateway)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path.rstrip("/") != "/api/cloud/internal/inference/v1"
            or len(capability) < 32):
        raise AcceptanceFailure("gateway_identity_unavailable")
    provider = GatewayProvider(api_key=capability, provider_id="custom_cloud", base_url=gateway)
    try:
        models = await asyncio.wait_for(provider.list_models(), timeout=min(15, remaining(deadline)))
        model = next((item for item in models if item.id == args.route), None)
        if model is None or model.metadata.get("upstream_provider") != "deepseek":
            raise AcceptanceFailure("expected_deepseek_route_missing")
        report.update(phase="gateway_dispatched_unknown", gateway_model_dispatches=1, usage={}, terminal_codes=[])
        private_json(state_path, report)
        text = ("Think through a rigorous complete proof before giving the result: determine all integer pairs "
                "(x,y) for which x^4+4y^4 is a prime number, justify every edge case and the factorization. "
                "Do not use tools or external resources.")
        async def consume():
            async for chunk in provider.stream_chat(args.route, [{"role": "user", "content": text}], max_tokens=32,
                    temperature=0, extra_body={"thinking": {"type": "enabled"}, "reasoning_effort": "low"}):
                if chunk.type == "usage":
                    report["usage"] = numbers(chunk.data)
                elif chunk.type == "error":
                    code = chunk.data.get("code")
                    from app.schemas.provider import SAFE_PROVIDER_ERRORS
                    report["terminal_codes"].append(code if isinstance(code, str) and code in SAFE_PROVIDER_ERRORS
                                                    else "unclassified_terminal_error")
                    report.update(safe_provider_error_metadata(chunk.data))
        await asyncio.wait_for(consume(), timeout=remaining(deadline))
        report["ok"] = bool(report["terminal_codes"] == ["model_output_exhausted"]
            and report["usage"].get("total", 0) > 0 and checked_id(report.get("request_id"), hex_only=True))
        report["phase"] = "passed" if report["ok"] else "observed_not_passed"
        return report
    finally:
        await provider._client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("runtime", "gateway"), required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--route", default="deepseek-office")
    args = parser.parse_args()
    report: dict[str, Any] = {"ok": False, "phase": "preflight_failed"}
    state_path = None
    try:
        if not checked_id(args.run_id, hex_only=True) or not checked_id(args.route):
            raise AcceptanceFailure("invalid_run_identity")
        os.umask(0o077)
        root = checked_directory(Path(os.environ.get("CLOUD_ENGINE_TENANT_ROOT", "/tenant")))
        directory = root / "state/incident-acceptance"
        directory.mkdir(mode=0o700, exist_ok=True)
        checked_directory(directory)
        state_path, report, claimed = claim_once(directory, args.run_id, args.mode)
        if not claimed:
            print(json.dumps({**plain_report(report), "already_attempted": True, "no_new_dispatch": True}, separators=(",", ":")))
            return 0 if report.get("ok") else 2
        deadline = time.monotonic() + MAX_WAIT_SECONDS
        def expired(_signum, _frame):
            raise AcceptanceFailure("acceptance_timeout_unknown")
        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, MAX_WAIT_SECONDS)
        report = (run_runtime(args, root, state_path, report, deadline) if args.mode == "runtime"
                  else asyncio.run(run_gateway(args, report, state_path, deadline)))
    except AcceptanceFailure as error:
        report.update(ok=False, phase="stopped_without_retry", failure_code=str(error))
    except Exception:
        # Transport exceptions may contain bearer headers, request text or SDK
        # response bodies. Neither repr nor traceback is suitable for output.
        report.update(ok=False, phase="unknown_without_retry", failure_code="inspection_required")
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    report = plain_report(report)
    if state_path is not None:
        try:
            private_json(state_path, report)
        except Exception:
            report.update(ok=False, phase="unknown_without_retry", failure_code="state_persistence_failed")
    print(json.dumps(report, separators=(",", ":")))
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
