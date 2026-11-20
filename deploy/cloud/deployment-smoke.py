#!/usr/bin/env python3
"""Real HTTPS/tenant/DeepSeek/Office acceptance; consumes real provider tokens.

Use Python 3.11+ with httpx, from a host that can reach the production origin.
--credentials: owner-only 0600 JSON {origin, email[, password]}; password ignored.
--session: owner-only 0600 JSON {origin, cookie}, from a human browser login.
--model-id: actual administrator-configured DeepSeek route (default deepseek-office).
--ca: optional public CA/server PEM for the internal self-signed HTTPS origin.
--report: NEW private JSON evidence file (never overwritten to retry billing).

Exactly one chat/prompt is submitted. Its original Agent may make multiple
metered provider attempts. Only one exact read-only cat command is approved;
unexpected permissions/errors abort the stream. Existing files are untouched.
No local development config, login endpoint, CAPTCHA or database is accessed.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from collections import Counter
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import ssl
import stat
import time
from urllib.parse import urlsplit
import uuid
import xml.etree.ElementTree as ET
import zipfile

import httpx


class SmokeFailure(Exception):
    """Only fixed, safe-to-display codes may cross the command boundary."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise SmokeFailure(code)


def private_json(path: Path, allowed: set[str], required: set[str] | None = None):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                and stat.S_IMODE(info.st_mode) == 0o600 and info.st_size <= 16384,
                "private_input_permissions")
        raw = stream.read(16385)
    require(len(raw) <= 16384, "private_input_too_large")
    data = json.loads(raw)
    required = allowed if required is None else required
    require(isinstance(data, dict) and required <= set(data) <= allowed, "private_input_shape")
    require(all(isinstance(value, str) and value for value in data.values()), "private_input_values")
    return data


def checked_origin(origin: str):
    value = urlsplit(origin)
    require(value.scheme == "https" and value.hostname and not value.username
            and not value.password and value.path == "" and not value.query and not value.fragment
            and not any(ord(c) < 33 for c in origin), "https_exact_origin_required")
    # Reading .port also rejects malformed/out-of-range ports.
    _ = value.port
    return origin


def safe_id(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value), "invalid_server_id")
    return value


def allowed_cat(args, workspace):
    if not isinstance(args, dict) or set(args) - {"command", "cwd", "timeout"}:
        return False
    timeout = args.get("timeout", 10)
    return (args.get("command") == f"cat -- {workspace}/sentinel.txt"
            and args.get("cwd") in (None, "", workspace)
            and isinstance(timeout, (int, float)) and not isinstance(timeout, bool)
            and math.isfinite(timeout) and 0 < timeout <= 15)


def tool_path(args, workspace, expected):
    if not isinstance(args, dict) or not isinstance(args.get("file_path"), str):
        return False
    path = PurePosixPath(args["file_path"])
    if ".." in path.parts:
        return False
    # This acceptance explicitly requires absolute targets, avoiding the
    # original tools' relative suxiaoyou_written destination convention.
    return str(path) == workspace + "/" + expected


def acceptance_rules(workspace):
    rules = [{"permission": "*", "action": "deny"}, {"permission": "bash", "action": "ask"}]
    for tool, filename in (("write", "result.txt"), ("office", "result.docx")):
        # The registry tests resource="*" before exposing a schema. A fallback
        # ask keeps the tool visible; only the exact new target is preapproved.
        rules.append({"permission": tool, "action": "ask"})
        rules.append({"permission": tool, "pattern": workspace + "/" + filename, "action": "allow"})
    return rules


