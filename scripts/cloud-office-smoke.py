#!/usr/bin/env python3
"""Real account/DeepSeek/Office precommit verification; spends provider tokens.

Credentials are read from private cloud-dev configuration. Evidence contains
test artefacts, account roles and accounting totals, never credentials or keys.
"""
from __future__ import annotations

import argparse
import asyncio
from cloud_login import login as human_login
import base64
from collections import Counter
import hashlib
import importlib.util
import io
import json
from pathlib import Path, PurePosixPath
import time
import uuid
import zipfile

import httpx

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("cloud_dev", ROOT / "scripts/cloud-dev.py")
dev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dev)


async def checked(client, method, path, **kwargs):
    response = await client.request(method, path, **kwargs)
    if not response.is_success:
        try:
            code = response.json().get("code", "")
        except ValueError:
            code = ""
        raise RuntimeError(f"{method} {path.split('?')[0]} HTTP {response.status_code} {code}")
    return response


async def login(client, account):
    auth = await human_login(client, account["email"], account["password"])
    client.headers["x-csrf-token"] = auth["csrf_token"]


async def agent_round(client, model, text, *, workspace, session_id=None):
    body = {"client_request_id": "office-real-" + uuid.uuid4().hex, "text": text,
        "model": model, "provider_id": "custom_cloud", "agent": "build", "reasoning": False,
        "workspace": workspace, "permission_presets": {"file_changes": True, "run_commands": False}}
    if session_id:
        body["session_id"] = session_id
    ids = (await checked(client, "POST", "/api/chat/prompt", json=body)).json()
    events, tools, results, errors, assistant, tool_errors = Counter(), [], [], [], [], []
    event = ""
    print("Real model stream started: " + ids["session_id"], flush=True)
    async with client.stream("GET", "/api/chat/stream/" + ids["stream_id"]) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if line.startswith("event: "):
                event = line[7:]
                events[event] += 1
            elif line.startswith("data: "):
                data = json.loads(line[6:])
                if event == "tool-call":
                    tools.append(data)
                    print("Tool called: " + str(data.get("tool")), flush=True)
                elif event == "tool-result":
                    results.append(data)
                    print("Tool result received", flush=True)
                elif event == "tool-error":
                    tool_errors.append(data)
                    print("Tool rejected candidate", flush=True)
                elif event in {"agent-error", "error"}:
                    errors.append(data)
                    print("Agent reported an error", flush=True)
                elif event == "text-delta":
                    assistant.append(data)
                elif event == "permission-request":
                    raise RuntimeError("Unexpected manual permission request; no blanket approval is supplied")
                elif event == "question":
                    errors.append({"code": "unexpected_question", "call_id": data.get("call_id")})
                    await checked(client, "POST", "/api/chat/abort", json={"stream_id": ids["stream_id"]})
    return {**ids, "events": dict(events), "tools": tools, "results": results,
            "errors": errors, "tool_errors": tool_errors, "assistant": assistant}


def inspect_office(content, format_name):
    with zipfile.ZipFile(io.BytesIO(content)) as package:
        names = package.namelist()
        if format_name == "docx":
            main = package.read("word/document.xml").decode()
            return {"table": "<w:tbl>" in main, "sections": "<w:sectPr" in main,
                "header": any(name.startswith("word/header") for name in names),
                "footer": any(name.startswith("word/footer") for name in names)}
        if format_name == "xlsx":
            main = "".join(package.read(name).decode() for name in names
                           if name.startswith("xl/worksheets/") and name.endswith(".xml"))
            return {"chart": any(name.startswith("xl/charts/chart") for name in names),
                "formula": "<f>" in main, "freeze": "<pane" in main,
                "validation": "<dataValidations" in main,
                "conditional_format": "<conditionalFormatting" in main}
        main = "".join(package.read(name).decode() for name in names
                       if name.startswith("ppt/slides/slide") and name.endswith(".xml"))
        return {"chart": any(name.startswith("ppt/charts/chart") for name in names),
            "notes": any(name.startswith("ppt/notesSlides/notesSlide") for name in names),
            "table": "<a:tbl>" in main}


