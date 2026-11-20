#!/usr/bin/env python3
"""Exercise a real model through the running cloud Web/API/Worker (billable)."""
from __future__ import annotations

import argparse
import asyncio
from cloud_login import HumanCaptchaRequired, login as human_login
import json
from pathlib import Path
import secrets
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "/api/cloud"


async def login(client, email, password):
    auth = await human_login(client, email, password)
    client.headers["X-CSRF-Token"] = auth["csrf_token"]
    return auth["user"]


async def run(base_url: str | None, model_id: str | None, max_output_tokens: int):
    config = json.loads((ROOT / ".cloud-dev.json").read_text())
    base_url = base_url or f"http://localhost:{config['web_port']}"
    async with httpx.AsyncClient(base_url=base_url, trust_env=False, timeout=30) as admin, \
               httpx.AsyncClient(base_url=base_url, trust_env=False, timeout=30) as user, \
               httpx.AsyncClient(base_url=base_url, trust_env=False, timeout=30) as stranger:
        await login(admin, config["email"], config["password"])
        routes_response = await admin.get(PREFIX + "/admin/models")
        routes_response.raise_for_status()
        routes = routes_response.json()
        selected = model_id or config.get("default_model_id")
        route = next((row for row in routes if row["id"] == selected), None) if selected else next(
            (row for row in routes if row["enabled"] and row["provider"] != "mock"), None)
        if route is None or route["provider"] == "mock" or not route["enabled"]:
            raise SystemExit("No enabled real model route. Run cloud-dev.py configure-model first; mock is refused.")
        if not route.get("key_configured"):
            raise SystemExit("The model key is unavailable to the running API. Configure its private key and restart API/Worker.")
        suffix = secrets.token_hex(4)
        password = secrets.token_urlsafe(18)
        accounts = []
        for label in ("tester", "isolation"):
            response = await admin.post(PREFIX + "/admin/users", json={
                "email": f"{label}-{suffix}@suxiaoyou.test", "password": password,
                "display_name": "功能测试" if label == "tester" else "隔离测试",
                "token_limit": 50000, "max_concurrent_jobs": 2,
            })
            response.raise_for_status()
            accounts.append(response.json())
        await login(user, accounts[0]["email"], password)
        await login(stranger, accounts[1]["email"], password)
        workspace = await user.post(PREFIX + "/workspaces", json={"name": "云端验收材料"})
        workspace.raise_for_status()
        workspace_id = workspace.json()["id"]
        conversation = await user.post(PREFIX + "/conversations", json={"workspace_id": workspace_id})
        conversation.raise_for_status()
        conversation_id = conversation.json()["id"]
        marker = "REAL-" + suffix.upper()
        material = f"验收编号：{marker}。月预算：25000 Token。安全边界：多用户、文件隔离、任务隔离和账号额度。"
        uploaded = await user.post(PREFIX + "/files", params={"workspace_id": workspace_id},
            files={"file": ("真实模型验收.txt", material.encode(), "text/plain")})
        uploaded.raise_for_status()
        asset = uploaded.json()
        assert "path" not in asset and "object_key" not in asset
        job = await user.post(PREFIX + "/jobs", json={
            "conversation_id": conversation_id, "prompt": "请用中文简洁总结附件中的验收编号、月 Token 预算和关键安全边界。请原样保留编号。",
            "model_id": route["id"], "max_output_tokens": max_output_tokens,
            "file_ids": [asset["id"]], "idempotency_key": secrets.token_hex(16),
        })
        job.raise_for_status()
        job_id = job.json()["id"]
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            result = await user.get(PREFIX + f"/jobs/{job_id}")
            result.raise_for_status()
            if result.json()["state"] in {"completed", "failed", "cancelled", "needs_review"}:
                break
            await asyncio.sleep(0.2)
        assert result.json()["state"] == "completed", result.text
        answer = result.json()["answer"]
        assert marker in answer and "25000" in answer.replace(",", "").replace("，", ""), answer
        history = await user.get(PREFIX + f"/conversations/{conversation_id}")
        assert [item["role"] for item in history.json()["messages"]] == ["user", "assistant"]
        stream = await user.get(PREFIX + f"/jobs/{job_id}/events")
        assert "event: completed" in stream.text and "event: delta" in stream.text
        for path in (f"/conversations/{conversation_id}", f"/files/{asset['id']}/download",
                     f"/jobs/{job_id}", f"/jobs/{job_id}/events"):
            assert (await stranger.get(PREFIX + path)).status_code == 404
        assert (await stranger.post(PREFIX + f"/jobs/{job_id}/cancel")).status_code == 404
        assert (await user.get(PREFIX + "/admin/users")).status_code == 403
        usage = (await user.get(PREFIX + "/usage")).json()
        assert usage["used_tokens"] > 0 and usage["reserved_tokens"] == 0
        requests = (await admin.get(PREFIX + "/admin/usage/requests")).json()
        receipt = next(row for row in requests if row["job_id"] == job_id)
        assert receipt["state"] == "settled" and receipt["total_tokens"] == usage["used_tokens"]
        assert receipt["price_snapshot"]["provider"] != "mock"
        assert (await stranger.get(PREFIX + "/usage")).json()["used_tokens"] == 0
        download = await user.get(PREFIX + f"/files/{asset['id']}/download")
        assert "文件隔离" in download.content.decode()
        audit = await admin.get(PREFIX + "/admin/audit")
        assert audit.status_code == 200 and len(audit.json()) >= 3
        # Leave one regular account and its materials available for human testing.
        config["test_user"] = {"email": accounts[0]["email"], "password": password}
        (ROOT / ".cloud-dev.json").write_text(json.dumps(config, ensure_ascii=False, indent=2))
        (ROOT / ".cloud-dev.json").chmod(0o600)
        report = {"base_url": base_url, "result": "passed", "provider": route["provider"],
            "model": route["model"], "model_id": route["id"], "job_id": job_id,
            "request_id": receipt["id"], "answer": answer, "usage": receipt["normalized_usage"], "checks": [
            "real cookie login and CSRF", "admin-created users", "file upload/download",
            "real paid model stream", "independent durable Worker", "document extraction", "conversation persistence",
            "SSE event replay", "cross-user resource/stream/cancel denial", "admin RBAC",
            "provider usage receipt and settled ledger", "audit trail"], "used_tokens": usage["used_tokens"],
            "test_credentials": str(ROOT / ".cloud-dev.json")}
        report_path = Path(config["data_dir"]) / "last-live-smoke.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        report_path.chmod(0o600)
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url")
    parser.add_argument("--model-id", help="Enabled real model route; defaults to the configured route")
    parser.add_argument("--max-output-tokens", type=int, default=512, choices=range(128, 2049), metavar="128..2048")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.base_url, args.model_id, args.max_output_tokens))
    except HumanCaptchaRequired as error:
        parser.exit(1, str(error) + "\n")
