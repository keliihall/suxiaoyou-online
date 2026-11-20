"""Offline contract tests only: temporary SQLite/files and httpx.MockTransport."""
from __future__ import annotations

import base64
import copy
from contextlib import closing
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import time

import httpx

SPEC = importlib.util.spec_from_file_location("acceptance_results2", Path(__file__).with_name("acceptance-results2.py"))
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)
RUN_ID = "a" * 32
WORKSPACE = Path("/tenant/workspaces/acceptance-results2-" + RUN_ID)


def message(identity, data, parts=None, session_id="new-session"):
    return {"id": identity, "session_id": session_id, "time_created": "2026-11-17T00:00:00Z",
            "data": data, "parts": parts or []}


def part(identity, data, message_id="answer"):
    return {"id": identity, "message_id": message_id, "session_id": "new-session",
            "time_created": "2026-11-17T00:00:00Z", "data": data}


def memory(identity="memory"):
    return message(identity, {"role": "assistant", "agent": "memory", "system": True, "cost": 0.01,
                              "tokens": {"input": 11, "output": 4, "total": 15},
                              "model_id": "cloud-model", "provider_id": "custom_cloud"})


def history(workspace=WORKSPACE):
    parts = [part("write-" + str(index), {"type": "tool", "tool": "write", "call_id": "write-" + str(index),
                 "state": {"status": "completed", "input": {"file_path": str(workspace / name), "content": value}}})
             for index, (name, value) in enumerate(helper.expected_values(RUN_ID).items())]
    parts += [part("present", {"type": "tool", "tool": "present_file", "call_id": "present",
                  "state": {"status": "completed", "input": {"file_path": str(workspace / "result.md")}}}),
              part("finish", {"type": "step-finish", "reason": "stop", "tokens": {"input": 10, "output": 2, "reasoning": 3, "total": 15}}),
              part("text", {"type": "text", "text": "OK " + RUN_ID})]
    rows = [message("user", {"role": "user", "agent": "build"}),
            message("answer", {"role": "assistant", "agent": "build", "finish": "stop"}, parts), memory()]
    return {"total": len(rows), "offset": 0, "messages": rows}


def make_database(path):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, data JSON, time_created TEXT, time_updated TEXT)")
        db.execute("CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, data JSON, time_created TEXT, time_updated TEXT)")
        db.execute("INSERT INTO message VALUES (?,?,?,?,?)", ("original", "old-session", json.dumps({"role": "user"}), "old", "old"))
        db.execute("INSERT INTO part VALUES (?,?,?,?,?,?)", ("original-part", "original", "old-session", json.dumps({"type": "text", "text": "private old content"}), "old", "old"))


def insert_messages(path, rows):
    with closing(sqlite3.connect(path)) as db, db:
        for row in rows:
            db.execute("INSERT INTO message VALUES (?,?,?,?,?)", (row["id"], row["session_id"], json.dumps(row["data"]), row["time_created"], row["time_created"]))
            for p in row["parts"]:
                db.execute("INSERT INTO part VALUES (?,?,?,?,?,?)", (p["id"], p["message_id"], p["session_id"], json.dumps(p["data"]), p["time_created"], p["time_created"]))