async def verify_file(client, session_id, filename, workspace, *, advanced=False):
    listing = (await checked(client, "GET", f"/api/sessions/{session_id}/files")).json()["files"]
    entry = next((item for item in listing if item["name"] == filename), None)
    if entry is None:
        raise RuntimeError("Requested Office output was not committed: " + filename)
    path = entry["path"]
    relative = str(PurePosixPath(path).relative_to(workspace))
    binary = (await checked(client, "POST", "/api/files/content-binary",
        json={"path": path, "workspace": workspace})).json()
    content = base64.b64decode(binary["content_base64"], validate=True)
    structure = inspect_office(content, filename.rsplit(".", 1)[1])
    if advanced and not all(structure.values()):
        raise RuntimeError("Office advanced structure verification failed: " + str(structure))
    context = (await checked(client, "GET", "/api/office-v2/context",
        params={"session_id": session_id})).json()
    identity = {"session_id": session_id, "workspace_instance_id": context["workspace_instance_id"],
        "relative_path": relative}
    validation = (await checked(client, "GET", "/api/office-v2/validation", params=identity)).json()
    if validation.get("status") != "authoritative_pass":
        raise RuntimeError("Committed Office file does not have current authoritative validation")
    rendered = (await checked(client, "POST", "/api/office-v2/render", json={**identity,
        "expected_source_sha256": hashlib.sha256(content).hexdigest()})).json()
    cache_key = rendered.get("cache_key") or rendered.get("manifest", {}).get("cache_key")
    if not cache_key:
        raise RuntimeError("Office renderer did not return a cache identity")
    page = await checked(client, "GET", "/api/office-v2/page",
        params={**identity, "cache_key": cache_key, "page_number": 1})
    if not page.content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("Office preview did not return PNG bytes")
    return {"filename": filename, "relative_path": relative, "bytes": len(content),
        "source_sha256": hashlib.sha256(content).hexdigest(), "structure": structure,
        "validation": validation, "render": rendered, "preview_png_sha256": hashlib.sha256(page.content).hexdigest()}


async def template_round(client, model, workspace, marker, evidence, save):
    signed = await agent_round(client, model,
        '请实际只用office工具创建 signed-brief.docx，使用已签名first_party_template，'
        'template_id="business-brief",template_version="1.0.0",values如下：'
        + json.dumps({"classification": "INTERNAL", "next_step": "Review cloud acceptance",
            "owner": "Cloud QA", "recipient": "Product team", "report_date": "2026-11-15",
            "summary": "The real cloud runtime generates and validates Office files.",
            "title": "Signed Cloud Template"})
        + '。再创建 signed-tracker.xlsx，first_party_template template_id="project-tracker",'
        'template_version="1.0.0",values='
        + json.dumps({"owner": "Cloud QA", "project_name": "Cloud Migration", "report_date": "2026-11-15"})
        + '。再创建 signed-update.pptx，first_party_template template_id="status-update",'
        'template_version="1.0.0",values='
        + json.dumps({"next_step": "Review acceptance", "owner": "Cloud QA", "period": "November 2026",
            "project_name": "Cloud Migration", "status": "In review", "summary": "Real signed template acceptance."})
        + '。不要附带document/workbook/presentation字段，禁止使用代码或写文件绕过office；'
        '不要调用todo或question；成功后present_file展示三份文件再简短报告。', workspace=workspace)
    evidence["rounds"].append(signed); save()
    for filename in ("signed-brief.docx", "signed-tracker.xlsx", "signed-update.pptx"):
        evidence["files"].append(await verify_file(client, signed["session_id"], filename, workspace)); save()
    context = (await checked(client, "GET", "/api/office-v2/context",
        params={"session_id": signed["session_id"]})).json()
    from docx import Document
    document = Document()
    document.add_heading("Cloud User Template", level=1)
    document.add_paragraph("Prepared for {{recipient}}.")
    document.add_paragraph("Review result: {{summary}}")
    data = io.BytesIO(); document.save(data)
    schema = [{"name": name, "type": "text", "required": True, "min_chars": 1, "max_chars": 200}
              for name in ("recipient", "summary")]
    scope = {"session_id": signed["session_id"], "workspace_instance_id": context["workspace_instance_id"]}
    imported = (await checked(client, "POST", "/api/office-v2/user-templates/import",
        files={"file": ("cloud-user-template.docx", data.getvalue(),
                         "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        data={**scope, "client_request_id": "office-import-" + marker, "display_name": "Cloud user template " + marker,
              "placeholder_schema": json.dumps(schema)})).json()["template"]
    evidence["template_import"] = imported; save()
    if imported["status"] != "needs_confirmation" or not imported["can_approve"] or imported["can_instantiate"]:
        raise RuntimeError("Uploaded template did not require authoritative user approval")
    preview = await checked(client, "GET", "/api/office-v2/user-templates/" + imported["template_ref"] + "/page",
        params={**scope, "revision": imported["revision"], "page_number": 1,
                "expected_state_version": imported["state_version"]})
    if not preview.content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("Uploaded user template preview is not PNG")
    approved = (await checked(client, "POST", "/api/office-v2/user-templates/" + imported["template_ref"] + "/approve",
        json={**scope, "revision": imported["revision"], "expected_state_version": imported["state_version"],
              "expected_source_sha256": imported["source"]["sha256"],
              "expected_render_cache_key": imported["render_evidence"]["cache_key"]})).json()["template"]
    evidence["template_approval"] = approved; save()
    if approved["status"] != "approved" or approved["can_instantiate"] is not True:
        raise RuntimeError("Approved template cannot be instantiated")
    result = await agent_round(client, model,
        "请实际只用office工具基于已批准用户模板创建 uploaded-brief.docx，user_template参数为："
        + json.dumps({"template_ref": approved["template_ref"], "revision": approved["revision"],
            "expected_state_version": approved["state_version"],
            "values": {"recipient": "Cloud Acceptance", "summary": "REAL_USER_TEMPLATE_" + marker}})
        + "。禁止使用普通document或first_party_template，不要用代码或写文件绕过office；成功后简短报告。",
        workspace=workspace, session_id=signed["session_id"])
    evidence["rounds"].append(result); save()
    verified = await verify_file(client, result["session_id"], "uploaded-brief.docx", workspace)
    binary = (await checked(client, "POST", "/api/files/content-binary",
        json={"path": workspace + "/" + verified["relative_path"], "workspace": workspace})).json()
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(binary["content_base64"]))) as package:
        xml = package.read("word/document.xml").decode()
    if "REAL_USER_TEMPLATE_" + marker not in xml or "{{" in xml:
        raise RuntimeError("User template substitution bytes do not match")
    evidence["files"].append(verified); save()
    print("Real signed and uploaded template flows verified", flush=True)


