#!/usr/bin/env python3
"""Exercise real tenant containers; --agent also spends real model tokens.

Uses private cloud-dev configuration, not browser credentials or fabricated
providers. Test files are uniquely named; existing user files are untouched.
"""
import argparse
import asyncio
from cloud_login import login as human_login
from collections import Counter
import importlib.util
import json
from pathlib import Path, PurePosixPath
import time
import uuid

import httpx

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("cloud_dev", ROOT / "scripts/cloud-dev.py")
dev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dev)


async def checked(client, method, path, **kwargs):
    response = await client.request(method, path, **kwargs)
    if not response.is_success:
        raise RuntimeError(f"{method} {path.split('?')[0]} failed: HTTP {response.status_code}")
    return response


async def login(client, account):
    auth = await human_login(client, account["email"], account["password"])
    client.headers["x-csrf-token"] = auth["csrf_token"]


async def read(client, filename):
    return await client.post("/api/files/content", json={"path": "/tenant/workspaces/" + filename,
                                                         "workspace": "/tenant/workspaces"})


async def agent_round(client, route_id, marker):
    filename = "cloud-parity-agent-" + marker + ".txt"
    prompt = await checked(client, "POST", "/api/chat/prompt", json={
        "client_request_id": "cloud-parity-" + uuid.uuid4().hex,
        "text": f"请实际使用文件工具创建 {filename}，内容为 ONLINE_REAL_AGENT_{marker}；"
                "随后实际读取该文件并回复其中的标记。必须完成真实文件写入和读取。",
        "model": route_id, "provider_id": "custom_cloud", "agent": "build", "reasoning": False,
        "workspace": "/tenant/workspaces", "permission_presets": {"file_changes": True, "run_commands": False}})
    ids, events, tools, errors, written = prompt.json(), Counter(), [], [], None
    event = ""
    async with client.stream("GET", "/api/chat/stream/" + ids["stream_id"]) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if line.startswith("event: "):
                event = line[7:]
                events[event] += 1
            elif line.startswith("data: ") and event in {"tool-call", "agent-error", "error"}:
                data = json.loads(line[6:])
                if event == "tool-call":
                    tools.append(data.get("tool"))
                    if data.get("tool") == "write":
                        candidate = PurePosixPath(data.get("arguments", {}).get("file_path", ""))
                        try:
                            relative = candidate.relative_to("/tenant/workspaces")
                        except ValueError:
                            continue
                        if relative.name == filename and ".." not in relative.parts:
                            written = str(relative)
                else:
                    errors.append(data.get("code") or data.get("error_type") or "agent_error")
    content = await read(client, written or filename)
    if errors or "write" not in tools or "read" not in tools or content.status_code != 200:
        raise RuntimeError("Real Agent write/read validation failed")
    if "ONLINE_REAL_AGENT_" + marker not in content.json().get("content", ""):
        raise RuntimeError("Agent file bytes do not match the requested marker")
    return {**ids, "file": written or filename, "file_verified": True, "events": dict(events), "tools": tools}


async def run(args):
    config = dev.configuration()
    origin = f"http://127.0.0.1:{config['api_port']}"
    timeout = httpx.Timeout(300, connect=10)
    async with httpx.AsyncClient(base_url=origin, timeout=timeout, trust_env=False) as admin, \
            httpx.AsyncClient(base_url=origin, timeout=timeout, trust_env=False) as ordinary:
        for _ in range(60):
            try:
                if (await admin.get("/readyz")).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(.5)
        else:
            raise RuntimeError("Cloud API did not become ready")
        await login(admin, config)
        if not config.get("test_user"):
            import secrets
            account = {"email": "parity-" + uuid.uuid4().hex[:10] + "@suxiaoyou.test",
                       "password": secrets.token_urlsafe(24)}
            await checked(admin, "POST", "/api/cloud/admin/users", json={**account, "display_name": "云端隔离验收"})
            config["test_user"] = account
            dev.write_configuration(config)
        await login(ordinary, config["test_user"])
        for name, client in (("administrator", admin), ("ordinary user", ordinary)):
            result = await checked(client, "POST", "/api/cloud/runtime/start")
            if result.json()["status"] != "ready":
                raise RuntimeError("Tenant runtime is not ready")
            print(f"Real runtime ready: {name}", flush=True)
        marker = uuid.uuid4().hex[:12]
        filename = "cloud-parity-isolation-" + marker + ".txt"
        for client, value in ((admin, "ADMIN_PRIVATE_"), (ordinary, "ORDINARY_PRIVATE_")):
            await checked(client, "POST", "/api/cloud-runtime/files/upload",
                files={"file": (filename, (value + marker).encode(), "text/plain")})
            result = await read(client, filename)
            if result.status_code != 200 or result.json().get("content") != value + marker:
                raise RuntimeError("Per-tenant file namespace isolation failed")
        if (await ordinary.get("/api/cloud/admin/users")).status_code != 403:
            raise RuntimeError("Ordinary account unexpectedly accessed administration")
        evidence = {"time": time.time(), "runtime": "real-docker", "same_virtual_path_isolated": True,
                    "ordinary_admin_denied": True, "model_calls": False}
        if args.agent:
            models = (await checked(admin, "GET", "/api/cloud/models")).json()
            selected = next((row for row in models if row["id"] == args.model and row.get("provider") != "mock"), None)
            if selected is None:
                raise RuntimeError("Selected real model route is unavailable; mock providers are refused")
            before = {row["id"] for row in (await checked(admin, "GET", "/api/cloud/usage/requests")).json()}
            evidence.update(await agent_round(admin, selected["id"], marker))
            if (await read(ordinary, evidence["file"])).status_code != 404:
                raise RuntimeError("Private Agent file leaked to another user")
            if (await ordinary.get("/api/sessions/" + evidence["session_id"])).status_code != 404:
                raise RuntimeError("Private Agent session leaked to another user")
            requests = [row for row in (await checked(admin, "GET", "/api/cloud/usage/requests")).json()
                        if row["id"] not in before and row["source"] == "agent"]
            if not requests or any(row["state"] != "settled" or row["total_tokens"] < 1 for row in requests):
                raise RuntimeError("Agent inference receipts were not completely settled")
            evidence.update(model_calls=True, requests=len(requests),
                total_tokens=sum(row["total_tokens"] for row in requests), cross_user_session_blocked=True,
                cross_user_file_blocked=True)
        output = Path(config["data_dir"]) / "last-agent-parity-smoke.json"
        dev.write_private(output, json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(evidence, ensure_ascii=False), flush=True)
        print("Private evidence saved: " + str(output), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", action="store_true", help="Execute real model tools; consumes vendor tokens")
    parser.add_argument("--model", default="deepseek-office")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except (RuntimeError, httpx.HTTPError) as error:
        parser.exit(1, "Parity smoke failed: " + (str(error) if isinstance(error, RuntimeError) else type(error).__name__) + "\n")
