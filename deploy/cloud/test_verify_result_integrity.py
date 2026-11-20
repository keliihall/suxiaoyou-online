"""Offline, fail-closed tests for release provenance. No Docker or provider."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("verify_results", Path(__file__).with_name("verify-result-integrity.py"))
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


class PinTests(unittest.TestCase):
    def pins(self):
        return {**{name: "a" * 64 for name in ("archive_sha256", "source_sha256", "manifest_sha256", "controller_sha256", "runtime_helper_sha256")},
                "images": {name: "sha256:" + "b" * 64 for name in ("api", "runtime", "web")},
                "changes": {verifier.MIGRATION: "c" * 64, "frontend/src/lib/message-history.ts": "d" * 64}}

    def test_final_pins_require_three_images_exact_scope_and_migration(self):
        verifier.validate_pins(self.pins())
        for change in (lambda p: p.update(source_sha256="FINALIZE"),
                       lambda p: p["images"].pop("runtime"),
                       lambda p: p["changes"].pop(verifier.MIGRATION),
                       lambda p: p["changes"].update({"backend/app/../../private": "e" * 64}),
                       lambda p: p["changes"].update({"backend/cloud_alembic/new.py": "e" * 64})):
            value = self.pins()
            change(value)
            with self.assertRaises(verifier.Failure):
                verifier.validate_pins(value)

    def test_duplicate_input_keys_are_rejected(self):
        with self.assertRaises(verifier.Failure):
            verifier.decode('{"images":{},"images":{"api":"changed"}}')

    def test_scopes_keep_runtime_data_without_demanding_excluded_api_assets(self):
        files = {"session/context_projection.py": "a" * 64, "data/font.ttf": "b" * 64}
        api, api_proof = verifier.source_expectation(files, "/app/app")
        runtime, runtime_proof = verifier.source_expectation(files, "/opt/backend/app")
        self.assertEqual(set(api), {"session/context_projection.py"})
        self.assertEqual(runtime, files)
        self.assertEqual((api_proof["files"], runtime_proof["files"]), (1, 2))
        self.assertNotEqual(api_proof["sha256"], runtime_proof["sha256"])
        with self.assertRaises(verifier.Failure):
            verifier.source_expectation(files, "/tenant")

    def test_original_message_or_part_fingerprint_changes_are_rejected(self):
        owner = "a" * 64
        tenant = {"owner_id": owner, "volume_name": "sxy-data-" + owner[:32], "state": "running", "generation": 1,
                  "runtime_image": "suxiaoyou-online-runtime:20261117-incident2",
                  "transcript": {name: {"count": 2, "sha256": "b" * 64} for name in ("message", "part")}}
        before = {"tenants": [tenant]}
        after = copy.deepcopy(before)
        after["tenants"][0].update(generation=2, runtime_image="suxiaoyou-online-runtime:20261117-results")
        self.assertEqual(verifier.transcript_proof(before, after), 1)
        after["tenants"][0]["transcript"]["part"]["sha256"] = "c" * 64
        with self.assertRaises(verifier.Failure):
            verifier.transcript_proof(before, after)


class CompiledTests(unittest.TestCase):
    CONTENT = ('throw new Error("Message history changed during refresh");'
               'const store={fileScope:"deliverables",fileRequestRevision:0};'
               'const url="?scope=all",action="chat-primary-action";')

    def build(self, folder, content=None):
        root = Path(folder)
        (root / "static/chunks").mkdir(parents=True)
        (root / "static/chunks/public.js").write_text(self.CONTENT if content is None else content)
        (root / "BUILD_ID").write_text("offline-build")
        return root

    def probe(self, root):
        return verifier.compiled_probe(["node", "-e", verifier.WEB_PROBE, str(root)])

    def test_public_assets_expose_only_urls_hashes_and_flags(self):
        with tempfile.TemporaryDirectory() as folder:
            value = self.probe(self.build(folder))
            self.assertTrue(value["ok"])
            self.assertEqual(value["assets"], [{"url": "https://nav.cyneu.com:18443/_next/static/chunks/public.js",
                                               "sha256": hashlib.sha256(self.CONTENT.encode()).hexdigest()}])
            self.assertTrue(all(value["checks"].values()))
            self.assertNotIn("Message history", json.dumps(value))

    def test_each_missing_feature_is_rejected(self):
        for marker in ("Message history changed during refresh", "fileScope", "deliverables", "fileRequestRevision", "?scope=", "chat-primary-action"):
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as folder:
                root = self.build(folder, self.CONTENT.replace(marker, "old"))
                with self.assertRaises(verifier.Failure):
                    self.probe(root)

    def test_missing_build_identity_and_symlinked_assets_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.build(folder)
            (root / "BUILD_ID").unlink()
            with self.assertRaises(verifier.Failure):
                self.probe(root)
        with tempfile.TemporaryDirectory() as folder:
            root = self.build(folder)
            (root / "static/chunks/link.js").symlink_to(root / "static/chunks/public.js")
            with self.assertRaises(verifier.Failure):
                self.probe(root)


class SchemaTests(unittest.TestCase):
    def test_actual_source_probe_rejects_changed_container_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.py"
            source.write_text("# reviewed source\n")
            files = {"source.py": hashlib.sha256(source.read_bytes()).hexdigest()}
            _, proof = verifier.source_expectation(files, "/opt/backend/app")
            program = verifier.SOURCE_PROBE.replace(
                'if str(root) not in {"/app/app","/opt/backend/app"}',
                "if str(root) != " + repr(str(root)))
            payload = json.dumps({"root": str(root), "files": files})
            accepted = subprocess.run([sys.executable, "-B", "-c", program], input=payload, capture_output=True, text=True)
            self.assertEqual(accepted.returncode, 0)
            self.assertEqual(json.loads(accepted.stdout), proof)
            source.write_text("# changed source\n")
            rejected = subprocess.run([sys.executable, "-B", "-c", program], input=payload, capture_output=True, text=True)
            self.assertNotEqual(rejected.returncode, 0)

    def test_real_sqlite_schema_and_migration_hash_are_checked_without_business_reads(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            migration, database = root / "0013.py", root / "schema.sqlite3"
            migration.write_text("# reviewed additive projection migration\n")
            with sqlite3.connect(database) as db:
                db.execute("CREATE TABLE alembic_version (version_num TEXT)")
                db.execute("INSERT INTO alembic_version VALUES ('0013_v110_context_projection')")
                db.execute("CREATE TABLE session_context_projection (session_id TEXT, anchor_message_id TEXT, boundary_text TEXT, pruned_tool_part_ids TEXT)")
            before = hashlib.sha256(database.read_bytes()).hexdigest()
            program = verifier.SCHEMA_PROBE.replace("/opt/backend/alembic/versions/0013_v110_context_projection.py", str(migration))
            program = program.replace("file:/tenant/state/data/suxiaoyou.db?mode=ro", database.as_uri() + "?mode=ro")
            def run(identity):
                return subprocess.run([sys.executable, "-B", "-c", program],
                                      input=json.dumps({"migration_sha256": identity}), capture_output=True, text=True)
            identity = hashlib.sha256(migration.read_bytes()).hexdigest()
            accepted = run(identity)
            self.assertEqual(accepted.returncode, 0)
            self.assertEqual(json.loads(accepted.stdout), {"ok": True, "migration_file": True, "head_0013": True, "projection_table": True})
            self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), before)
            self.assertNotEqual(run("a" * 64).returncode, 0)
            with sqlite3.connect(database) as db:
                db.execute("UPDATE alembic_version SET version_num='0012_v110_workspace_identity_v2'")
            self.assertNotEqual(run(identity).returncode, 0)
            with sqlite3.connect(database) as db:
                db.execute("UPDATE alembic_version SET version_num='0013_v110_context_projection'")
                db.execute("DROP TABLE session_context_projection")
            self.assertNotEqual(run(identity).returncode, 0)


if __name__ == "__main__":
    unittest.main()