class ReplyTests(unittest.TestCase):
    def test_prompt_final_string_is_unambiguous_json_and_exact_policy(self):
        body, values = helper.prompt_body(RUN_ID, WORKSPACE, "deepseek-office")
        self.assertIn('\n"OK ' + RUN_ID + '"\n', body["text"])
        self.assertNotIn("Then return exactly OK", body["text"])
        self.assertEqual(body["client_request_id"], "results2-" + RUN_ID)
        self.assertTrue(body["reasoning"])
        self.assertEqual(body["permission_presets"], {"file_changes": True, "run_commands": False})
        self.assertEqual(len(values), 3)
        rules = body["permission_rules"]
        self.assertEqual(rules[0], {"action": "deny", "permission": "*", "pattern": "*"})
        self.assertEqual(rules[-1], {"action": "allow", "permission": "present_file", "pattern": str(WORKSPACE / "result.md")})

    def test_final_reply_ignores_late_hidden_memory_accounting(self):
        result = helper.verify_task(history(), RUN_ID, WORKSPACE)
        self.assertEqual(result["final_assistant_id"], "answer")
        self.assertTrue(result["final_reply_exact"])
        self.assertEqual(result["task_step_usage"]["total"], 15)
        value = history()
        value["messages"].append(message("hidden-system", {"role": "assistant", "system": True}, [part("hidden-text", {"type": "text", "text": "hidden"})]))
        value["total"] += 1
        self.assertEqual(helper.verify_task(value, RUN_ID, WORKSPACE)["visible_history_sha256"], result["visible_history_sha256"])

    def test_incorrect_punctuation_quotes_or_extra_reply_fails(self):
        for text in ("OK " + RUN_ID + ".", '"OK ' + RUN_ID + '"', "OK", "OK " + RUN_ID + " more"):
            value = history()
            value["messages"][1]["parts"][-1]["data"]["text"] = text
            with self.assertRaisesRegex(helper.Failure, "final_reply_mismatch"):
                helper.verify_task(value, RUN_ID, WORKSPACE)

    def test_visible_late_assistant_is_not_silently_ignored(self):
        value = history()
        value["messages"].append(message("late", {"role": "assistant"}))
        value["total"] += 1
        with self.assertRaisesRegex(helper.Failure, "final_reply_mismatch"):
            helper.verify_task(value, RUN_ID, WORKSPACE)

    def test_task_and_tool_errors_fail_even_if_final_reply_is_correct(self):
        for mutation, code in (("step-error", "task_terminal_error"), ("message-error", "task_terminal_error"),
                               ("present-error", "tool_not_completed")):
            value = history()
            if mutation == "step-error":
                value["messages"][1]["parts"].insert(0, part("bad", {"type": "step-error", "error_code": "model_output_exhausted"}))
            elif mutation == "message-error":
                value["messages"][1]["data"]["error"] = "private upstream body"
            else:
                value["messages"][1]["parts"][3]["data"]["state"]["status"] = "error"
            with self.assertRaisesRegex(helper.Failure, code):
                helper.verify_task(value, RUN_ID, WORKSPACE)
            evidence = helper.terminal_evidence(value)
            self.assertNotIn("private", json.dumps(evidence))
        self.assertEqual(helper.terminal_evidence(value)["terminal_safe_codes"], ["tool_execution_failed"])

    def test_wrong_tool_target_or_content_and_incomplete_page_fail(self):
        for field, content, code in (("file_path", str(WORKSPACE / "elsewhere.md"), "write_target_mismatch"),
                                      ("content", "wrong", "write_input_mismatch")):
            value = history()
            value["messages"][1]["parts"][0]["data"]["state"]["input"][field] = content
            with self.assertRaisesRegex(helper.Failure, code):
                helper.verify_task(value, RUN_ID, WORKSPACE)
        value = history()
        value["total"] = 201
        with self.assertRaisesRegex(helper.Failure, "incomplete_history_page"):
            helper.verify_task(value, RUN_ID, WORKSPACE)


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "tenant.db"
        make_database(self.path)
        self.before = helper.transcript_snapshot(self.path)

    def test_actual_readonly_sqlite_full_rows_allow_only_empty_memory_accounting(self):
        insert_messages(self.path, [memory()])
        after = helper.transcript_snapshot(self.path)
        result = helper.stable_snapshot(self.before, after)
        self.assertEqual(result["added_memory_accounting_ids"], ["memory"])
        self.assertEqual(result["added_memory_accounting_count"], 1)
        self.assertEqual(self.before["part"], after["part"])
        report = helper.snapshot_evidence(after)
        self.assertNotIn("private old content", json.dumps(report))
        self.assertIn("original", report["message"]["row_sha256_by_id"])
        self.assertEqual(len(report["message"]["sha256"]), 64)

    def test_any_original_field_mutation_or_deletion_is_detected(self):
        for table in ("message", "part"):
            with self.subTest(table=table):
                with closing(sqlite3.connect(self.path)) as db, db:
                    db.execute("UPDATE " + table + " SET time_updated = 'new'")
                with self.assertRaisesRegex(helper.Failure, "original_" + table + "_changed_or_missing"):
                    helper.stable_snapshot(self.before, helper.transcript_snapshot(self.path))
                with closing(sqlite3.connect(self.path)) as db, db:
                    db.execute("UPDATE " + table + " SET time_updated = 'old'")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DELETE FROM part")
        with self.assertRaisesRegex(helper.Failure, "original_part_changed_or_missing"):
            helper.stable_snapshot(self.before, helper.transcript_snapshot(self.path))

    def test_new_parts_under_memory_shell_are_rejected(self):
        row = memory()
        row["parts"] = [part("new-content", {"type": "text", "text": "cannot hide content"}, "memory")]
        insert_messages(self.path, [row])
        with self.assertRaisesRegex(helper.Failure, "unexpected_new_part_after_finish"):
            helper.stable_snapshot(self.before, helper.transcript_snapshot(self.path))

    def test_memory_system_flags_alone_and_unknown_payload_are_not_an_allowlist(self):
        cases = [message("extra", {"role": "assistant", "system": True}),
                 message("extra", {"role": "assistant", "agent": "memory"}), memory("extra"), memory("extra")]
        cases[-2]["data"]["error"] = "private error"
        cases[-1]["data"]["cost"] = float("nan")
        for row in cases:
            with self.subTest(data=row["data"]):
                insert_messages(self.path, [row])
                with self.assertRaisesRegex(helper.Failure, "unexpected_new_message_after_finish"):
                    helper.stable_snapshot(self.before, helper.transcript_snapshot(self.path))
                with closing(sqlite3.connect(self.path)) as db, db:
                    db.execute("DELETE FROM message WHERE id = 'extra'")

    def test_visible_hash_changes_for_real_message_part_or_order_changes(self):
        value = history()
        baseline = helper.visible_history_hash(value)
        changed = copy.deepcopy(value)
        changed["messages"][-1]["data"]["tokens"]["input"] += 99
        self.assertEqual(helper.visible_history_hash(changed), baseline)
        changed["messages"][1]["parts"][-1]["data"]["text"] += " changed"
        self.assertNotEqual(helper.visible_history_hash(changed), baseline)