class Acceptance:
    def __init__(self, args, credentials, session, output):
        self.args, self.credentials, self.output = args, credentials, output
        self.cookie = session["cookie"]
        self.csrf = ""
        self.run_id = uuid.uuid4().hex[:16]
        self.workspace = "/tenant/workspaces/deployment-" + self.run_id
        self.marker = "SUXIAOYOU_REAL_DEPLOY_" + secrets.token_hex(16)
        self.stream_id = None
        self.prompt_count = 0
        self.bash_approved = None
        self.calls = {}
        self.results = {}
        self.report = {"kind": "real-production-deployment-acceptance", "run_id": self.run_id,
            "origin": credentials["origin"], "started_at": time.time(), "complete": False,
            "model_id": credentials["model_id"], "prompt_submissions": 0, "workspace": self.workspace,
            "checks": {}, "http": [], "events": [], "event_counts": {}, "limits": {
                "wall_seconds": 300, "prompt_submissions": 1, "exact_cat_approvals": 1,
                "provider_attempts": "not equivalent to prompt count; all attempts are metered"}}

    def save(self):
        # Full evidence is private; additionally redact authentication values
        # even if an unexpected server error included one in an SSE payload.
        encoded = json.dumps(self.report, ensure_ascii=False, indent=2)
        for secret in (self.cookie, self.csrf, self.credentials.get("password", "")):
            if secret:
                encoded = encoded.replace(secret, "[REDACTED]")
        self.output.seek(0)
        self.output.write((encoded + "\n").encode())
        self.output.truncate()
        self.output.flush()
        os.fsync(self.output.fileno())

    async def checked(self, client, method, path, **kwargs):
        require(path.startswith("/") and not path.startswith("//") and "?" not in path, "invalid_local_path")
        response = await client.request(method, path, **kwargs)
        self.report["http"].append({"method": method, "path": path, "status": response.status_code,
                                    "bytes": len(response.content)})
        require(response.is_success, "http_request_failed")
        return response

    async def data(self, client, method, path, **kwargs):
        return (await self.checked(client, method, path, **kwargs)).json()

    async def abort(self, client):
        try:
            async with asyncio.timeout(10):
                # A lost prompt response must never cause another paid POST.
                # The new *conversation* session is known before submission,
                # so find/abort only its job without touching anybody else's.
                if not self.stream_id and self.prompt_count and self.report.get("session_id"):
                    response = await client.get("/api/chat/active", timeout=5)
                    if response.status_code == 200:
                        matches = [row for row in response.json() if row.get("session_id") == self.report["session_id"]]
                        if len(matches) == 1:
                            self.stream_id = safe_id(matches[0].get("stream_id"))
                if not self.stream_id:
                    self.report["abort"] = {"attempted": False, "active_stream_unconfirmed": bool(self.prompt_count)}
                    return
                response = await client.post("/api/chat/abort", json={"stream_id": self.stream_id}, timeout=5)
                self.report["abort"] = {"attempted": True, "http_status": response.status_code}
        except BaseException:
            self.report["abort"] = {"attempted": True, "confirmed": False}

    async def binary(self, client, filename):
        data = await self.data(client, "POST", "/api/files/content-binary", json={
            "path": self.workspace + "/" + filename, "workspace": self.workspace})
        require(isinstance(data.get("content_base64"), str) and len(data["content_base64"]) <= 8_000_000,
                "invalid_binary_response")
        content = base64.b64decode(data["content_base64"], validate=True)
        require(data.get("size") == len(content), "binary_size_mismatch")
        return content

    async def stream(self, client, ids):
        self.stream_id = safe_id(ids.get("stream_id"))
        require(safe_id(ids.get("session_id")) == self.report["session_id"], "prompt_session_mismatch")
        self.report["stream_id"] = self.stream_id
        self.save()
        counts, event, lines = Counter(), "", []
        async with client.stream("GET", "/api/chat/stream/" + self.stream_id) as response:
            require(response.status_code == 200, "stream_http_failed")
            require(response.headers.get("content-type", "").startswith("text/event-stream"), "invalid_stream_type")
            async for line in response.aiter_lines():
                require(len(line) <= 1_048_576, "stream_event_too_large")
                if line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    lines.append(line[5:].lstrip())
                elif line == "" and lines:
                    data = json.loads("\n".join(lines))
                    lines = []
                    counts[event] += 1
                    require(sum(counts.values()) <= 20000, "stream_event_budget")
                    # Text deltas are counted; tool arguments/results/errors are
                    # retained privately to distinguish tool execution from prose.
                    if event not in {"text-delta", "reasoning-delta"}:
                        self.report["events"].append({"event": event, "data": data})
                    self.report["event_counts"] = dict(counts)
                    await self.event(client, event, data)
        require(not lines, "truncated_stream_event")
        require(counts["done"] == 1, "stream_missing_done")
        require(self.bash_approved is not None, "exact_cat_was_not_approved")
        for tool in ("bash", "write", "office"):
            require(any(call["tool"] == tool and call_id in self.results for call_id, call in self.calls.items()),
                    "required_tool_not_completed")
        bash_output = self.results.get(self.bash_approved, {}).get("output", "")
        require(isinstance(bash_output, str) and self.marker in bash_output, "cat_did_not_read_sentinel")
        require(self.results[self.bash_approved].get("metadata", {}).get("exit_code") == 0,
                "cat_did_not_exit_successfully")
        self.report["checks"].update(sse_completed=True, exact_readonly_bash_completed=True,
                                      text_and_office_tools_completed=True)
        self.save()

    async def event(self, client, event, data):
        require(isinstance(data, dict), "invalid_stream_payload")
        if event in {"agent-error", "error", "tool-error", "compaction-error"}:
            raise SmokeFailure("agent_or_tool_error")
        if event in {"question", "plan-review", "task-batch-start", "retry", "desync"}:
            raise SmokeFailure("unexpected_agent_interaction")
        if event == "tool-call":
            tool, args, call_id = data.get("tool"), data.get("arguments"), safe_id(data.get("call_id"))
            require(call_id not in self.calls and len(self.calls) < 12, "unexpected_tool_repetition")
            permitted = ((tool == "bash" and allowed_cat(args, self.workspace))
                         or (tool == "write" and tool_path(args, self.workspace, "result.txt"))
                         or (tool == "office" and tool_path(args, self.workspace, "result.docx")
                             and args.get("operation") == "create")
                         or (tool == "read" and any(tool_path(args, self.workspace, name)
                             for name in ("sentinel.txt", "result.txt"))))
            require(permitted, "unexpected_tool_call")
            if tool == "bash":
                require(call_id == self.bash_approved, "bash_started_without_exact_approval")
            self.calls[call_id] = {"tool": tool}
        elif event == "tool-result":
            call_id = safe_id(data.get("call_id"))
            require(call_id in self.calls and call_id not in self.results, "unmatched_tool_result")
            self.results[call_id] = data
        elif event == "permission-request":
            require(self.bash_approved is None and data.get("tool") == "bash"
                    and data.get("permission") == "bash" and not data.get("arguments_truncated")
                    and allowed_cat(data.get("arguments"), self.workspace), "unexpected_permission_request")
            call_id = safe_id(data.get("tool_call_id"))
            # The original engine publishes permission-request before the
            # post-approval tool-call event. Bind the future call to this ID.
            require(call_id not in self.calls, "bash_permission_after_execution")
            await self.checked(client, "POST", "/api/chat/respond", json={
                "stream_id": self.stream_id, "call_id": safe_id(data.get("call_id")),
                "response": {"allowed": True, "remember": False}})
            self.bash_approved = call_id
            self.save()
        elif event == "done":
            require(data.get("finish_reason") in {"stop", "complete"}, "agent_not_finished_successfully")

    async def office(self, client):
        content = await self.binary(client, "result.docx")
        require(content.startswith(b"PK"), "office_file_not_docx")
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            require(len(archive.infolist()) <= 1000 and sum(i.file_size for i in archive.infolist()) <= 20_000_000,
                    "office_archive_budget")
            xml = ET.fromstring(archive.read("word/document.xml"))
            require(self.marker in "".join(xml.itertext()), "docx_missing_sentinel")
        context = await self.data(client, "GET", "/api/office-v2/context", params={"session_id": self.report["session_id"]})
        identity = {"session_id": self.report["session_id"],
                    "workspace_instance_id": safe_id(context.get("workspace_instance_id")), "relative_path": "result.docx"}
        validation = await self.data(client, "GET", "/api/office-v2/validation", params=identity)
        require(validation.get("status") == "authoritative_pass", "office_not_authoritatively_validated")
        digest = hashlib.sha256(content).hexdigest()
        rendered = await self.data(client, "POST", "/api/office-v2/render", json={**identity, "expected_source_sha256": digest}, timeout=90)
        cache_key = rendered.get("cache_key") or rendered.get("manifest", {}).get("cache_key")
        require(isinstance(cache_key, str) and 1 <= len(cache_key) <= 256, "office_render_cache_missing")
        png = await self.checked(client, "GET", "/api/office-v2/page", params={**identity, "cache_key": cache_key, "page_number": 1})
        require(png.content.startswith(b"\x89PNG\r\n\x1a\n"), "office_preview_not_png")
        self.report["office"] = {"bytes": len(content), "sha256": digest, "validation": validation,
            "render": rendered, "preview_png_bytes": len(png.content), "preview_png_sha256": hashlib.sha256(png.content).hexdigest()}
        self.report["checks"].update(docx_binary_verified=True, office_authoritative_validation=True, office_png_verified=True)
        self.save()

    async def execute(self, client):
        await self.checked(client, "GET", "/livez")
        await self.checked(client, "GET", "/readyz")
        auth = await self.data(client, "GET", "/api/cloud/auth/me")
        user = auth.get("user", {})
        require(user.get("email", "").strip().lower() == self.credentials["email"].strip().lower(), "session_account_mismatch")
        require(user.get("role") == "admin" and user.get("is_active") is True, "active_admin_session_required")
        self.csrf = auth.get("csrf_token", "")
        require(isinstance(self.csrf, str) and 16 <= len(self.csrf) <= 256, "invalid_csrf")
        client.headers["x-csrf-token"] = self.csrf
        self.report["checks"].update(authenticated_human_session=True, admin_role=True, tls_verified=True)
        models = await self.data(client, "GET", "/api/cloud/models")
        selected = [m for m in models if m.get("id") == self.credentials["model_id"] and m.get("provider") == "deepseek"]
        require(len(selected) == 1, "real_deepseek_route_required")
        self.report["model"] = {key: selected[0].get(key) for key in ("id", "provider", "model", "name")}
        self.report["admin_user_count"] = len(await self.data(client, "GET", "/api/cloud/admin/users"))
        self.report["admin_usage_user_count"] = len((await self.data(client, "GET", "/api/cloud/admin/usage"))["users"])
        before = await self.data(client, "GET", "/api/cloud/usage")
        require(before.get("reserved_tokens") == 0, "preexisting_reserved_usage")
        require(before.get("remaining_tokens", 0) >= 50000, "insufficient_acceptance_budget")
        before_ids = {r["id"] for r in await self.data(client, "GET", "/api/cloud/usage/requests")}
        self.report["usage_before"] = before
        runtime = await self.data(client, "POST", "/api/cloud/runtime/start", timeout=130)
        require(runtime.get("ready") is True or runtime.get("status") == "ready", "runtime_not_ready")
        self.report["runtime"] = runtime
        await self.checked(client, "GET", "/health")
        require(not await self.data(client, "GET", "/api/chat/active"), "preexisting_active_generation")
        created = await self.data(client, "POST", "/api/cloud-runtime/files/directories/create",
            json={"parent": "/tenant/workspaces", "name": "deployment-" + self.run_id})
        require(created.get("path") == self.workspace, "unexpected_workspace_path")
        seed = (self.marker + "\n").encode()
        uploaded = await self.data(client, "POST", "/api/cloud-runtime/files/upload", data={"directory": self.workspace},
            files={"file": ("sentinel.txt", seed, "text/plain")})
        require(uploaded.get("content_hash") == hashlib.sha256(seed).hexdigest(), "upload_digest_mismatch")
        require(await self.binary(client, "sentinel.txt") == seed, "upload_download_mismatch")
        conversation = await self.data(client, "POST", "/api/sessions", json={
            "directory": self.workspace, "title": "Deployment acceptance " + self.run_id})
        self.report["session_id"] = safe_id(conversation.get("id"))
        self.report["checks"].update(runtime_ready=True, private_workspace_created=True, upload_download_verified=True)
        self.save()
        command = f"cat -- {self.workspace}/sentinel.txt"
        text = (f"Perform this bounded deployment acceptance in {self.workspace}. Only use bash, write, and office tools. "
            f"First call bash with exactly command={json.dumps(command)}, cwd={json.dumps(self.workspace)}, timeout=10. "
            "A one-time permission will be supplied only for that exact read-only command. "
            "Use its real output; do not guess or invent file contents. "
            f"Then use write with absolute file_path={json.dumps(self.workspace + '/result.txt')} to save exactly the marker read, followed by a newline. "
            f"Then use the real office tool with absolute file_path={json.dumps(self.workspace + '/result.docx')}, operation=create, "
            "to create a short one-page English DOCX, title Cloud Deployment Acceptance, one paragraph containing that exact marker. "
            "Do not use code_execute, shell writes, subagents, memory, network, other files or other tools. "
            "Do not bypass Office precommit validation. If any tool fails, stop immediately without retry. "
            "After all three real tools complete, reply with one short sentence.")
        rules = acceptance_rules(self.workspace)
        require(self.prompt_count == 0, "prompt_already_submitted")
        self.prompt_count += 1
        self.report["prompt_submissions"] = self.prompt_count
        self.report["prompt"] = text
        self.save()  # Persist intent before the only possibly chargeable POST.
        ids = await self.data(client, "POST", "/api/chat/prompt", json={
            "client_request_id": "deployment-" + self.run_id, "text": text,
            "session_id": self.report["session_id"],
            "model": self.credentials["model_id"], "provider_id": "custom_cloud", "agent": "build", "reasoning": False,
            "workspace": self.workspace, "permission_presets": {"file_changes": False, "run_commands": False},
            "permission_rules": rules})
        await self.stream(client, ids)
        require((await self.binary(client, "result.txt")).decode().strip() == self.marker, "agent_text_file_mismatch")
        self.report["checks"]["agent_text_file_verified"] = True
        await self.office(client)
        for _ in range(20):
            after = await self.data(client, "GET", "/api/cloud/usage")
            rows = [r for r in await self.data(client, "GET", "/api/cloud/usage/requests") if r["id"] not in before_ids]
            if rows and after.get("reserved_tokens") == 0 and all(r.get("state") == "settled" for r in rows):
                break
            await asyncio.sleep(1)
        require(after.get("period") == before.get("period"), "billing_period_changed")
        require(rows and after.get("reserved_tokens") == 0, "outstanding_or_missing_usage")
        require(all(r.get("source") == "agent" and r.get("model_id") == self.credentials["model_id"]
                    and r.get("state") == "settled" and r.get("total_tokens", 0) > 0 for r in rows), "inference_not_settled")
        total = sum(r["total_tokens"] for r in rows)
        require(after["used_tokens"] - before["used_tokens"] == total, "usage_ledger_mismatch")
        require(not await self.data(client, "GET", "/api/chat/active"), "generation_still_active")
        self.report.update(usage_after=after, new_receipts=rows, total_tokens=total, provider_attempts=len(rows), complete=True)
        self.report["checks"].update(usage_positive=True, usage_settled=True, reserved_tokens_zero=True, ledger_matches=True)


