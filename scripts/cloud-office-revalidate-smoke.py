#!/usr/bin/env python3
"""Revalidate an existing real template after a runtime upgrade; no model call."""
import asyncio
from cloud_login import HumanCaptchaRequired, login as human_login
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("dev", ROOT / "scripts/cloud-dev.py")
dev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dev)


async def checked(client, method, url, **kwargs):
    response = await client.request(method, url, **kwargs)
    if not response.is_success:
        error = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        code = error.get("code", "request_failed") if isinstance(error, dict) else "request_failed"
        raise RuntimeError(f"{method} {url.split('?')[0]} HTTP {response.status_code}: {code}")
    return response


async def main(run_id, restart=False):
    config = dev.configuration()
    original = json.loads((Path(config["data_dir"]) / ("office-real-" + run_id + ".json")).read_text())
    completed = next(f for f in original["files"] if f["filename"] == "uploaded-brief.docx")
    scope = {key: completed["validation"][key] for key in ("session_id", "workspace_instance_id")}
    ref = original["template_approval"]["template_ref"]
    endpoint = "/api/office-v2/user-templates/" + ref
    def body(template):
        return {**scope, "revision": template["revision"], "expected_state_version": template["state_version"],
            "expected_source_sha256": template["source"]["sha256"],
            "expected_render_cache_key": template["render_evidence"]["cache_key"]}
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{config['api_port']}", trust_env=False, timeout=300) as client:
        login = await human_login(client, config["test_user"]["email"], config["test_user"]["password"])
        client.headers["x-csrf-token"] = login["csrf_token"]
        if restart:
            await checked(client, "POST", "/api/cloud/runtime/restart")
        before = (await checked(client, "GET", "/api/cloud/usage/requests")).json()
        listing = (await checked(client, "GET", "/api/office-v2/user-templates", params=scope)).json()["templates"]
        old = next(t for t in listing if t["template_ref"] == ref and t["revision"] == 1)
        assert old["status"] == "approved" and not old["can_instantiate"]
        unchanged = (await checked(client, "POST", endpoint + "/approve", json=body(old))).json()
        assert unchanged["idempotent"] and not unchanged["template"]["can_instantiate"]
        refreshed = (await checked(client, "POST", endpoint + "/revalidate", json=body(old))).json()["template"]
        assert refreshed["template_ref"] == old["template_ref"] and refreshed["revision"] == 2
        assert refreshed["source"]["sha256"] == old["source"]["sha256"]
        assert refreshed["status"] == "needs_confirmation" and not refreshed["can_instantiate"]
        assert refreshed["render_evidence"]["renderer_version"] != old["render_evidence"]["renderer_version"]
        replay = await client.post(endpoint + "/revalidate", json=body(old))
        assert replay.status_code == 409
        page = await checked(client, "GET", endpoint + "/page", params={**scope,
            "revision": refreshed["revision"], "expected_state_version": refreshed["state_version"], "page_number": 1})
        assert page.content.startswith(b"\x89PNG\r\n\x1a\n")
        approved = (await checked(client, "POST", endpoint + "/approve", json=body(refreshed))).json()["template"]
        assert approved["can_instantiate"] and approved["status"] == "approved"
        listing = (await checked(client, "GET", "/api/office-v2/user-templates", params=scope)).json()["templates"]
        retained = next(t for t in listing if t["template_ref"] == ref and t["revision"] == 1)
        assert retained["source"] == old["source"] and retained["render_evidence"] == old["render_evidence"]
        assert retained["status"] == "approved" and retained["time_approved"] == old["time_approved"]
        assert retained["placeholder_schema"] == old["placeholder_schema"]
        assert retained["state_version"] == old["state_version"] + 1
        after = (await checked(client, "GET", "/api/cloud/usage/requests")).json()
        assert {row["id"] for row in before} == {row["id"] for row in after}
        evidence = {"kind": "real-runtime-upgrade-template-revalidation", "original_revision": 1,
            "new_revision": approved["revision"], "source_sha256": approved["source"]["sha256"],
            "old_approval_does_not_authorize_new_renderer": True,
            "old_source_approval_and_render_evidence_retained": True,
            "new_revision_requires_user_confirmation": True, "old_state_cas_replay_status": replay.status_code,
            "new_preview_png_sha256": hashlib.sha256(page.content).hexdigest(),
            "old_renderer_version": old["render_evidence"]["renderer_version"],
            "new_renderer_version": approved["render_evidence"]["renderer_version"],
            "approved_can_instantiate": approved["can_instantiate"], "model_calls": 0}
        target = ROOT.parent / "assessment/parity-office-validation.json"
        public = json.loads(target.read_text()); public["user_template_upgrade_revalidation"] = evidence
        target.write_text(json.dumps(public, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(evidence, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--restart", action="store_true", help="Restart only the ordinary fixture account engine")
    args = parser.parse_args()
    try:
        asyncio.run(main(args.run_id, args.restart))
    except HumanCaptchaRequired as error:
        parser.exit(1, str(error) + "\n")