class DownloadTests(unittest.TestCase):
    def response(self):
        raw = helper.expected_values(RUN_ID)["result.md"].encode()
        return {"name": "result.md", "path": str(WORKSPACE / "result.md"), "size": len(raw),
                "content_base64": base64.b64encode(raw).decode()}

    def check(self, response=None, status=200):
        calls = []
        def handle(request):
            calls.append((request.method, request.url.path))
            self.assertEqual(json.loads(request.content), {"path": str(WORKSPACE / "result.md"), "workspace": str(WORKSPACE)})
            return httpx.Response(status, json=self.response() if response is None else response,
                                  headers={"Location": "https://elsewhere.invalid/"})
        with httpx.Client(base_url="http://127.0.0.1:8080", transport=httpx.MockTransport(handle),
                          follow_redirects=False, trust_env=False) as client:
            try:
                return helper.verify_download(client, WORKSPACE, RUN_ID, deadline=time.monotonic() + 30), calls
            finally:
                self.assertEqual(calls, [("POST", "/api/files/content-binary")])

    def test_exact_online_download_proves_bytes_and_hash_without_printing_body(self):
        result, calls = self.check()
        self.assertEqual(result["sha256"], hashlib.sha256(helper.expected_values(RUN_ID)["result.md"].encode()).hexdigest())
        self.assertTrue(result["exact_content"])
        self.assertNotIn("SUXIAOYOU_RESULT_INTEGRITY", json.dumps(result))
        self.assertNotIn(str(WORKSPACE), json.dumps(result))
        self.assertEqual(result["requests"], 1)

    def test_tampered_bytes_encoding_metadata_fail(self):
        for field, value in (("content_base64", base64.b64encode(b"wrong").decode()),
                             ("content_base64", "@@@"), ("path", "/different/result.md"),
                             ("name", "draft.md"), ("size", 1)):
            response = self.response()
            response[field] = value
            with self.subTest(field=field), self.assertRaises(helper.Failure):
                self.check(response)

    def test_http_error_or_redirect_never_retries_or_follows(self):
        for status in (302, 401, 500):
            with self.subTest(status=status), self.assertRaisesRegex(helper.Failure, "runtime_http_failed"):
                self.check(status=status)


