"""Pure HTTP contract tests for existing-output reads; no model or server."""
import base64
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch

import httpx

SPEC = importlib.util.spec_from_file_location("preview_content", Path(__file__).with_name("verify-file-preview-content.py"))
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


class ExistingContentTests(unittest.TestCase):
    def client(self, calls, *, wrong_text=False, wrong_binary=False, wrong_scope=False, status=200, large=False):
        def handle(request):
            path = request.url.raw_path.decode()
            pair = (request.method, path)
            calls.append(pair)
            self.assertIn(pair, verifier.ALLOWED)
            if large:
                return httpx.Response(200, content=b"x" * (verifier.MAX_BYTES + 1))
            if status != 200:
                return httpx.Response(status, headers={"Location": "https://unrelated.invalid/"})
            if request.method == "GET":
                names = ["result.md"] if path == verifier.SESSION_FILES else ["draft.md", "process-notes.json", "result.md"]
                if wrong_scope:
                    names.append("other-session.md")
                return httpx.Response(200, json={"files": [{"name": name, "path": verifier.WORKSPACE + "/" + name} for name in names]})
            self.assertEqual(json.loads(request.content), verifier.BODY)
            value = {"name": "result.md", "path": verifier.FILE_PATH, "size": verifier.EXPECTED_SIZE}
            if path == verifier.CONTENT:
                value["content"] = "wrong" if wrong_text else verifier.EXPECTED.decode()
            else:
                value["content_base64"] = base64.b64encode(b"wrong" if wrong_binary else verifier.EXPECTED).decode()
            return httpx.Response(200, json=value)
        return httpx.Client(base_url="http://127.0.0.1:8080", transport=httpx.MockTransport(handle),
                            trust_env=False, follow_redirects=False)

    def test_exact_four_real_read_endpoints_and_safe_hash_only_result(self):
        calls = []
        with self.client(calls) as client:
            result = verifier.verify_existing_content(client)
        self.assertEqual(calls, list(verifier.ALLOWED))
        verifier.validate_result(result)
        self.assertEqual((result["model_requests"], result["file_writes"], result["created_sessions"]), (0, 0, 0))
        self.assertEqual(result["bytes"], 60)
        self.assertEqual(result["sha256"], "c27613951ecc027af64296b34b93b40a1a83214cb7a2af5bd081fc10094e2bf3")
        serialized = json.dumps(result)
        for private in (verifier.FILE_PATH, verifier.EXPECTED.decode(), "content_base64", "Authorization", "suxiaoyou_st_"):
            self.assertNotIn(private, serialized)

    def test_unapproved_endpoint_method_and_file_body_never_reach_transport(self):
        for method, path, body in (("POST", "/api/chat/prompt", {}), ("POST", "/api/sessions", {}),
                                   ("POST", "/api/files/write", verifier.BODY), ("GET", verifier.CONTENT, None),
                                   ("POST", verifier.CONTENT, {**verifier.BODY, "path": "/other/file.md"}),
                                   ("POST", verifier.BINARY, {"path": verifier.FILE_PATH}),
                                   ("GET", verifier.SESSION_FILES + "?scope=unknown", None),
                                   ("GET", "http://external.invalid" + verifier.SESSION_FILES, None)):
            calls = []
            with self.subTest(method=method, path=path), self.client(calls) as client:
                with self.assertRaises(verifier.Failure) as error:
                    verifier.read_json(client, method, path, body=body, deadline=time.monotonic() + 1)
                self.assertEqual(str(error.exception), "endpoint_not_allowlisted")
            self.assertEqual(calls, [])

    def test_wrong_delivery_text_or_download_fail_without_retry(self):
        for options, count in (({"wrong_scope": True}, 1), ({"wrong_text": True}, 3), ({"wrong_binary": True}, 4)):
            calls = []
            with self.subTest(options=options), self.client(calls, **options) as client:
                with self.assertRaises(verifier.Failure):
                    verifier.verify_existing_content(client)
            self.assertEqual(len(calls), count)

    def test_http_redirect_auth_or_server_failure_never_retries(self):
        for status in (302, 401, 500):
            calls = []
            with self.subTest(status=status), self.client(calls, status=status) as client:
                with self.assertRaises(verifier.Failure):
                    verifier.verify_existing_content(client)
            self.assertEqual(calls, [verifier.ALLOWED[0]])

    def test_body_budget_and_expired_deadline_fail_closed(self):
        calls = []
        with self.client(calls, large=True) as client:
            with self.assertRaises(verifier.Failure) as error:
                verifier.verify_existing_content(client)
            self.assertEqual(str(error.exception), "content_response_too_large")
        self.assertEqual(calls, [verifier.ALLOWED[0]])
        calls = []
        with self.client(calls) as client:
            with self.assertRaises(verifier.Failure):
                verifier.read_json(client, "GET", verifier.SESSION_FILES, deadline=time.monotonic() - 1)
        self.assertEqual(calls, [])

    def test_duplicate_response_json_and_extra_private_result_fields_are_rejected(self):
        with self.assertRaises(verifier.Failure):
            verifier.decode('{"files":[],"files":[]}')
        calls = []
        with self.client(calls) as client:
            result = verifier.verify_existing_content(client)
        with self.assertRaises(verifier.Failure):
            verifier.validate_result({**result, "token": "private"})

    def test_root_execs_fixed_tenant_uid_with_only_reviewed_source_stdin(self):
        calls = []
        with self.client(calls) as client:
            result = verifier.verify_existing_content(client)
        response = SimpleNamespace(returncode=0, stdout=(verifier.MARKER + json.dumps(result)).encode())
        with patch.object(verifier.os, "getuid", return_value=0), patch.object(verifier.os, "geteuid", return_value=0), \
                patch.object(verifier, "source_bytes", return_value=b"# frozen helper\n"), \
                patch.object(verifier.subprocess, "run", return_value=response) as run:
            self.assertEqual(verifier.root_main(), result)
        args, kwargs = run.call_args
        self.assertEqual(args[0][:len(verifier.PREFIX)], verifier.PREFIX)
        self.assertEqual(args[0][len(verifier.PREFIX):len(verifier.PREFIX) + 5], ["exec", "-i", "--user", "10001:10001", verifier.TENANT])
        self.assertEqual(kwargs["input"], b"# frozen helper\n")
        self.assertEqual(kwargs["timeout"], 75)
        self.assertNotIn("CLOUD_ENGINE_AUTH_TOKEN", str(args))
        self.assertNotIn("env", args[0][len(verifier.PREFIX):])


if __name__ == "__main__":
    unittest.main()
