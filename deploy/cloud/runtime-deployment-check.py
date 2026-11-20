#!/usr/bin/env python3
"""Real administrative deployment check inside the CURRENT production API.

Run with compose exec -T api python runtime-deployment-check.py after seeding.
This is a supervisor-authenticated service operation, not a browser login test.
It creates no Web AuthSession, calls no login/CAPTCHA endpoint, changes no auth
control, and never exposes a service capability or provider credential.

Exactly one original-Agent prompt may spend real DeepSeek tokens. The request
is bounded to 300 seconds plus at most 10 seconds of abort cleanup, with no
paid retry. A new private report is created under the shared cloud data volume;
an existing report name is refused. A unique acceptance workspace is retained.
Stream/file proof code is shared in source with deployment-smoke.py; this file
is standalone and does not depend on a sibling file in the production image.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from collections import Counter
import hashlib
import io
import json
import logging
import math
import os
from pathlib import Path, PurePosixPath
import re
import secrets
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


class RuntimeAcceptance:
    def __init__(self, settings, model_id, output):
        self.settings, self.model_id, self.output = settings, model_id, output
        self.run_id = uuid.uuid4().hex[:16]
        self.workspace = "/tenant/workspaces/deployment-" + self.run_id
        self.marker = "SUXIAOYOU_REAL_DEPLOY_" + secrets.token_hex(16)
        self.stream_id = None
        self.prompt_count = 0
        self.bash_approved = None
        self.calls, self.results = {}, {}
        self.redactions = set()
        self.report = {"kind": "real-administrative-production-runtime-acceptance",
            "run_id": self.run_id, "started_at": time.time(), "complete": False,
            "web_authentication": "pending_human_browser_login",
            "authorization": "existing administrative supervisor service capability",
            "model_id": model_id, "prompt_submissions": 0, "workspace": self.workspace,
            "checks": {}, "http": [], "events": [], "event_counts": {},
            "limits": {"acceptance_seconds": 300, "abort_cleanup_seconds": 10,
                "prompt_submissions": 1, "exact_cat_approvals": 1,
                "provider_attempts": "Original Agent steps are separately metered; not equivalent to prompt count"}}

    def save(self):
        encoded = json.dumps(self.report, ensure_ascii=False, indent=2)
        for secret in self.redactions:
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

    async def agent(self, client):
        await self.checked(client, "GET", "/livez")
        await self.checked(client, "GET", "/health")
        require(not await self.data(client, "GET", "/api/chat/active"), "preexisting_active_generation")
        context = await self.data(client, "GET", "/api/cloud-runtime/files/context")
        require(context.get("workspace_root") == "/tenant/workspaces"
                and context.get("office_cloud", {}).get("ready") is True, "tenant_office_not_ready")
        created = await self.data(client, "POST", "/api/cloud-runtime/files/directories/create", json={
            "parent": "/tenant/workspaces", "name": "deployment-" + self.run_id})
        require(created.get("path") == self.workspace, "unexpected_workspace_path")
        seed = (self.marker + "\n").encode()
        uploaded = await self.data(client, "POST", "/api/cloud-runtime/files/upload", data={"directory": self.workspace},
            files={"file": ("sentinel.txt", seed, "text/plain")})
        require(uploaded.get("content_hash") == hashlib.sha256(seed).hexdigest(), "upload_digest_mismatch")
        require(await self.binary(client, "sentinel.txt") == seed, "upload_download_mismatch")
        conversation = await self.data(client, "POST", "/api/sessions", json={
            "directory": self.workspace, "title": "Deployment acceptance " + self.run_id})
        self.report["session_id"] = safe_id(conversation.get("id"))
        self.report["checks"].update(engine_health=True, tenant_office_ready=True,
            private_workspace_created=True, upload_download_verified=True)
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
        require(self.prompt_count == 0, "prompt_already_submitted")
        self.prompt_count += 1
        self.report["prompt_submissions"] = self.prompt_count
        self.report["prompt"] = text
        self.save()
        ids = await self.data(client, "POST", "/api/chat/prompt", json={
            "client_request_id": "deployment-" + self.run_id, "session_id": self.report["session_id"],
            "text": text, "model": self.model_id, "provider_id": "custom_cloud", "agent": "build", "reasoning": False,
            "workspace": self.workspace, "permission_presets": {"file_changes": False, "run_commands": False},
            "permission_rules": acceptance_rules(self.workspace)})
        await self.stream(client, ids)
        require((await self.binary(client, "result.txt")).decode().strip() == self.marker, "agent_text_file_mismatch")
        self.report["checks"]["agent_text_file_verified"] = True
        await self.office(client)
        require(not await self.data(client, "GET", "/api/chat/active"), "generation_still_active")


async def usage_snapshot(database, user_id):
    from sqlalchemy import func, select
    from app.cloud.metering import usage_summary
    from app.cloud.models import AuthSession, RequestRecord

    async with database.sessions() as session:
        usage = await usage_summary(session, user_id)
        rows = (await session.scalars(select(RequestRecord).where(RequestRecord.user_id == user_id)
            .order_by(RequestRecord.created_at.desc()).limit(200))).all()
        records = [{name: getattr(row, name) for name in (
            "id", "model_id", "source", "state", "reserved_tokens", "total_tokens", "cost_microusd")} for row in rows]
        sessions = await session.scalar(select(func.count()).select_from(AuthSession))
    return {"usage": usage, "records": records, "web_auth_sessions": sessions}


async def inspect_runtime(proof, supervisor, user_id):
    record = await supervisor.record(user_id)
    require(record is not None and record.state == "running", "runtime_record_not_running")
    info = await supervisor._inspect(record)
    require(isinstance(info, dict) and info.get("State", {}).get("Running") is True, "runtime_container_not_running")
    host, config = info.get("HostConfig", {}), info.get("Config", {})
    owner = hashlib.sha256(user_id.encode()).hexdigest()
    network = "sxy-net-" + owner[:32]
    labels = config.get("Labels") or {}
    require(config.get("User") == "10001:10001" and host.get("ReadonlyRootfs") is True
            and host.get("Privileged") is False and "ALL" in host.get("CapDrop", [])
            and "no-new-privileges:true" in host.get("SecurityOpt", [])
            and host.get("Memory", 0) > 0 and host.get("NanoCpus", 0) > 0 and host.get("PidsLimit", 0) > 0,
            "runtime_resource_or_security_contract")
    require(labels.get("io.suxiaoyou.cloud.owner") == owner
            and labels.get("io.suxiaoyou.cloud.generation") == str(record.generation)
            and host.get("NetworkMode") == network, "runtime_owner_or_network_contract")
    network_info = json.loads(await supervisor._docker("network", "inspect", network))[0]
    members = {item.get("Name") for item in network_info.get("Containers", {}).values()}
    require((network_info.get("Labels") or {}).get("io.suxiaoyou.cloud.owner") == owner
            and members == {record.container_name, proof.control_container}, "runtime_network_membership_contract")
    require(not any(bindings for bindings in info.get("NetworkSettings", {}).get("Ports", {}).values()),
            "runtime_published_host_port")
    mounts = info.get("Mounts", [])
    require(len(mounts) == 1 and mounts[0].get("Type") == "volume" and mounts[0].get("Destination") == "/tenant"
            and mounts[0].get("Name") == "sxy-data-" + owner[:32], "runtime_mount_contract")
    volume = json.loads(await supervisor._docker("volume", "inspect", mounts[0]["Name"]))[0]
    require((volume.get("Labels") or {}).get("io.suxiaoyou.cloud.owner") == owner, "runtime_volume_owner_contract")
    daemon = json.loads(await supervisor._docker("info", "--format", "{{json .}}"))
    require(any("rootless" in item for item in daemon.get("SecurityOptions", []))
            and str(daemon.get("CgroupVersion")) == "2" and daemon.get("CgroupDriver") == "systemd",
            "runtime_daemon_not_rootless_cgroup2")
    proof.report["runtime_resources"] = {"memory_bytes": host["Memory"], "nano_cpus": host["NanoCpus"],
        "pids_limit": host["PidsLimit"], "container_user": config["User"], "private_network": True,
        "host_ports": 0, "mount_count": len(mounts), "generation": record.generation}
    proof.report["checks"].update(runtime_nonroot=True, runtime_readonly=True, runtime_capabilities_dropped=True,
        runtime_no_host_ports=True, runtime_private_volume_and_network=True, runtime_resources_bounded=True,
        docker_rootless_cgroup2=True)
    proof.save()


def new_report(settings, name):
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}\.json", name), "invalid_report_name")
    root = Path(os.path.abspath(settings.data_dir))
    metadata = root.lstat()
    require(stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == os.geteuid()
            and not stat.S_IMODE(metadata.st_mode) & 0o022, "unsafe_cloud_data_directory")
    directory = root / "deployment-checks"
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    metadata = directory.lstat()
    require(stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == os.geteuid()
            and stat.S_IMODE(metadata.st_mode) == 0o700, "unsafe_report_directory")
    fd = os.open(directory / name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "w+b")


async def run(args):
    from sqlalchemy import select
    from app.cloud.config import CloudSettings
    from app.cloud.database import Database
    from app.cloud.models import ModelRoute, User
    from app.cloud.office_gateway import OfficeGateway
    from app.cloud.runtime import RuntimeSupervisor

    os.umask(0o077)
    require(os.getuid() == 10001 and os.geteuid() == 10001, "run_inside_current_api_as_service_uid")
    settings = CloudSettings()
    require(settings.environment == "production" and settings.cookie_secure is True and not settings.allow_mock_provider
            and not settings.auto_migrate and settings.full_runtime_enabled and settings.runtime_network_mode
            and settings.database_url.startswith("postgresql+asyncpg://"), "real_production_postgresql_required")
    checked_origin(settings.allowed_origin)
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}", args.model_id), "invalid_model_id")
    database = Database(settings)
    try:
        await database.initialize()
        async with database.sessions() as session:
            users = (await session.scalars(select(User).limit(2))).all()
            require(len(users) == 1 and users[0].role == "admin" and users[0].is_active is True,
                    "fresh_deployment_with_one_active_admin_required")
            user_id = users[0].id
            route = await session.get(ModelRoute, args.model_id)
            require(route is not None and route.enabled and route.kind == "chat" and route.provider == "deepseek"
                    and route.owner_user_id in (None, user_id), "enabled_real_deepseek_route_required")
        with new_report(settings, args.report_name) as output:
            proof = RuntimeAcceptance(settings, args.model_id, output)
            proof.report["checks"].update(production_flags=True, postgresql_schema_ready=True,
                unique_active_admin=True, enabled_real_deepseek_route=True)
            proof.save()
            supervisor = RuntimeSupervisor(database, settings)
            supervisor.office_gateway = OfficeGateway(settings)
            client = None
            try:
                async with asyncio.timeout(300):
                    control = json.loads(await supervisor._docker("container", "inspect", os.uname().nodename))[0]
                    labels = control.get("Config", {}).get("Labels") or {}
                    require(labels.get("com.docker.compose.project") == "suxiaoyou-cloud"
                            and labels.get("com.docker.compose.service") == "api"
                            and str(labels.get("com.docker.compose.oneoff", "")).lower() == "false"
                            and control.get("State", {}).get("Running") is True, "current_stable_api_container_required")
                    proof.control_container = control.get("Name", "").lstrip("/")
                    require(bool(proof.control_container), "current_api_container_name_missing")
                    async with httpx.AsyncClient(trust_env=False, timeout=10) as local:
                        ready = await local.get("http://127.0.0.1:8000/readyz")
                        require(ready.status_code == 200 and ready.json().get("mode") == "cloud", "current_api_listener_not_ready")
                    before = await usage_snapshot(database, user_id)
                    require(before["usage"].get("reserved_tokens") == 0 and before["usage"].get("remaining_tokens", 0) >= 50000,
                            "preexisting_usage_or_insufficient_acceptance_budget")
                    before_ids = {row["id"] for row in before["records"]}
                    proof.report["usage_before"] = before
                    descriptor = await supervisor.office_gateway.descriptor()
                    payload = descriptor.get("payload", {})
                    require(payload.get("purpose") == "descriptor" and payload.get("sandbox") == {
                        "uid": 10001, "root_read_only": True, "network_denied": True}, "office_golden_isolation_probe_failed")
                    proof.report["office_descriptor"] = descriptor
                    proof.report["checks"].update(current_api_listener=True, office_golden_probe=True)
                    status = await supervisor.start(user_id)
                    require(status.get("ready") is True and status.get("status") == "ready", "runtime_not_ready")
                    proof.report["checks"]["runtime_ready"] = True
                    await inspect_runtime(proof, supervisor, user_id)
                    base, engine_token = await supervisor.endpoint(user_id)
                    parsed = urlsplit(base)
                    require(parsed.scheme == "http" and parsed.hostname and parsed.hostname.startswith("sxy-user-")
                            and parsed.port == 8080 and not parsed.username and not parsed.password,
                            "unexpected_private_engine_endpoint")
                    proof.redactions.add(engine_token)
                    client = httpx.AsyncClient(base_url=base, trust_env=False, follow_redirects=False,
                        timeout=httpx.Timeout(30, connect=10), headers={"Authorization": "Bearer " + engine_token,
                        "User-Agent": "Suxiaoyou-Administrative-Deployment-Check/1"})
                    await proof.agent(client)
                    for _ in range(20):
                        after = await usage_snapshot(database, user_id)
                        rows = [row for row in after["records"] if row["id"] not in before_ids]
                        if rows and after["usage"]["reserved_tokens"] == 0 and all(row["state"] == "settled" for row in rows):
                            break
                        await asyncio.sleep(1)
                    require(after["usage"]["period"] == before["usage"]["period"], "billing_period_changed")
                    require(rows and after["usage"]["reserved_tokens"] == 0 and all(row["state"] == "settled"
                            and row["source"] == "agent" and row["model_id"] == args.model_id and row["total_tokens"] > 0 for row in rows),
                            "inference_not_completely_settled")
                    total = sum(row["total_tokens"] for row in rows)
                    require(after["usage"]["used_tokens"] - before["usage"]["used_tokens"] == total, "usage_ledger_mismatch")
                    require(after["web_auth_sessions"] == before["web_auth_sessions"], "web_session_count_changed")
                    proof.report.update(usage_after=after, new_receipts=rows, provider_attempts=len(rows), total_tokens=total, complete=True)
                    proof.report["checks"].update(usage_positive=True, usage_settled=True, reserved_tokens_zero=True,
                        ledger_matches=True, web_auth_session_count_unchanged=True)
            except BaseException as exc:
                if client is not None:
                    await proof.abort(client)
                proof.report["failure"] = exc.code if isinstance(exc, SmokeFailure) else type(exc).__name__
                raise
            finally:
                if client is not None:
                    await client.aclose()
                proof.report["finished_at"] = time.time()
                proof.save()
            return {"complete": proof.report["complete"], "run_id": proof.run_id,
                "checks": proof.report["checks"], "web_authentication": proof.report["web_authentication"],
                "prompt_submissions": proof.prompt_count, "provider_attempts": proof.report.get("provider_attempts"),
                "total_tokens": proof.report.get("total_tokens"), "test_files": ["sentinel.txt", "result.txt", "result.docx"],
                "private_report_name": args.report_name}
    finally:
        await database.close()


def main():
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-id", default="deepseek-office")
    parser.add_argument("--report-name", default="deployment-runtime-check.json")
    args = parser.parse_args()
    try:
        result = asyncio.run(run(args))
    except BaseException as exc:
        code = exc.code if isinstance(exc, SmokeFailure) else type(exc).__name__
        print(json.dumps({"complete": False, "failure": code, "web_authentication": "pending_human_browser_login"}))
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