class OnceGuardTests(unittest.TestCase):
    def test_guard_is_exclusive_durable_and_ambiguous_dispatch_cannot_repeat(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            folder = root / "result-integrity-acceptance-v2"
            args = SimpleNamespace(run_id=RUN_ID, route="deepseek-office")
            calls = []
            def unknown_runner(args, path, report):
                report.update(phase="prompt_dispatched_unknown", prompt_dispatches=1)
                helper.private_write(path, report)
                calls.append("POST")
                raise httpx.ReadTimeout("private error containing token")
            with patch.object(helper.os, "fsync", wraps=helper.os.fsync) as sync:
                first, status = helper.execute(args, folder, runner=unknown_runner)
            self.assertGreaterEqual(sync.call_count, 6)  # directory + guard + before POST + terminal state
            self.assertEqual(status, 1)
            self.assertEqual(first["prompt_dispatches"], 1)
            self.assertEqual(first["failure"], "acceptance_operation_failed")
            state = folder / (RUN_ID + ".json")
            before = state.read_bytes()
            second, status = helper.execute(args, folder, runner=unknown_runner)
            self.assertEqual(status, 2)
            self.assertEqual(second["phase"], "already_claimed_no_dispatch")
            self.assertEqual(calls, ["POST"])
            self.assertEqual(state.read_bytes(), before)
            self.assertEqual(state.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("private error", before.decode())

    def test_guard_never_follows_symlink_and_keeps_original_failed_guard(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            old = root / "result-integrity-acceptance"
            old.mkdir(mode=0o700)
            original = old / (RUN_ID + ".json")
            original.write_text('{"phase":"failed_or_unknown"}')
            before = original.read_bytes()
            folder = root / "result-integrity-acceptance-v2"
            args = SimpleNamespace(run_id=RUN_ID, route="deepseek-office")
            helper.execute(args, folder, runner=lambda args, path, report: {**report, "ok": True})
            self.assertEqual(original.read_bytes(), before)
            linked = root / "linked"
            linked.symlink_to(folder, target_is_directory=True)
            with self.assertRaisesRegex(helper.Failure, "linked_acceptance_directory"):
                helper.execute(args, linked)

    def test_actual_prompt_transport_timeout_is_dispatched_once_and_never_retried(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            workspace_root = root / "workspaces"
            workspace_root.mkdir()
            database = root / "tenant.db"
            make_database(database)
            calls = []
            def handle(request):
                calls.append((request.method, request.url.path))
                if request.method == "GET":
                    return httpx.Response(200, json=[])
                raise httpx.ReadTimeout("private upstream failure", request=request)
            client = httpx.Client(base_url="http://127.0.0.1:8080", transport=httpx.MockTransport(handle))
            args = SimpleNamespace(run_id=RUN_ID, route="deepseek-office")
            with patch("httpx.Client", return_value=client), patch.object(helper, "DATABASE", database), \
                 patch.object(helper, "runtime_token", return_value="private-not-printed"), \
                 patch.dict(helper.os.environ, {"CLOUD_ENGINE_WORKSPACE_ROOT": str(workspace_root)}):
                first, status = helper.execute(args, root / "result-integrity-acceptance-v2")
                self.assertEqual(status, 1)
                helper.execute(args, root / "result-integrity-acceptance-v2")
            self.assertEqual(calls, [("GET", "/api/chat/active"), ("POST", "/api/chat/prompt")])
            self.assertEqual(first["prompt_dispatches"], 1)
            self.assertEqual(first["phase"], "failed_or_unknown")


class EndToEndOfflineTests(unittest.TestCase):
    def test_background_delivery_download_peer_isolation_and_three_stable_reads(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            workspace_root = root / "workspaces"
            workspace_root.mkdir()
            workspace = workspace_root / ("acceptance-results2-" + RUN_ID)
            database = root / "tenant.db"
            make_database(database)
            current = history(workspace)
            calls, sleeps = [], []
            def files(names):
                return {"files": [{"name": name, "path": str(workspace / name)} for name in names]}
            def handle(request):
                calls.append((request.method, request.url.path, request.url.query.decode()))
                path = request.url.path
                if path == "/api/chat/active":
                    return httpx.Response(200, json=[])
                if path == "/api/chat/prompt":
                    guard = json.loads((root / "result-integrity-acceptance-v2" / (RUN_ID + ".json")).read_text())
                    self.assertEqual(guard["phase"], "prompt_dispatched_unknown")
                    self.assertEqual(guard["prompt_dispatches"], 1)
                    self.assertEqual(request.method, "POST")
                    self.assertEqual(json.loads(request.content)["client_request_id"], "results2-" + RUN_ID)
                    for name, content in helper.expected_values(RUN_ID).items():
                        (workspace / name).write_text(content)
                    insert_messages(database, current["messages"])
                    return httpx.Response(200, json={"session_id": "new-session", "stream_id": "new-stream"})
                if path == "/api/sessions":
                    self.assertEqual(json.loads(request.content)["directory"], str(workspace))
                    return httpx.Response(201, json={"id": "peer-session"})
                if path == "/api/messages/new-session":
                    return httpx.Response(200, json=current)
                if path == "/api/messages/peer-session":
                    return httpx.Response(200, json={"total": 0, "offset": 0, "messages": []})
                if path == "/api/sessions/new-session/files":
                    return httpx.Response(200, json=files(helper.expected_values(RUN_ID) if request.url.query else ["result.md"]))
                if path == "/api/sessions/peer-session/files":
                    return httpx.Response(200, json=files([]))
                if path == "/api/files/content-binary":
                    raw = (workspace / "result.md").read_bytes()
                    return httpx.Response(200, json={"name": "result.md", "path": str(workspace / "result.md"),
                                                   "size": len(raw), "content_base64": base64.b64encode(raw).decode()})
                self.fail("Unexpected route: " + path)
            def sleep(seconds):
                sleeps.append(seconds)
                if len(sleeps) == 1:
                    row = memory("late-memory")
                    insert_messages(database, [row])
                    current["messages"].append(row)
                    current["total"] += 1
            client = httpx.Client(base_url="http://127.0.0.1:8080", transport=httpx.MockTransport(handle))
            args = SimpleNamespace(run_id=RUN_ID, route="deepseek-office")
            with patch("httpx.Client", return_value=client), patch.object(helper, "DATABASE", database), \
                 patch.object(helper, "runtime_token", return_value="private-not-printed"), \
                 patch.object(helper.time, "sleep", side_effect=sleep), \
                 patch.dict(helper.os.environ, {"CLOUD_ENGINE_WORKSPACE_ROOT": str(workspace_root)}):
                result, status = helper.execute(args, root / "result-integrity-acceptance-v2")
            self.assertEqual(status, 0, result)
            self.assertTrue(result["ok"])
            self.assertEqual(sleeps, [11, 11, 11])
            self.assertEqual(result["stability_reads"], 3)
            self.assertEqual([row["added_memory_accounting_count"] for row in result["stability"]], [1, 1, 1])
            self.assertTrue(result["original_pre_task_rows_unchanged"])
            self.assertTrue(result["visible_history_unchanged"])
            self.assertEqual(result["sse_subscriptions"], 0)
            self.assertEqual(result["successful_writes"], 3)
            self.assertEqual(result["successful_presentations"], 1)
            self.assertEqual(result["download"]["sha256"], result["file_sha256"]["result.md"])
            self.assertEqual(sum(method == "POST" and path == "/api/chat/prompt" for method, path, query in calls), 1)
            self.assertEqual(sum(method == "POST" and path == "/api/sessions" for method, path, query in calls), 1)
            self.assertEqual(sum(method == "POST" and path == "/api/files/content-binary" for method, path, query in calls), 1)
            self.assertFalse(any("stream" in path for method, path, query in calls))
            text = json.dumps(result)
            for private in ("private-not-printed", "private old content", "SUXIAOYOU_RESULT_INTEGRITY", str(workspace)):
                self.assertNotIn(private, text)


if __name__ == "__main__":
    unittest.main()
