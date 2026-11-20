"""Zero-network checks for the paid acceptance runner and original wire schemas."""
from __future__ import annotations

import ast
import asyncio
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from app.schemas.agent import PermissionRule, Ruleset
from app.streaming.events import SSEEvent

spec = importlib.util.spec_from_file_location("deployment_smoke", Path(__file__).with_name("deployment-smoke.py"))
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class AcceptanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.output = io.BytesIO()
        # save() needs a real private descriptor for fsync.
        output_path = Path(self.folder.name) / "report.json"
        fd = os.open(output_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        self.output = os.fdopen(fd, "w+b")
        self.addCleanup(self.output.close)
        self.acceptance = smoke.Acceptance(SimpleNamespace(), {
            "origin": "https://acceptance.invalid", "email": "test@example.invalid",
            "password": "PRIVATE_PASSWORD_EXAMPLE", "model_id": "deepseek-office"},
            {"cookie": "PRIVATE_COOKIE_EXAMPLE"}, self.output)
        self.acceptance.report["session_id"] = "conversation_123"

    def test_private_credentials_allow_unused_password(self):
        path = Path(self.folder.name) / "credentials.json"
        path.write_text(json.dumps({"origin": "https://acceptance.invalid", "email": "test@example.invalid", "password": "unused"}))
        path.chmod(0o600)
        result = smoke.private_json(path, {"origin", "email", "password"}, {"origin", "email"})
        self.assertEqual(result["email"], "test@example.invalid")
        path.chmod(0o644)
        with self.assertRaises(smoke.SmokeFailure):
            smoke.private_json(path, set(result))
        path.chmod(0o600)
        alias = path.with_name("alias.json")
        alias.symlink_to(path)
        with self.assertRaises(OSError):
            smoke.private_json(alias, set(result))

    def test_https_origin_rejects_credentials_paths_and_http(self):
        self.assertEqual(smoke.checked_origin("https://192.168.1.211:8443"), "https://192.168.1.211:8443")
        for origin in ("http://192.168.1.211:8443", "https://u:p@server", "https://server/", "https://server/#/session", "https://server?token=x"):
            with self.subTest(origin=origin), self.assertRaises(smoke.SmokeFailure):
                smoke.checked_origin(origin)

    def test_exact_readonly_cat(self):
        workspace = self.acceptance.workspace
        correct = {"command": f"cat -- {workspace}/sentinel.txt", "cwd": workspace, "timeout": 10}
        self.assertTrue(smoke.allowed_cat(correct, workspace))
        for changes in ({"command": correct["command"] + "; pwd"}, {"command": "cat sentinel.txt"},
                        {"cwd": "/tenant"}, {"timeout": float("nan")}, {"timeout": True},
                        {"timeout": 16}, {"description": "extra field"}):
            self.assertFalse(smoke.allowed_cat({**correct, **changes}, workspace))

    def test_rules_keep_original_registry_tools_visible_and_targets_bounded(self):
        workspace = self.acceptance.workspace
        rules = Ruleset(rules=[PermissionRule(**item) for item in smoke.acceptance_rules(workspace)])
        for tool, filename in (("write", "result.txt"), ("office", "result.docx")):
            self.assertEqual(rules.evaluate(tool, "*"), "ask")
            self.assertEqual(rules.evaluate(tool, workspace + "/" + filename), "allow")
            self.assertEqual(rules.evaluate(tool, workspace + "/unexpected.txt"), "ask")
        self.assertEqual(rules.evaluate("bash", "*"), "ask")
        self.assertEqual(rules.evaluate("code_execute", "*"), "deny")

    def test_original_producers_match_client_field_assumptions(self):
        source = ast.parse((ROOT / "backend/app/session/processor.py").read_text())
        required = {
            "PERMISSION_REQUEST": {"call_id", "tool_call_id", "tool", "permission", "arguments", "arguments_truncated"},
            "TOOL_START": {"call_id", "tool", "arguments"},
            "TOOL_RESULT": {"call_id", "tool", "output", "metadata"},
        }
        for event, keys in required.items():
            candidates = [node for node in ast.walk(source) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name) and node.func.id == "SSEEvent" and len(node.args) >= 2
                and isinstance(node.args[0], ast.Name) and node.args[0].id == event and isinstance(node.args[1], ast.Dict)]
            self.assertTrue(any(keys <= {key.value for key in node.args[1].keys if isinstance(key, ast.Constant)}
                                for node in candidates), event)
        permission_function = next(n for n in ast.walk(source) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_ask_permission")
        payload = next(n.args[1] for n in ast.walk(permission_function) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name) and n.func.id == "SSEEvent")
        mapping = {key.value: value for key, value in zip(payload.keys, payload.values)}
        for field in ("tool", "permission"):
            self.assertIsInstance(mapping[field], ast.Name)
            self.assertEqual(mapping[field].id, "tool_name")
        files = ast.parse((ROOT / "backend/app/api/files.py").read_text())
        function = next(n for n in ast.walk(files) if isinstance(n, ast.AsyncFunctionDef) and n.name == "get_file_content_binary")
        fields = next({key.value for key in n.value.keys} for n in ast.walk(function) if isinstance(n, ast.Return) and isinstance(n.value, ast.Dict))
        self.assertTrue({"content_base64", "size", "path"} <= fields)

    def test_single_prompt_submission_site_and_no_authentication_mutation(self):
        source = ast.parse(Path(__file__).with_name("deployment-smoke.py").read_text())
        paths = [node.value for node in ast.walk(source) if isinstance(node, ast.Constant)
                 and isinstance(node.value, str) and node.value.startswith("/api/")]
        self.assertEqual(paths.count("/api/chat/prompt"), 1)
        self.assertFalse(any(path.startswith("/api/cloud/auth/") and path != "/api/cloud/auth/me" for path in paths))

    async def test_original_wire_permission_precedes_tool_start(self):
        acceptance = self.acceptance
        workspace = acceptance.workspace
        requests = []
        events = [SSEEvent("permission-request", {"call_id": "permission_1", "tool_call_id": "bash_1",
            "tool": "bash", "permission": "bash", "arguments_truncated": False,
            "arguments": {"command": f"cat -- {workspace}/sentinel.txt", "cwd": workspace, "timeout": 10}})]
        for name, filename in (("bash", None), ("write", "result.txt"), ("office", "result.docx")):
            arguments = ({"command": f"cat -- {workspace}/sentinel.txt", "timeout": 10} if name == "bash"
                else {"file_path": workspace + "/" + filename, "operation": "create"})
            events.extend([SSEEvent("tool-call", {"call_id": name + "_1", "tool": name, "arguments": arguments}),
                SSEEvent("tool-result", {"call_id": name + "_1", "tool": name,
                    "output": acceptance.marker if name == "bash" else "completed", "metadata": {"exit_code": 0}})])
        events.append(SSEEvent("done", {"finish_reason": "stop", "session_id": "conversation_123"}))

        def handler(request):
            requests.append((request.method, request.url.path))
            if request.url.path == "/api/chat/respond":
                self.assertEqual(json.loads(request.content)["response"], {"allowed": True, "remember": False})
                return httpx.Response(200, json={"ok": True})
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content="".join(event.encode() for event in events))

        async with httpx.AsyncClient(base_url="https://acceptance.invalid", transport=httpx.MockTransport(handler)) as client:
            await acceptance.stream(client, {"stream_id": "stream_123", "session_id": "conversation_123"})
        self.assertEqual(requests, [("GET", "/api/chat/stream/stream_123"), ("POST", "/api/chat/respond")])
        self.assertTrue(acceptance.report["checks"]["sse_completed"])

    async def test_unexpected_permission_is_not_approved(self):
        requests = []

        def handler(request):
            requests.append(request.url.path)
            return httpx.Response(200, json={"status": "aborted"})

        self.acceptance.stream_id = "stream_123"
        async with httpx.AsyncClient(base_url="https://acceptance.invalid", transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(smoke.SmokeFailure):
                await self.acceptance.event(client, "permission-request", {"tool": "bash", "permission": "bash",
                    "arguments": {"command": "cat /etc/passwd"}, "call_id": "p1", "tool_call_id": "b1"})
            await self.acceptance.abort(client)
        self.assertEqual(requests, ["/api/chat/abort"])

    async def test_lost_prompt_response_only_aborts_own_known_conversation(self):
        self.acceptance.prompt_count = 1
        requests = []

        def handler(request):
            requests.append((request.url.path, request.content))
            if request.url.path == "/api/chat/active":
                return httpx.Response(200, json=[{"stream_id": "foreign", "session_id": "other"},
                    {"stream_id": "own", "session_id": "conversation_123"}])
            self.assertEqual(json.loads(request.content), {"stream_id": "own"})
            return httpx.Response(200, json={"status": "aborted"})

        async with httpx.AsyncClient(base_url="https://acceptance.invalid", transport=httpx.MockTransport(handler)) as client:
            await self.acceptance.abort(client)
        self.assertEqual([path for path, _ in requests], ["/api/chat/active", "/api/chat/abort"])
        self.assertEqual(self.acceptance.prompt_count, 1)

    def test_private_report_redacts_authentication_values(self):
        self.acceptance.csrf = "PRIVATE_CSRF_EXAMPLE"
        self.acceptance.report["unexpected_server_value"] = "PRIVATE_COOKIE_EXAMPLE PRIVATE_PASSWORD_EXAMPLE PRIVATE_CSRF_EXAMPLE"
        self.acceptance.save()
        self.output.seek(0)
        data = self.output.read()
        for secret in (b"PRIVATE_COOKIE_EXAMPLE", b"PRIVATE_PASSWORD_EXAMPLE", b"PRIVATE_CSRF_EXAMPLE"):
            self.assertNotIn(secret, data)


if __name__ == "__main__":
    unittest.main()