async def run(args):
    credentials = private_json(args.credentials, {"origin", "email", "password"}, {"origin", "email"})
    credentials["model_id"] = args.model_id
    session = private_json(args.session, {"origin", "cookie"})
    origin = checked_origin(credentials["origin"])
    require(session["origin"] == origin, "session_origin_mismatch")
    require(re.fullmatch(r"[A-Za-z0-9_-]{32,256}", session["cookie"]), "invalid_session_cookie")
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}", credentials["model_id"]), "invalid_model_id")
    context = ssl.create_default_context(cafile=str(args.ca) if args.ca else None)
    fd = os.open(args.report, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w+b") as output:
        acceptance = Acceptance(args, credentials, session, output)
        acceptance.save()
        async with httpx.AsyncClient(base_url=origin, verify=context, timeout=httpx.Timeout(30, connect=10),
                follow_redirects=False, trust_env=False, headers={"Origin": origin, "User-Agent": "Suxiaoyou-Deployment-Acceptance/1"}) as client:
            client.cookies.set("cloud_session", session["cookie"], domain=urlsplit(origin).hostname, path="/")
            try:
                async with asyncio.timeout(300):
                    await acceptance.execute(client)
            except BaseException as exc:
                await acceptance.abort(client)
                acceptance.report["failure"] = exc.code if isinstance(exc, SmokeFailure) else type(exc).__name__
                raise
            finally:
                acceptance.report["finished_at"] = time.time()
                acceptance.save()
        return {"complete": acceptance.report["complete"], "run_id": acceptance.run_id,
            "checks": acceptance.report["checks"], "prompt_submissions": acceptance.prompt_count,
            "provider_attempts": acceptance.report.get("provider_attempts"), "total_tokens": acceptance.report.get("total_tokens")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", required=True, type=Path)
    parser.add_argument("--session", required=True, type=Path)
    parser.add_argument("--model-id", default="deepseek-office")
    parser.add_argument("--ca", type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = asyncio.run(run(args))
    except BaseException as exc:
        # Never include repr/str/tracebacks of an HTTP request, exception or
        # credential-bearing command. Private evidence keeps a safe error code.
        code = exc.code if isinstance(exc, SmokeFailure) else type(exc).__name__
        print(json.dumps({"complete": False, "failure": code}))
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
