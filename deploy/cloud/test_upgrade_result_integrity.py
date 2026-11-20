"""Offline release contracts. No Docker, network, provider or user data."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parent


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


base = module("result_integrity_base_tests", "test_upgrade_incident.py")
controller = module("result_integrity_controller", "upgrade-result-integrity.py")
runtime = module("result_integrity_runtime", "result-integrity-runtime.py")
acceptance = module("result_integrity_acceptance", "acceptance-results.py")


class ControllerTests(base.IncidentControllerTests):
    def setUp(self):
        p = patch.object(base, "controller", controller)
        p.start()
        self.addCleanup(p.stop)

    def pins(self):
        return patch.multiple(controller, SOURCE_SHA256="a" * 64,
                              HELPER_SHA256="d" * 64,
                              OLD_OVERRIDE_SHA256="b" * 64,
                              EXPECTED_SOURCE_CHANGES={"backend/app/example.py": "c" * 64})

    def test_mixed_previous_release_images_are_pinned(self):
        self.assertEqual(controller.OLD_VERSION, "20261117-composer")
        self.assertEqual(controller.OLD_BACKEND_VERSION, "20261117-incident2")
        self.assertEqual(controller.VERSION, "20261117-results")
        self.assertNotEqual(controller.OLD_CONFIG, controller.OLD_BACKEND_CONFIG)
        self.assertEqual(controller.OLD_IMAGES["api"], "suxiaoyou-cloud-api:20261117-incident2")
        self.assertEqual(controller.OLD_IMAGES["runtime"], "suxiaoyou-online-runtime:20261117-incident2")
        self.assertEqual(controller.OLD_IMAGES["web"], "suxiaoyou-cloud-web:20261117-composer")

    def test_only_exact_additive_tenant_migration_is_allowed(self):
        name = controller.SQLITE_MIGRATION
        master, read = self.source({name: b"reviewed additive migration"})
        allowed = {"backend/app/example.py": controller.digest(b"after"),
                   name: controller.digest(b"reviewed additive migration")}
        with patch.object(controller, "read", side_effect=read), \
                patch.object(controller, "EXPECTED_SOURCE_CHANGES", allowed):
            self.assertEqual(set(controller.verify_source(master, "a" * 64)[2]), set(allowed))
        master, read = self.source({"backend/alembic/versions/unreviewed.py": b"unsafe"})
        with patch.object(controller, "read", side_effect=read):
            with self.assertRaises(controller.Failure) as failure:
                controller.verify_source(master, "a" * 64)
            self.assertEqual(failure.exception.code, "dependencies_or_schema_changed")

    def test_blind_old_runtime_rollback_is_rejected_before_host_access(self):
        with patch.object(controller, "read") as read, patch.object(controller, "command") as command:
            with self.assertRaises(controller.Failure) as failure:
                controller.activate(None, "a" * 64, rollback=True)
        self.assertEqual(failure.exception.code, "schema_rollback_requires_reviewed_offline_projection_downgrade")
        read.assert_not_called()
        command.assert_not_called()


class TranscriptTests(unittest.TestCase):
    def test_direct_runtime_helper_also_rejects_blind_rollback(self):
        old_tag = "suxiaoyou-online-runtime:20261117-incident2"
        new_tag = "suxiaoyou-online-runtime:20261117-results"
        request = {"mode": "refresh", "operation_id": runtime.OPERATION,
                   "origin": runtime.ORIGIN, "api_image_id": "sha256:" + "a" * 64,
                   "runtime_image": old_tag,
                   "images": {old_tag: "sha256:" + "b" * 64, new_tag: "sha256:" + "c" * 64},
                   "baseline": {}, "rollback": True}
        with patch.dict(runtime.__dict__, {"REQUEST": request}):
            with self.assertRaises(runtime.Failure) as failure:
                runtime.request_values()
        self.assertEqual(failure.exception.code, "schema_rollback_requires_reviewed_offline_projection_downgrade")

    def test_fingerprint_reads_actual_sqlite_rows_without_modifying_database(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "synthetic.sqlite3"
            with sqlite3.connect(path) as db:
                for table in ("message", "part"):
                    db.execute("CREATE TABLE " + table + " (id TEXT PRIMARY KEY, data TEXT)")
                    db.execute("INSERT INTO " + table + " VALUES (?, ?)", ("synthetic", '{"text":"fixture"}'))
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            program = runtime.TRANSCRIPT_PROBE.replace("file:/tenant/state/data/suxiaoyou.db?mode=ro", path.as_uri() + "?mode=ro")
            output = subprocess.run([sys.executable, "-c", program], check=True, capture_output=True, text=True)
            value = json.loads(output.stdout)
            self.assertEqual({name: row["count"] for name, row in value.items()}, {"message": 1, "part": 1})
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
            with sqlite3.connect(path) as db:
                db.execute("UPDATE part SET data = ?", ('{"text":"changed"}',))
            changed = json.loads(subprocess.run([sys.executable, "-c", program], check=True, capture_output=True, text=True).stdout)
            self.assertEqual(value["message"], changed["message"])
            self.assertNotEqual(value["part"], changed["part"])

    def test_changed_original_transcript_is_rejected(self):
        public = type("Public", (), {"unchanged": staticmethod(lambda *_: None)})
        before = {"images": {}, "origin": runtime.ORIGIN,
                  "tenants": [{"owner_id": "owner", "transcript": {"part": "old"}}]}
        runtime.unchanged(public, before, copy.deepcopy(before))
        after = copy.deepcopy(before)
        after["tenants"][0]["transcript"]["part"] = "changed"
        with self.assertRaises(runtime.Failure) as failure:
            runtime.unchanged(public, before, after)
        self.assertEqual(failure.exception.code, "original_transcript_changed")


class AcceptanceTests(unittest.TestCase):
    def test_new_guard_directory_is_synced_before_claim(self):
        with tempfile.TemporaryDirectory() as folder:
            child = Path(folder) / "new-acceptance"
            real_sync = os.fsync
            synced = []
            def observe(fd):
                synced.append(os.fstat(fd).st_ino)
                real_sync(fd)
            with patch.object(acceptance.os, "fsync", side_effect=observe):
                acceptance.ensure_acceptance_directory(child)
            self.assertEqual(synced, [Path(folder).stat().st_ino])
            self.assertEqual(stat.S_IMODE(child.stat().st_mode), 0o700)

    def test_guard_creation_and_replace_sync_the_private_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "guard.json"
            real_sync = os.fsync
            kinds = []
            def observe(fd):
                kinds.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
                real_sync(fd)
            with patch.object(acceptance.os, "fsync", side_effect=observe):
                acceptance.private_write(path, {"phase": "prepared"}, exclusive=True)
                acceptance.private_write(path, {"phase": "dispatched_unknown"})
            self.assertEqual(kinds, ["file", "directory", "file", "directory"])

    def test_once_guard_cannot_overwrite_or_follow_existing_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "guard.json"
            acceptance.private_write(path, {"phase": "prepared"}, exclusive=True)
            with self.assertRaises(FileExistsError):
                acceptance.private_write(path, {"phase": "second_dispatch"}, exclusive=True)
            self.assertEqual(json.loads(path.read_text()), {"phase": "prepared"})

    def test_provider_task_has_three_new_targets_and_one_explicit_delivery(self):
        body, files = acceptance.prompt_body("a" * 32, Path("/tenant/workspaces/acceptance-new"), "deepseek-office")
        self.assertEqual(set(files), {"process-notes.json", "draft.md", "result.md"})
        allowed = [row for row in body["permission_rules"] if row["action"] == "allow"]
        self.assertEqual(len(allowed), 4)
        self.assertEqual({row["permission"] for row in allowed}, {"write", "present_file"})
        self.assertTrue(all(row["pattern"].startswith("/tenant/workspaces/acceptance-new/") for row in allowed))


if __name__ == "__main__":
    unittest.main()