async def run(args):
    config = dev.configuration()
    marker = args.run_id or uuid.uuid4().hex[:10]
    workspace = "/tenant/workspaces/office-real-" + marker
    evidence_path = Path(config["data_dir"]) / ("office-real-" + marker + ".json")
    evidence = (json.loads(evidence_path.read_text()) if evidence_path.exists() else
        {"time": time.time(), "run_id": marker, "runtime": "real-docker",
         "provider": "deepseek", "model": args.model, "rounds": [], "files": []})
    if "failure" in evidence:
        evidence.setdefault("failure_history", []).append(evidence.pop("failure"))
    def save():
        dev.write_private(evidence_path, json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    timeout = httpx.Timeout(360, connect=10)
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{config['api_port']}", timeout=timeout, trust_env=False) as client:
        await login(client, config if args.account == "admin" else config["test_user"])
        routes = (await checked(client, "GET", "/api/cloud/models")).json()
        if not any(row["id"] == args.model and row.get("provider") != "mock" for row in routes):
            raise RuntimeError("Real DeepSeek model route is unavailable")
        before = {row["id"] for row in (await checked(client, "GET", "/api/cloud/usage/requests")).json()}
        if not args.run_id:
            await checked(client, "POST", "/api/cloud-runtime/files/directories/create",
                json={"parent": "/tenant/workspaces", "name": "office-real-" + marker})
        try:
            text = (
                "请只使用真实 office 工具，创建以下三个文件，禁止用 bash/code_execute/write 绕过 Office 提交前校验。"
                "所有文档内容用英文，示例短小，能在一页呈现。禁止伪造完成；如遇工具错误请原样报告并停止。"
                "1. cloud-report.docx：标题 Cloud Migration Acceptance，正文一段，包含2列2行数据表，"
                "表头蓝色；使用 sections 配置A4纵向、页眉Cloud Office、页脚Acceptance。"
                "2. cloud-budget.xlsx：Data 工作表，A1:B3为Item/Amount,Alpha/10,Beta/20，B4公式=SUM(B2:B3)，"
                "冻结A2，B2:B3设置大于15黄色条件格式，B2:B3设置整数0至100数据验证，A6位置原生柱状图。"
                "3. cloud-update.pptx：只有一页，标题 Cloud Acceptance；左下角2列2行简表，右侧原生柱状图"
                "Alpha=10 Beta=20；使用明确位置避免重叠，speaker_notes 写入 REAL_CLOUD_NOTES_" + marker +
                "。每次创建成功后再做下一个，全部完成后只简短报告结果。"
            )
            if args.stage in {"all", "basic"}:
                result = await agent_round(client, args.model, text, workspace=workspace)
                evidence["rounds"].append(result); save()
                if result["errors"]:
                    raise RuntimeError("Real Office agent returned errors")
                for filename in ("cloud-report.docx", "cloud-budget.xlsx", "cloud-update.pptx"):
                    verified = await verify_file(client, result["session_id"], filename, workspace, advanced=True)
                    evidence["files"].append(verified); save()
                    print("Real Office verified: " + filename, flush=True)
            if args.stage in {"advanced", "presentation"}:
                spreadsheet_prompt = (
                    "请只使用真实office工具创建两份英文文件，禁止用代码或写文件绕过Office提交前校验。"
                    "1.cloud-budget.xlsx: Data工作表rows=[[Item,Amount],[Alpha,10],[Beta,20],[Total,=SUM(B2:B3)]];"
                    "冻结A2；B2:B3大于15黄色条件格式；B2:B3整数between 0和100数据验证；"
                    "原生bar chart,data_range=B1:B3,categories_range=A2:A3,titles_from_data=true,anchor=A6。"
                    "重要：A6令15cm宽图表在纸张内，避免D1造成边缘空白打印页。")
                result = await agent_round(client, args.model,
                    (spreadsheet_prompt if args.stage == "advanced" else
                     "请只用真实office工具创建一份英文文件，禁止用代码或写文件绕过提交前校验。")
                    + "cloud-update.pptx:只有一页，使用Title Only布局，标题Cloud Acceptance在顶部，左侧表格"
                    "left_inches=0.5,top_inches=2,width_inches=4,height_inches=1.5，headers=[Item,Amount],rows=[[Alpha,10],[Beta,20]]；"
                    "原生bar图在右侧left_inches=5,top_inches=1.5,width_inches=4,height_inches=3，"
                    "categories=[Alpha,Beta],series=[{name:Amount,values:[10,20]}]；"
                    "speaker_notes为REAL_CLOUD_NOTES_" + marker +
                    "。不要调用todo或question。每次成功再下一个，如工具错误就直接报告错误并结束。"
                    "完成后调用present_file展示文件再简短报告。", workspace=workspace)
                evidence["rounds"].append(result); save()
                for filename in (("cloud-budget.xlsx", "cloud-update.pptx") if args.stage == "advanced" else ("cloud-update.pptx",)):
                    verified = await verify_file(client, result["session_id"], filename, workspace, advanced=True)
                    evidence["files"].append(verified); save()
                    print("Real advanced Office verified: " + filename, flush=True)
            if args.stage in {"all", "templates"}:
                await template_round(client, args.model, workspace, marker, evidence, save)
            requests = [row for row in (await checked(client, "GET", "/api/cloud/usage/requests")).json()
                        if row["id"] not in before and row["source"] == "agent"]
            accounting = {"requests": len(requests), "states": dict(Counter(row["state"] for row in requests)),
                "total_tokens": sum(row["total_tokens"] for row in requests)}
            if not requests or any(row["state"] != "settled" or row["total_tokens"] < 1 for row in requests):
                raise RuntimeError("Office model receipts are not completely settled")
            evidence.setdefault("stage_accounting", {})[args.stage] = accounting
            evidence.setdefault("completed_stages", []).append(args.stage)
            evidence["complete"] = {"cloud-report.docx", "cloud-budget.xlsx", "cloud-update.pptx", "signed-brief.docx",
                "signed-tracker.xlsx", "signed-update.pptx", "uploaded-brief.docx"}.issubset({f["filename"] for f in evidence["files"]})
            save()
            print(json.dumps({"evidence": str(evidence_path), "files": len(evidence["files"]),
                              "accounting": accounting, "complete": evidence["complete"]}, ensure_ascii=False), flush=True)
        except BaseException as error:
            evidence["failure"] = str(error) if isinstance(error, RuntimeError) else type(error).__name__
            save()
            print("Private evidence saved: " + str(evidence_path), flush=True)
            raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="deepseek-office")
    parser.add_argument("--stage", choices=["basic", "advanced", "presentation", "templates", "all"], default="basic")
    parser.add_argument("--account", choices=["admin", "ordinary"], default="ordinary")
    parser.add_argument("--run-id")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except (RuntimeError, httpx.HTTPError) as error:
        parser.exit(1, "Real Office verification failed: " + (str(error) if isinstance(error, RuntimeError) else type(error).__name__) + "\n")
