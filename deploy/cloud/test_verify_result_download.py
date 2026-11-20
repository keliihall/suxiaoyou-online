"""Offline HTTP read-route checks; requires the existing backend httpx dependency."""
import base64
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

import httpx

SPEC = importlib.util.spec_from_file_location("verify_result_download", Path(__file__).with_name("verify-result-download.py"))
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


class DownloadTests(unittest.TestCase):
    run_id = "a" * 32
    workspace = Path("/tenant/workspaces/acceptance-results-" + run_id)

    def report(self):
        return {"run_id": self.run_id, "ok": True, "phase": "passed", "session_id": "new-owning-session",
                "prompt_dispatches": 1, "peer_session_dispatches": 1, "successful_writes": 3,
                "successful_presentations": 1, "final_reply_exact": True, "peer_session_isolated": True,
                "stability_reads": 3, "stable_seconds_at_least": 33,
                "deliverables": ["result.md"], "all_files": ["draft.md", "process-notes.json", "result.md"],
                "file_sha256": {"result.md": hashlib.sha256(verifier.expected_content(self.run_id)).hexdigest()}}

    def response(self):
        raw = verifier.expected_content(self.run_id)
        return {"name": "result.md", "path": str(self.workspace / "result.md"), "size": len(raw),
                "content_base64": base64.b64encode(raw).decode(), "mime_type": "text/markdown"}

    def client(self, calls, *, response=None, status=200):
        def handle(request):
            calls.append((request.method, request.url.path))
            if request.method == "GET":
                self.assertEqual(request.url.path, "/api/sessions/new-owning-session/files")
                return httpx.Response(200, json={"files": [{"name": "result.md", "path": str(self.workspace / "result.md")}]})
            self.assertEqual((request.method, request.url.path), ("POST", "/api/files/content-binary"))
            self.assertEqual(json.loads(request.content), {"path": str(self.workspace / "result.md"), "workspace": str(self.workspace)})
            return httpx.Response(status, json=self.response() if response is None else response,
                                  headers={"Location": "https://unrelated.invalid/"} if status == 302 else {})
        return httpx.Client(base_url="http://127.0.0.1:8080", transport=httpx.MockTransport(handle),
                            trust_env=False, follow_redirects=False)

    def test_download_uses_exact_online_read_routes_and_reports_only_hashes(self):
        calls = []
        with self.client(calls) as client:
            value = verifier.verify_download(client, self.report(), self.run_id, self.workspace)
        self.assertTrue(value["ok"])
        self.assertEqual(value["sha256"], self.report()["file_sha256"]["result.md"])
        self.assertEqual((value["download_requests"], value["model_requests"]), (1, 0))
        self.assertEqual(calls, [("GET", "/api/sessions/new-owning-session/files"), ("POST", "/api/files/content-binary")])
        text = json.dumps(value)
        self.assertNotIn("SUXIAOYOU_RESULT_INTEGRITY", text)
        self.assertNotIn(str(self.workspace), text)
        self.assertNotIn("content_base64", text)

    def test_unfinished_or_mismatched_report_is_rejected_before_http(self):
        for field, value in (("phase", "running_without_sse"), ("stability_reads", 2), ("successful_presentations", 2), ("run_id", "b" * 32)):
            report = self.report()
            report[field] = value
            calls = []
            with self.client(calls) as client, self.assertRaises(verifier.Failure):
                verifier.verify_download(client, report, self.run_id, self.workspace)
            self.assertEqual(calls, [])

    def test_corrupt_bytes_encoding_or_wrong_identity_is_rejected(self):
        for field, value in (("content_base64", base64.b64encode(b"wrong").decode()), ("content_base64", "@@bad@@"),
                             ("path", "/different/result.md"), ("size", 999), ("name", "draft.md")):
            response = copy.deepcopy(self.response())
            response[field] = value
            calls = []
            with self.client(calls, response=response) as client, self.assertRaises(verifier.Failure):
                verifier.verify_download(client, self.report(), self.run_id, self.workspace)
            self.assertEqual(len(calls), 2)

    def test_redirect_and_http_failure_do_not_retry_or_follow(self):
        for status in (302, 401, 500):
            calls = []
            with self.client(calls, status=status) as client, self.assertRaises(verifier.Failure):
                verifier.verify_download(client, self.report(), self.run_id, self.workspace)
            self.assertEqual(sum(method == "POST" for method, _ in calls), 1)
            self.assertEqual(len(calls), 2)

    def test_private_report_is_read_without_changes_and_rejects_nonprivate_mode(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / (self.run_id + ".json")
            path.write_text(json.dumps(self.report()))
            path.chmod(0o600)
            before = path.read_bytes()
            self.assertEqual(verifier.read_report(path), self.report())
            self.assertEqual(path.read_bytes(), before)
            path.chmod(0o644)
            with self.assertRaises(verifier.Failure):
                verifier.read_report(path)


if __name__ == "__main__":
    unittest.main()
