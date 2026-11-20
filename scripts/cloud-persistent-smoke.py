#!/usr/bin/env python3
"""Real Goal/task-batch/scheduler/MCP acceptance on a dedicated cloud account.

No fixtures. --phase prepare installs the official npm filesystem MCP package
only in the new account's HOME and restarts only that private engine. Other
phases call the real DeepSeek route and consume paid Tokens. Credentials and
full event evidence remain in private local files; public output is sanitized.
Actual cumulative consumption is limited to the internal 300,000 Token acceptance budget.
The monthly account admission quota is independently operator-configured; this script never raises it.
"""
from __future__ import annotations

import argparse
import asyncio
from cloud_login import HumanCaptchaRequired, login as human_login
import base64
from collections import Counter
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import subprocess
import time
from urllib.parse import quote
import uuid

import httpx

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("cloud_dev", ROOT / "scripts/cloud-dev.py")
dev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dev)
PUBLIC = ROOT.parent / "assessment/parity-persistent-validation.json"


def private(path, value):
    dev.write_private(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


class Failure(RuntimeError):
    pass


async def checked(client, method, path, **kwargs):
    response = await client.request(method, path, **kwargs)
    if not response.is_success:
        try:
            detail = response.json().get("detail")
            code = detail.get("code") if isinstance(detail, dict) else "http_error"
        except (ValueError, AttributeError):
            code = "invalid_response"
        raise Failure(f"{method} {path.split('?')[0]} HTTP {response.status_code} ({code})")
    return response.json()


async def login(client, account, *, session_file=None):
    auth = await human_login(client, account["email"], account["password"], session_file=session_file)
    client.headers["x-csrf-token"] = auth["csrf_token"]
    return auth["user"]


async def receipts(client):
    rows = await checked(client, "GET", "/api/cloud/usage/requests")
    usage = await checked(client, "GET", "/api/cloud/usage")
    return {"total_tokens": usage["used_tokens"], "reserved_tokens": usage["reserved_tokens"],
            "states": dict(Counter(row["state"] for row in rows)), "requests": len(rows),
            "sources": dict(Counter(row["source"] for row in rows)),
            "request_ids": [row["id"] for row in rows]}


async def guard(client):
    totals = await receipts(client)
    if totals["total_tokens"] >= 250_000:
        raise Failure("Actual Token consumption near 300,000 safety budget; further paid phases refused")
    return totals


def safe_arguments(args, workspace):
    for key, value in args.items():
        if key in {"path", "file_path", "directory", "source", "destination"} and isinstance(value, str):
            path = PurePosixPath(value)
            if ".." in path.parts or (path.is_absolute() and not path.is_relative_to(workspace)):
                return False
    return True


async def stream(client, ids, account, *, pause_first_step=False):
    events, tools, errors, permissions, batches, text = Counter(), [], [], [], [], []
    raw = []
    event = ""
    paused = None
    async with client.stream("GET", "/api/chat/stream/" + ids["stream_id"], timeout=300) as response:
        if response.status_code != 200:
            raise Failure(f"SSE HTTP {response.status_code}")
        async for line in response.aiter_lines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
                events[event] += 1
                raw.append({"event": event, "data": data})
                if event == "step-start" and pause_first_step and paused is None:
                    path = "/api/sessions/" + ids["session_id"] + "/goal"
                    current = await checked(client, "GET", path)
                    paused = await checked(client, "POST", path + "/pause", json={"client_request_id": "safe-pause-" + uuid.uuid4().hex, "expected_revision": current["revision"]})
                if event == "tool-call":
                    tools.append(data.get("tool"))
                    print("Actual tool:", data.get("tool"), flush=True)
                elif event in {"agent-error", "tool-error", "error"}:
                    errors.append({"event": event, "code": data.get("error_type") or data.get("code"),
                                   "message": data.get("error") or data.get("error_message")})
                    await client.post("/api/chat/abort", json={"stream_id": ids["stream_id"]})
                elif event == "permission-request":
                    tool = data.get("tool", "")
                    permitted = tool in {"write", "edit", "apply_patch", "read", "get_goal", "update_goal"} or tool.startswith(account["connector_id"] + "_")
                    permitted = permitted and safe_arguments(data.get("arguments", {}), account["workspace"])
                    await checked(client, "POST", "/api/chat/respond", json={"stream_id": ids["stream_id"], "call_id": data["call_id"], "response": {"allowed": permitted, "remember": False}})
                    permissions.append({"tool": tool, "allowed": permitted})
                elif event.startswith("task-batch"):
                    batches.append(data)
                elif event == "text-delta":
                    text.append(data.get("text") or data.get("delta") or "")
    return {**ids, "events": dict(events), "tools": tools, "errors": errors, "permissions": permissions,
            "batches": batches, "text": "".join(text), "raw_events": raw, "paused": paused}


async def prompt(client, account, text, *, agent="build"):
    await guard(client)
    ids = await checked(client, "POST", "/api/chat/prompt", json={"client_request_id": "persistent-" + uuid.uuid4().hex,
        "text": text, "model": "deepseek-office", "provider_id": "custom_cloud", "agent": agent,
        "reasoning": False, "workspace": account["workspace"],
        "permission_presets": {"file_changes": True, "run_commands": False}})
    return await stream(client, ids, account)


async def content(client, account, name):
    return await checked(client, "POST", "/api/files/content", json={"path": account["workspace"] + "/" + name, "workspace": account["workspace"]})


async def prepare(client, account):
    await checked(client, "POST", "/api/cloud/runtime/start")
    directory = await client.post("/api/cloud-runtime/files/directories/create", json={"parent": "/tenant/workspaces", "name": account["workspace"].rsplit("/", 1)[1]})
    if directory.status_code not in {200, 409}:
        raise Failure("Private acceptance directory could not be created")
    await client.post("/api/cloud-runtime/files/upload", data={"directory": account["workspace"]},
        files={"file": ("sentinel.txt", ("PRIVATE_MCP_" + account["marker"] + "\n2,3,5\n").encode(), "text/plain")})
    container = "sxy-user-" + hashlib.sha256(account["user_id"].encode()).hexdigest()[:32]
    # Only the new user's container/HOME is changed. Never copy host config or
    # inherit provider/cloud capability environment into npm install scripts.
    program = r'''
import json, os, pathlib, re, subprocess
settings = json.loads(__import__('sys').argv[1])
home = pathlib.Path('/tenant/home')
prefix = home / '.suxiaoyou/parity-mcp'
prefix.mkdir(parents=True, exist_ok=True)
env = {k:v for k,v in os.environ.items() if k in {'PATH','HOME','TMPDIR','XDG_CACHE_HOME','XDG_CONFIG_HOME','XDG_DATA_HOME','LANG'}}
version = subprocess.check_output(['npm','view','@modelcontextprotocol/server-filesystem','version','--json'], env=env, text=True).strip().strip('"')
if not re.fullmatch(r'[0-9]+[.][0-9]+[.][0-9]+',version): raise SystemExit('Unexpected npm version')
subprocess.run(['npm','install','--prefix',str(prefix),'--ignore-scripts','--no-audit','--no-fund','--save-exact','@modelcontextprotocol/server-filesystem@'+version], env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
plugin = home / '.suxiaoyou/plugins' / settings['plugin']
(plugin/'.claude-plugin').mkdir(parents=True, exist_ok=True)
(plugin/'skills/sentinel').mkdir(parents=True, exist_ok=True)
(plugin/'.claude-plugin/plugin.json').write_text(json.dumps({'name':settings['plugin'],'version':'1.0.0','description':'Private acceptance plugin','author':{'name':'Acceptance test'}}))
(plugin/'skills/sentinel/SKILL.md').write_text('---\\nname: sentinel\\ndescription: Read the private acceptance sentinel.\\n---\\nRead sentinel.txt only inside the explicit acceptance workspace.\\n'.replace('\\n','\n'))
command = ['/usr/local/bin/node',str(prefix/'node_modules/@modelcontextprotocol/server-filesystem/dist/index.js'),settings['workspace']]
(plugin/'.mcp.json').write_text(json.dumps({'mcpServers':{settings['connector_id']:{'type':'local','command':command,'cwd':settings['workspace'],'enabled':False,'environment':{'HOME':str(home),'NODE_ENV':'production'}}}}))
print(json.dumps({'npm_package':'@modelcontextprotocol/server-filesystem','version':version,'command':command,'home':str(home)}))
'''
    docker_env = dict(os.environ)
    configured_daemon = dev.configuration().get("runtime_docker_host")
    if configured_daemon:
        docker_env["DOCKER_HOST"] = configured_daemon
    run = await asyncio.create_subprocess_exec("docker", "exec", "--user", "10001:10001", container, "python", "-c", program,
        json.dumps({key: account[key] for key in ("plugin", "connector_id", "workspace")}), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=docker_env)
    output, _ = await run.communicate()
    if run.returncode:
        private(Path(dev.configuration()["data_dir"]) / "persistent-installer-error.json", {"return_code": run.returncode, "stderr": _.decode(errors="replace"), "stdout": output.decode(errors="replace")})
        raise Failure("Private npm/plugin installation failed; diagnosis saved privately")
    installed = json.loads(output)
    await checked(client, "POST", "/api/cloud/runtime/restart")
    return {**installed, "dedicated_runtime_restarted": True}


async def plugins(client, account):
    name = account["plugin"]
    status = await checked(client, "GET", "/api/plugins/" + name)
    if status["enabled"]:
        await checked(client, "POST", "/api/plugins/" + name + "/disable")
    initial = await checked(client, "GET", "/api/skills")
    await checked(client, "POST", "/api/plugins/" + name + "/enable")
    enabled = await checked(client, "GET", "/api/skills")
    skill = next(row for row in enabled if row["name"].startswith(name + ":"))
    detail = await checked(client, "GET", "/api/skills/" + quote(skill["name"], safe=""))
    await checked(client, "POST", "/api/skills/" + quote(skill["name"], safe="") + "/disable")
    disabled = await checked(client, "GET", "/api/skills")
    assert next(row for row in disabled if row["name"] == skill["name"])["enabled"] is False
    await checked(client, "POST", "/api/skills/" + quote(skill["name"], safe="") + "/enable")
    await checked(client, "POST", "/api/plugins/" + name + "/disable")
    final = await checked(client, "GET", "/api/skills")
    assert not any(row["name"].startswith(name + ":") for row in initial + final)
    await checked(client, "POST", "/api/plugins/" + name + "/enable")
    return {"plugin": name, "source": status["source"], "skill": skill["name"], "read_content": "sentinel.txt" in detail["content"],
            "plugin_enable_adds_skill": True, "plugin_disable_removes_skill": True, "skill_disable_enable_verified": True}


async def mcp(client, account):
    name = account["connector_id"]
    await checked(client, "POST", "/api/connectors/" + name + "/enable")
    detail = await checked(client, "GET", "/api/connectors/" + name)
    approval = detail["local_approval"]
    expected = ["/usr/local/bin/node", "/tenant/home/.suxiaoyou/parity-mcp/node_modules/@modelcontextprotocol/server-filesystem/dist/index.js", account["workspace"]]
    assert approval["command"] == expected and approval["cwd"] == account["workspace"]
    # Reject unconfirmed and stale fingerprints before explicitly approving
    # exactly the just-reviewed Node binary/argv/cwd/environment fingerprint.
    unconfirmed = await checked(client, "POST", "/api/connectors/" + name + "/approve-local-startup", json={"fingerprint": approval["fingerprint"], "confirmed": False})
    stale = await checked(client, "POST", "/api/connectors/" + name + "/approve-local-startup", json={"fingerprint": "sha256:" + "0" * 64, "confirmed": True})
    approved = await checked(client, "POST", "/api/connectors/" + name + "/approve-local-startup", json={"fingerprint": approval["fingerprint"], "confirmed": True})
    detail = await checked(client, "GET", "/api/connectors/" + name)
    assert unconfirmed.get("error_code") == "explicit_confirmation_required" and stale.get("error_code") == "local_command_changed"
    assert approved["success"] and detail["connected"] and detail["tools_count"] > 0
    if account.get("_connect_only"):
        return {"explicit_confirmation_rejected": True, "changed_fingerprint_rejected": True, "fingerprint": approval["fingerprint"], "connected": True, "tools_count": detail["tools_count"]}
    tool_prefix = name
    available = await checked(client, "GET", "/api/tools")
    reader = next(row["id"] for row in available if row["id"].startswith(tool_prefix + "_") and row["id"].endswith("_read_text_file"))
    run = await prompt(client, account, f"仅使用 MCP 工具 {reader} 读取 {account['workspace']}/sentinel.txt，不使用内置read/Bash，不写文件，回复首行标记。")
    assert any(str(tool).startswith(tool_prefix + "_") for tool in run["tools"])
    assert "PRIVATE_MCP_" + account["marker"] in run["text"] and not run["errors"]
    return {"explicit_confirmation_rejected": True, "changed_fingerprint_rejected": True, "fingerprint": approval["fingerprint"],
            "connected": True, "tools_count": detail["tools_count"], "fresh_tool_permission": run["permissions"], "actual_agent_call": run}


async def mcp_connect(client, account):
    return await mcp(client, {**account, "_connect_only": True})


async def goal(client, account):
    await guard(client)
    marker = "PRIVATE_MCP_" + account["marker"]
    started = await checked(client, "POST", "/api/chat/goal", json={"client_request_id": "initial-" + uuid.uuid4().hex,
        "objective": f"只读验证当前工作区sentinel.txt第一行是{marker}。第一轮并行调用get_goal和read读取sentinel.txt；随后调用update_goal(status=complete,evidence引用sentinel.txt、criterion覆盖完成条件、expected_revision使用get_goal返回值)。不写文件，不生成待办，不调用其他工具，不长篇说明。",
        "definition_of_done": "sentinel.txt first line equals " + marker, "token_budget": 65000, "max_continuations": 0,
        "model": "deepseek-office", "provider_id": "custom_cloud", "reasoning": False, "workspace": account["workspace"],
        "permission_presets": {"file_changes": True, "run_commands": False}})
    path = "/api/sessions/" + started["session_id"] + "/goal"
    initial = await stream(client, started, account, pause_first_step=True)
    paused = await checked(client, "GET", path)
    private(Path(dev.configuration()["data_dir"]) / "persistent-goal-initial.json", {"started": started, "paused": paused, "run": initial})
    if paused["status"] != "paused":
        raise Failure("Goal failed to reach the explicit safe pause boundary")
    resumed = await checked(client, "POST", path + "/resume", json={"client_request_id": "resume-" + uuid.uuid4().hex, "expected_revision": paused["revision"]})
    run = await stream(client, resumed, account)
    final = await checked(client, "GET", path)
    private(Path(dev.configuration()["data_dir"]) / "persistent-goal-attempt.json", {"created": started["goal"], "paused": paused, "resumed": resumed, "final": final, "initial": initial, "run": run})
    if final["status"] != "complete":
        raise Failure("Real Goal did not complete: " + str(final.get("blocker_code") or final["status"]))
    file = await content(client, account, "sentinel.txt")
    assert marker in file["content"] and not run["errors"]
    return {"created": started["goal"], "paused": paused, "resumed": resumed["goal"], "final": final, "file_verified": True, "actual_agent_call": run}


async def goal_resume(client, account):
    await guard(client)
    prior = json.loads((Path(dev.configuration()["data_dir"]) / "persistent-goal-initial.json").read_text())
    path = "/api/sessions/" + prior["started"]["session_id"] + "/goal"
    paused = await checked(client, "GET", path)
    if paused["status"] != "paused":
        raise Failure("Saved Goal is not paused; explicit review is required")
    resumed = await checked(client, "POST", path + "/resume", json={"client_request_id": "resume-fixed-" + uuid.uuid4().hex, "expected_revision": paused["revision"]})
    run = await stream(client, resumed, account)
    final = await checked(client, "GET", path)
    result = {"created": prior["started"]["goal"], "paused": paused, "resumed": resumed["goal"], "final": final, "actual_agent_call": run}
    private(Path(dev.configuration()["data_dir"]) / "persistent-goal-resume-attempt.json", result)
    if final["status"] != "complete":
        raise Failure("Resumed real Goal did not complete: " + str(final.get("blocker_code") or final["status"]))
    file = await content(client, account, "sentinel.txt")
    assert "PRIVATE_MCP_" + account["marker"] in file["content"] and not run["errors"]
    return {**result, "file_verified": True}


async def batch(client, account):
    await guard(client)
    result = await checked(client, "POST", "/api/chat/task-batch", json={"mode": "parallel", "workspace": account["workspace"],
        "permission_presets": {"file_changes": False, "run_commands": False},
        "tasks": [{"title": "Read private sentinel " + str(index), "prompt": "实际read读取sentinel.txt，只回复第一行标记，不调用其他工具。", "agent": "explore", "model": "deepseek-office", "provider_id": "custom_cloud"} for index in range(2)]})
    run = await stream(client, result, account)
    private(Path(dev.configuration()["data_dir"]) / "persistent-batch-attempt.json", run)
    children = run["batches"][-1]["tasks"] if run["batches"] else []
    if len(children) != 2 or any(child["status"] != "completed" for child in children):
        raise Failure("Actual task-batch has incomplete or failed child Agents")
    assert not run["errors"]
    proofs = []
    for child in children:
        messages = await checked(client, "GET", "/api/messages/" + child["session_id"])
        encoded = json.dumps(messages, ensure_ascii=False)
        assert "PRIVATE_MCP_" + account["marker"] in encoded
        proofs.append({"session_id": child["session_id"], "marker_in_persisted_messages": True})
    container = "sxy-user-" + hashlib.sha256(account["user_id"].encode()).hexdigest()[:32]
    program = """
import json, sqlite3, sys
sessions = json.loads(sys.argv[1])
db = sqlite3.connect('file:/tenant/state/data/suxiaoyou.db?mode=ro', uri=True)
db.row_factory = sqlite3.Row
rows = db.execute('SELECT id,session_id,workspace_instance_id,root_turn_id,parent_turn_id,depth,status,time_finished FROM turn_run WHERE session_id IN ('+','.join('?' for _ in sessions)+')', sessions).fetchall()
print(json.dumps([dict(row) for row in rows]))
"""
    docker_env = dict(os.environ)
    docker_env["DOCKER_HOST"] = dev.configuration()["runtime_docker_host"]
    process = await asyncio.create_subprocess_exec("docker", "exec", "--user", "10001:10001", container,
        "python", "-c", program, json.dumps([run["session_id"], *[child["session_id"] for child in children]]),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=docker_env)
    output, _ = await process.communicate()
    if process.returncode:
        raise Failure("Read-only task-batch checkpoint ledger proof unavailable")
    turns = json.loads(output)
    root = next(row for row in turns if row["session_id"] == run["session_id"])
    child_turns = [row for row in turns if row["session_id"] != run["session_id"]]
    assert root["status"] == "completed" and root["parent_turn_id"] is None and root["time_finished"]
    assert len(child_turns) == 2 and all(row["status"] == "completed" and row["parent_turn_id"] == root["id"] and row["root_turn_id"] == root["id"] and row["workspace_instance_id"] == root["workspace_instance_id"] and row["time_finished"] for row in child_turns)
    return {"children": proofs, "mode": "parallel", "durable_turns": turns, "actual_agent_call": run}


async def automation(client, account):
    await guard(client)
    task = await checked(client, "POST", "/api/automations", json={"name": "Private scheduled acceptance " + account["marker"],
        "prompt": "实际read读取sentinel.txt，只回复第一行标记。不要写文件、联网、MCP或其他操作。",
        "schedule_config": {"type": "interval", "minutes": 1}, "model": "deepseek-office", "agent": "explore", "workspace": account["workspace"], "timeout_seconds": 150})
    path = "/api/automations/" + task["id"]
    try:
        await checked(client, "POST", path + "/run")
        deadline = time.monotonic() + 220
        while time.monotonic() < deadline:
            history = await checked(client, "GET", path + "/runs")
            if len(history) >= 2:
                await checked(client, "PATCH", path, json={"enabled": False})
            if {row["triggered_by"] for row in history if row["status"] == "success"} >= {"manual", "schedule"}:
                proofs = []
                for row in history:
                    encoded = json.dumps(await checked(client, "GET", "/api/messages/" + row["session_id"]), ensure_ascii=False)
                    assert "PRIVATE_MCP_" + account["marker"] in encoded
                    proofs.append({**row, "marker_in_persisted_messages": True})
                return {"task_id": task["id"], "no_browser_or_sse_attached": True, "history": proofs, "disabled_after_probe": True}
            if any(row["status"] in {"error", "timeout"} for row in history):
                raise Failure("Actual automation history contains a failed run")
            await asyncio.sleep(5)
        raise Failure("Scheduled independent execution did not finish before deadline")
    finally:
        await checked(client, "PATCH", path, json={"enabled": False})


async def memory(client, account):
    await guard(client)
    path = "/api/workspace-memory?workspace_path=" + quote(account["workspace"], safe="")
    previous = await checked(client, "GET", path)
    before = await receipts(client)
    response = await checked(client, "POST", "/api/workspace-memory/refresh?workspace_path=" + quote(account["workspace"], safe=""))
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        current = await checked(client, "GET", path)
        after = await receipts(client)
        if current.get("content") and (current.get("time_updated") != previous.get("time_updated") or after["total_tokens"] > before["total_tokens"]):
            exported = await checked(client, "POST", "/api/workspace-memory/export?workspace_path=" + quote(account["workspace"], safe=""))
            binary = await checked(client, "POST", "/api/files/content-binary", json={"path": exported["exported_to"], "workspace": account["workspace"]})
            data = base64.b64decode(binary["content_base64"])
            assert data.decode() == current["content"]
            return {"queued_session_id": response["session_id"], "time_updated": current["time_updated"], "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "authenticated_download_matches": True, "new_settled_refresh_receipts": [item for item in after["request_ids"] if item not in before["request_ids"]]}
        await asyncio.sleep(3)
    raise Failure("Real memory refresh did not produce updated content")


async def run(args):
    config = dev.configuration()
    folder = Path(config["data_dir"])
    account_path = folder / "persistent-parity-account.json"
    evidence_path = folder / "last-persistent-parity-smoke.json"
    evidence = json.loads(evidence_path.read_text()) if evidence_path.exists() else {"kind": "real-private-runtime-deepseek-persistent-capabilities", "phases": {}}
    timeout = httpx.Timeout(300, connect=10)
    base = f"http://127.0.0.1:{config['api_port']}"
    if account_path.exists():
        account = json.loads(account_path.read_text())
    else:
        async with httpx.AsyncClient(base_url=base, timeout=timeout, trust_env=False) as admin:
            await login(admin, config)
            marker = uuid.uuid4().hex[:10]
            account = {"email": f"persistent-{marker}@suxiaoyou.test", "password": secrets.token_urlsafe(28),
                       "marker": marker, "workspace": "/tenant/workspaces/persistent-" + marker,
                       "plugin": "persistent-probe-" + marker, "connector_id": "qa-filesystem-" + marker}
            user = await checked(admin, "POST", "/api/cloud/admin/users", json={"email": account["email"], "password": account["password"],
                "display_name": "持续能力独立验收", "token_limit": 100000, "image_limit": 0, "max_concurrent_jobs": 3})
            account["user_id"] = user["id"]
            private(account_path, account)
    session_path = folder / "persistent-parity-session.json"
    async with httpx.AsyncClient(base_url=base, timeout=timeout, trust_env=False) as client:
        # Legacy raw-cookie caches lack origin/account checks and are rejected
        # by the shared loader; a validated private session or human login is required.
        await login(client, account, session_file=session_path if session_path.exists() else None)
        private(session_path, {"origin": base, "cookie": client.cookies["cloud_session"]})
        print("Dedicated real acceptance account authenticated; phase:", args.phase, flush=True)
        phases = {"prepare": prepare, "plugins": plugins, "mcp": mcp, "mcp-connect": mcp_connect, "goal": goal, "goal-resume": goal_resume, "batch": batch, "automation": automation, "memory": memory}
        before_phase = await receipts(client)
        previous_attempt = evidence["phases"].get(args.phase)
        if args.phase != "summary" and previous_attempt and not previous_attempt.get("passed"):
            evidence.setdefault("failed_attempts", []).append({"phase": args.phase, **previous_attempt})
        try:
            if args.phase != "summary":
                data = await phases[args.phase](client, account)
                evidence["phases"][args.phase] = {"passed": True, "result": data}
            evidence["accounting"] = await receipts(client)
        except (Failure, AssertionError, httpx.HTTPError, KeyError, StopIteration) as error:
            evidence["phases"][args.phase] = {"passed": False, "error": str(error) if isinstance(error, Failure) else type(error).__name__}
            evidence["accounting"] = await receipts(client)
            print("Real acceptance phase failed:", evidence["phases"][args.phase]["error"], flush=True)
        finally:
            if args.phase != "summary":
                evidence["phases"][args.phase]["accounting_before"] = before_phase
                evidence["phases"][args.phase]["accounting_after"] = evidence.get("accounting")
                evidence["phases"][args.phase]["actual_token_delta"] = evidence.get("accounting", {}).get("total_tokens", 0) - before_phase["total_tokens"]
            evidence["time"] = time.time()
            evidence["workspace"] = account["workspace"]
            private(evidence_path, evidence)
            public = {"kind": evidence["kind"], "internal_actual_token_limit": 300000, "workspace": account["workspace"], "runtime_image": evidence.get("runtime_image"), "accounting": evidence.get("accounting"), "phases": {}, "failed_attempts": [{key: row.get(key) for key in ("phase", "error", "actual_token_delta")} for row in evidence.get("failed_attempts", [])], "negative_cases": evidence.get("negative_cases", []), "accounting_checkpoints": evidence.get("accounting_checkpoints", [])}
            for name, phase in evidence["phases"].items():
                data = phase.get("result", {})
                public["phases"][name] = {"passed": phase["passed"], **({"error": phase["error"]} if not phase["passed"] else {})}
                if name == "goal" and not phase["passed"] and evidence["phases"].get("goal-resume", {}).get("passed"):
                    public["phases"][name]["superseded_by"] = "goal-resume"
                if "actual_token_delta" in phase:
                    public["phases"][name]["actual_token_delta"] = phase["actual_token_delta"]
                for key in ("connected", "tools_count", "explicit_confirmation_rejected", "changed_fingerprint_rejected", "dedicated_runtime_restarted", "npm_package", "version", "plugin_enable_adds_skill", "plugin_disable_removes_skill", "skill_disable_enable_verified", "file_verified", "children", "mode", "task_id", "no_browser_or_sse_attached", "history", "disabled_after_probe", "authenticated_download_matches", "bytes", "sha256", "fingerprint", "blocked_code", "before_limit_tokens", "side_effects_started", "provider_requests", "status_sequence", "plugin_reloaded_after_restart", "skill_reloaded_after_restart", "sentinel_sha256", "durable_turns", "persistent_memory_receipts", "runtime_image", "goal_status", "parent_session_id", "automation_remained_disabled", "no_new_model_calls", "accounting"):
                    if key in data:
                        public["phases"][name][key] = data[key]
                if "actual_agent_call" in data:
                    call = data["actual_agent_call"]
                    public["phases"][name]["actual_agent_call"] = {key: call[key] for key in ("session_id", "stream_id", "tools", "events", "permissions", "errors")}
                if name in {"goal", "goal-resume"} and data:
                    public["phases"][name]["statuses"] = [data[key]["status"] for key in ("created", "paused", "resumed", "final")]
            PUBLIC.write_text(json.dumps(public, ensure_ascii=False, indent=2) + "\n")
            print(json.dumps({"phase": args.phase, "passed": evidence["phases"].get(args.phase, {}).get("passed"), "accounting": evidence.get("accounting")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=["prepare", "plugins", "mcp", "mcp-connect", "goal", "goal-resume", "batch", "automation", "memory", "summary"], required=True)
    arguments = parser.parse_args()
    try:
        asyncio.run(run(arguments))
    except (Failure, HumanCaptchaRequired, httpx.HTTPError) as error:
        parser.exit(1, "Acceptance setup failed: " + (str(error) if isinstance(error, (Failure, HumanCaptchaRequired)) else type(error).__name__) + "\n")
