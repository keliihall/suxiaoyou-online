#!/usr/bin/env python3
"""Real isolated account: Git worktree, Hook consent, rewind and emergency stop.

Consumes real model tokens, capped at 100,000 settled tokens between rounds.
Only uniquely created test directories are mutated or rewound. Credentials are
kept in private cloud-dev storage; reports contain no account credentials.
"""
import asyncio
from cloud_login import HumanCaptchaRequired, login as human_login
import argparse
from collections import Counter
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import uuid

import httpx

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("cloud_dev", ROOT / "scripts/cloud-dev.py")
dev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dev)


async def checked(client, method, url, **kwargs):
    response = await client.request(method, url, **kwargs)
    if not response.is_success:
        try:
            code = response.json().get("code", "")
        except ValueError:
            code = ""
        raise RuntimeError(f"{method} {url} HTTP {response.status_code} {code}")
    return response


async def login(client, account):
    auth = await human_login(client, account["email"], account["password"])
    client.headers["x-csrf-token"] = auth["csrf_token"]
    return auth["user"]


def docker_python(container, script):
    result = subprocess.run(["docker", "--host", "unix:///Users/kelii/.lima/suxiaoyou-online/sock/docker.sock",
        "exec", "-i", container, "python", "-"], input=script.encode(), capture_output=True, timeout=60)
    if result.returncode:
        raise RuntimeError("Dedicated test container setup failed")
    return result.stdout.decode().strip()


async def receipts(client):
    rows = (await checked(client, "GET", "/api/cloud/usage/requests")).json()
    total = sum(row["total_tokens"] for row in rows)
    if total >= 100_000:
        raise RuntimeError("Real runtime acceptance model budget reached")
    return {"requests": len(rows), "total_tokens": total, "states": dict(Counter(r["state"] for r in rows))}


async def prompt(client, workspace, text, *, session=None, expected_hook=None, agent="explore"):
    await receipts(client)
    body = {"client_request_id": "runtime-control-" + uuid.uuid4().hex,
        "text": text, "model": "deepseek-office", "provider_id": "custom_cloud", "reasoning": False,
        "agent": agent,
        "workspace": workspace, "permission_presets": {"file_changes": True, "run_commands": False}}
    if session:
        body["session_id"] = session
    ids = (await checked(client, "POST", "/api/chat/prompt", json=body)).json()
    result = {**ids, "events": Counter(), "tools": [], "approved_hooks": [], "errors": []}
    event = ""
    try:
        async with asyncio.timeout(240):
            async with client.stream("GET", "/api/chat/stream/" + ids["stream_id"]) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("event: "):
                        event = line[7:]; result["events"][event] += 1
                    elif line.startswith("data: "):
                        data = json.loads(line[6:])
                        if event == "tool-call":
                            result["tools"].append(data)
                        elif event in {"agent-error", "error", "tool-error"}:
                            result["errors"].append(data)
                        elif event == "permission-request":
                            descriptor = data.get("arguments", {})
                            if (expected_hook is None or data.get("permission") != "hook_command"
                                    or descriptor.get("hook_id") != expected_hook
                                    or descriptor.get("event") != "UserPromptSubmit"
                                    or descriptor.get("source") != "project"):
                                raise RuntimeError("Unexpected runtime permission request")
                            await checked(client, "POST", "/api/chat/respond", json={
                                "stream_id": ids["stream_id"], "call_id": data["call_id"], "response": True})
                            result["approved_hooks"].append({"hook_id": expected_hook,
                                "fingerprint": descriptor["fingerprint"]})
                        elif event == "question":
                            raise RuntimeError("Unexpected clarification during bounded acceptance")
    except BaseException:
        try:
            await client.post("/api/chat/abort", json={"stream_id": ids["stream_id"]}, timeout=10)
        except httpx.HTTPError:
            pass
        raise
    if result["errors"]:
        raise RuntimeError("Real runtime Agent reported an error")
    return result


async def main(resume=None):
    config, marker = dev.configuration(), resume or uuid.uuid4().hex[:10]
    private = Path(config["data_dir"]) / ("runtime-control-" + marker + ".json")
    previous = json.loads(private.read_text()) if resume else None
    account = previous["account"] if previous else {"email": "runtime-control-" + marker + "@suxiaoyou.test", "password": secrets.token_urlsafe(24)}
    evidence = previous["evidence"] if previous else {"kind": "real-tenant-original-runtime-control", "run_id": marker, "rounds": []}
    dev.write_private(private, json.dumps({"account": account, "evidence": evidence}, indent=2))
    def save():
        dev.write_private(private, json.dumps({"account": account, "evidence": evidence}, ensure_ascii=False, indent=2))
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{config['api_port']}", timeout=300, trust_env=False) as admin, \
            httpx.AsyncClient(base_url=f"http://127.0.0.1:{config['api_port']}", timeout=300, trust_env=False) as user:
        if not resume:
            await login(admin, config)
        if not resume:
            await checked(admin, "POST", "/api/cloud/admin/users", json={**account,
                "display_name": "Runtime acceptance " + marker, "token_limit": 100_000})
        created = await login(user, account)
        await checked(user, "POST", "/api/cloud/runtime/start")
        print("Dedicated runtime ready: " + marker, flush=True)
        container = "sxy-user-" + hashlib.sha256(created["id"].encode()).hexdigest()[:32]
        workspace = "/tenant/workspaces/runtime-control-" + marker
        seed = '''import json,subprocess
from pathlib import Path
root=Path(WORKSPACE);root.mkdir(exist_ok=True);(root/".gitignore").write_text(".suxiaoyou/\\nhook.marker\\n")
(root/"README.txt").write_text("REAL_RUNTIME_CONTROL_MARKER\\n")
config=root/".suxiaoyou";config.mkdir(exist_ok=True);script=config/"marker.py"
script.write_text("#!/usr/local/bin/python\\nimport json,sys\\nfrom pathlib import Path\\ne=json.load(sys.stdin)\\nPath('hook.marker').write_text('REAL_HOOK_'+e['event'])\\nprint(json.dumps({'version':1,'decision':'continue'}))\\n");script.chmod(0o700)
(config/"hooks.json").write_text(json.dumps({'version':1,'hooks':[{'hook_id':'cloud-marker','event':'UserPromptSubmit','failure_policy':'required','timeout_seconds':5,'command':[str(script)]}]}))
if not (root/".git").exists():
 for args in [('init',),('config','user.name','Cloud QA'),('config','user.email','cloud-qa@suxiaoyou.test'),('add','README.txt','.gitignore'),('commit','-m','Seed isolated runtime acceptance')]:subprocess.run(['git','-C',str(root),*args],check=True,capture_output=True)
print(subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD']).decode().strip())
'''.replace("WORKSPACE", repr(workspace))
        evidence["git_seed_commit"] = await asyncio.to_thread(docker_python, container, seed)
        session = (await checked(user, "POST", "/api/sessions", json={"directory": workspace,
            "title": "Runtime acceptance " + marker})).json()["id"]
        try:
            if evidence.get("hook"):
                result = evidence["rounds"][0]
                session = result["session_id"]
            else:
                result = await prompt(user, workspace, "只实际调用read读取README.txt，然后回复其中标记。不要调用其他工具。",
                    session=session, expected_hook="cloud-marker")
                evidence["rounds"].append(result); save()
            hook = (await checked(user, "GET", "/api/security/hooks", params={"session_id": session})).json()
            marker_file = (await checked(user, "POST", "/api/files/content",
                json={"path": workspace + "/hook.marker", "workspace": workspace})).json()["content"]
            assert marker_file == "REAL_HOOK_UserPromptSubmit"
            evidence["hook"] = {"approval_requests": len(result["approved_hooks"]), "marker_verified": True,
                "listed": hook, "credential_free_environment": "original hook runner"}; save()
            print("Real Hook approval and execution verified", flush=True)
            context = (await checked(user, "GET", "/api/runtime/context", params={"session_id": session})).json()
            evidence["worktree_preflight"] = context; save()
            bound = (await checked(user, "POST", "/api/runtime/worktrees/create-bind",
                json={"session_id": session, "ref": "HEAD"})).json()
            scope = {"session_id": session, "workspace_instance_id": bound["workspace_instance_id"]}
            inspection = (await checked(user, "GET", "/api/runtime/worktrees/inspect", params=scope)).json()
            assert inspection["clean"] and inspection["registered"] and inspection["head"] == evidence["git_seed_commit"]
            released = (await checked(user, "POST", "/api/runtime/worktrees/release", json=scope)).json()
            after = (await checked(user, "GET", "/api/runtime/worktrees/inspect", params=scope)).json()
            assert not after["available"] and after["already_released"]
            evidence["worktree"] = {"bound": bound, "inspection": inspection, "released": released, "after": after}; save()
            print("Real Git worktree binding/inspect/release verified", flush=True)
            rewind_workspace = (await checked(user, "POST", "/api/cloud-runtime/files/directories/create",
                json={"parent": "/tenant/workspaces", "name": "rewind-control-" + marker})).json()["path"]
            result = await prompt(user, rewind_workspace,
                "只实际使用write工具创建 rewind-proof.txt，内容是REAL_REWIND_PROOF，然后实际read读回。不要调用其他工具。", agent="build")
            evidence["rounds"].append(result); save()
            sid = result["session_id"]
            files = (await checked(user, "GET", f"/api/sessions/{sid}/files")).json()["files"]
            generated = next(row for row in files if row["name"] == "rewind-proof.txt")
            before = (await checked(user, "POST", "/api/files/content", json={"path": generated["path"], "workspace": rewind_workspace})).json()
            assert "REAL_REWIND_PROOF" in before["content"]
            context = (await checked(user, "GET", "/api/runtime/context", params={"session_id": sid})).json()
            scope = {"session_id": sid, "workspace_instance_id": context["workspace_instance_id"]}
            checkpoints = (await checked(user, "GET", "/api/runtime/checkpoints", params=scope)).json()
            target = checkpoints["checkpoints"][0]["checkpoint_id"]
            preview = (await checked(user, "POST", "/api/runtime/rewind/preview", json={**scope, "checkpoint_id": target})).json()
            assert preview["can_execute"]
            execution = (await checked(user, "POST", "/api/runtime/rewind/execute", json={**scope, "checkpoint_id": target})).json()
            missing = await user.post("/api/files/content", json={"path": generated["path"], "workspace": rewind_workspace})
            assert missing.status_code == 404
            evidence["rewind"] = {"checkpoint_list": checkpoints, "preview": preview, "execution": execution, "created_file_removed": True}; save()
            print("Real checkpoint rewind execution verified", flush=True)
            active = (await checked(user, "POST", "/api/security/emergency-stop", json={"active": True})).json()
            denied = await user.post("/api/chat/prompt", json={"text": "This must be rejected before inference", "model": "deepseek-office",
                "provider_id": "custom_cloud", "workspace": workspace})
            assert denied.status_code in {403,409,423,503}, denied.status_code
            resumed = (await checked(user, "POST", "/api/security/emergency-stop", json={"active": False})).json()
            result = await prompt(user, workspace, "只实际read读取README.txt并回复标记，不要调用其他工具。", session=session, expected_hook="cloud-marker")
            evidence["rounds"].append(result)
            evidence["emergency_stop"] = {"activation_warnings": active.get("warnings"), "blocked_new_prompt_status": denied.status_code,
                "resume_warnings": resumed.get("warnings"), "post_resume_real_read_succeeded": any(t.get("tool")=="read" for t in result["tools"])}
            evidence["accounting"] = await receipts(user); evidence["complete"] = True; save()
            output = ROOT.parent / "assessment/parity-runtime-control-validation.json"
            output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
            print(json.dumps({"complete": True, "evidence": str(output), "accounting": evidence["accounting"]}, ensure_ascii=False), flush=True)
        except BaseException as error:
            evidence["failure"] = str(error) if isinstance(error, (RuntimeError, AssertionError)) else type(error).__name__; save()
            print("Private runtime evidence saved: " + str(private), flush=True)
            raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume")
    try:
        asyncio.run(main(parser.parse_args().resume))
    except HumanCaptchaRequired as error:
        parser.exit(1, str(error) + "\n")
