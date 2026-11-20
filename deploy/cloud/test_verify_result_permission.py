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
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("verify_result_permission", Path(__file__).with_name("verify-result-permission.py"))
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


class PinTests(unittest.TestCase):
    def pins(self):
        return {**{name: "a" * 64 for name in ("archive_sha256", "source_sha256", "manifest_sha256", "controller_sha256", "runtime_helper_sha256")},
                "images": {"api": "sha256:" + "b" * 64, "runtime": "sha256:" + "c" * 64, "web": verifier.WEB_IMAGE},
                "changes": {verifier.PROCESSOR: verifier.PROCESSOR_SHA}}

    def test_final_pins_require_unchanged_web_and_exact_permission_patch_only(self):
        verifier.validate_pins(self.pins())
        for change in (lambda p: p.update(source_sha256="FINALIZE"),
                       lambda p: p["images"].pop("runtime"),
                       lambda p: p["images"].update(web="sha256:" + "d" * 64),
                       lambda p: p["changes"].pop(verifier.PROCESSOR),
                       lambda p: p["changes"].update({verifier.PROCESSOR: "d" * 64}),
                       lambda p: p["changes"].update({verifier.MIGRATION: verifier.MIGRATION_SHA}),
                       lambda p: p["changes"].update({"frontend/src/lib/message-history.ts": "d" * 64}),
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
                  "runtime_image": "suxiaoyou-online-runtime:20261117-results",
                  "transcript": {name: {"count": 2, "sha256": "b" * 64} for name in ("message", "part")}}
        before = {"tenants": [tenant]}
        after = copy.deepcopy(before)
        after["tenants"][0].update(generation=2, runtime_image="suxiaoyou-online-runtime:20261117-results2")
        self.assertEqual(verifier.transcript_proof(before, after), 1)
        after["tenants"][0]["transcript"]["part"]["sha256"] = "c" * 64
        with self.assertRaises(verifier.Failure):
            verifier.transcript_proof(before, after)

    def test_patch_does_not_claim_or_perform_a_schema_migration(self):
        state = {"schema_changes": False, "cloud_schema_changes": False, "tenant_schema_migration": None,
                 "dependencies_changed": False, "credentials_changed": False, "automatic_rollback": False}
        verifier.deployment_contract(state)
        for key, value in (("schema_changes", True), ("cloud_schema_changes", True),
                           ("tenant_schema_migration", verifier.MIGRATION), ("automatic_rollback", True)):
            wrong = dict(state)
            wrong[key] = value
            with self.subTest(key=key), self.assertRaises(verifier.Failure):
                verifier.deployment_contract(wrong)
        self.assertEqual(verifier.WEB_VERSION, verifier.OLD_VERSION)
        self.assertEqual(verifier.VERSION, "20261117-results2")

    def test_web_proxy_database_identity_image_mounts_and_health_are_preserved(self):
        originals = {name: {"id": name + "-old", "image_id": "sha256:" + "a" * 64,
                            "image": name + ":results", "mounts": [{"Name": name + "-volume"}]}
                     for name in ("api", "worker", "web", "proxy", "db")}
        current = {name: {**value, "running": True, "oom": False, "health": "healthy"}
                   for name, value in originals.items()}
        current["api"].update(id="api-new", image="api:results2", image_id="sha256:" + "b" * 64)
        current["worker"].update(id="worker-new", image="api:results2", image_id="sha256:" + "b" * 64)
        verifier.preserved_services(current, originals, "api-new")
        for name, key, value in (("web", "id", "web-new"), ("proxy", "id", "proxy-new"),
                                 ("db", "id", "db-new"), ("web", "image_id", "sha256:" + "c" * 64),
                                 ("proxy", "image", "proxy:other"), ("api", "mounts", []),
                                 ("web", "health", "starting"), ("api", "id", "api-other")):
            wrong = copy.deepcopy(current)
            wrong[name][key] = value
            with self.subTest(name=name, key=key), self.assertRaises(verifier.Failure):
                verifier.preserved_services(wrong, originals, "api-new")


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


class MainContractTests(unittest.TestCase):
    def test_complete_backend_patch_keeps_web_and_existing_schema_without_migration_change_pin(self):
        repository = Path(__file__).resolve().parents[2]
        sources = {name: (repository / name).read_bytes() for name in (verifier.PROCESSOR, verifier.MIGRATION)}
        manifest = {
            "source_sha256": "d" * 64,
            "source_file_count": len(sources),
            "files": [{"path": name, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
                      for name, raw in sources.items()],
        }
        manifest_raw = json.dumps(manifest).encode()
        controller, helper = b"# frozen controller\n", b"# frozen runtime helper\n"
        images = {"api": "sha256:" + "b" * 64, "runtime": "sha256:" + "c" * 64, "web": verifier.WEB_IMAGE}
        pins = {"archive_sha256": "a" * 64, "source_sha256": manifest["source_sha256"],
                "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
                "controller_sha256": hashlib.sha256(controller).hexdigest(),
                "runtime_helper_sha256": hashlib.sha256(helper).hexdigest(),
                "images": images, "changes": {verifier.PROCESSOR: verifier.PROCESSOR_SHA}}
        pins_raw = json.dumps(pins).encode()
        owner = "a" * 64
        tenant = {"owner_id": owner, "volume_name": "sxy-data-" + owner[:32], "state": "running", "generation": 1,
                  "runtime_image": "suxiaoyou-online-runtime:" + verifier.OLD_BACKEND,
                  "transcript": {name: {"count": 1, "sha256": "e" * 64} for name in ("message", "part")}}
        before = {"billing": {"count": 1, "sha256": "f" * 64}, "web_auth_sessions": {"count": 1},
                  "runtime_image": tenant["runtime_image"], "tenants": [tenant]}
        after = copy.deepcopy(before)
        after.update(runtime_image="suxiaoyou-online-runtime:" + verifier.VERSION,
                     api_id="1" * 64, api_image_id=images["api"])
        after["tenants"][0].update(generation=2, runtime_image=after["runtime_image"], container_id="2" * 64)
        services = {}
        for index, name in enumerate(("db", "api", "worker", "web", "proxy"), start=3):
            kind = "api" if name == "worker" else name
            tag_version = verifier.WEB_VERSION if name == "web" else verifier.VERSION
            image = "suxiaoyou-cloud-" + kind + ":" + tag_version
            services[name] = {"id": "1" * 64 if name == "api" else str(index) * 64,
                              "image_id": images.get(kind, "sha256:" + "9" * 64), "image": image,
                              "running": True, "oom": False, "health": "healthy", "mounts": [{"Name": name + "-volume"}]}
        original = copy.deepcopy(services)
        for name in ("api", "worker"):
            original[name].update(id="0" * 64, image="suxiaoyou-cloud-api:" + verifier.OLD_BACKEND, image_id="sha256:" + "8" * 64)
        state = {"version": verifier.VERSION, "old_version": verifier.OLD_VERSION, "phase": "active",
                 "source_archive_sha256": pins["archive_sha256"], "source_sha256": pins["source_sha256"],
                 "controller_sha256": pins["controller_sha256"], "runtime_helper_sha256": pins["runtime_helper_sha256"],
                 "built_images": images, "source_changes": [verifier.PROCESSOR], "original_services": original,
                 "schema_changes": False, "cloud_schema_changes": False, "tenant_schema_migration": None,
                 "dependencies_changed": False, "credentials_changed": False, "automatic_rollback": False,
                 "before": before, "after": after}
        runtime_name = "sxy-user-" + owner[:32]
        containers = {verifier.PROJECT + "-" + name + "-1": value for name, value in services.items()}
        containers[runtime_name] = {"id": "2" * 64, "image_id": images["runtime"], "image": after["runtime_image"],
                                    "running": True, "oom": False, "mounts": [{"Type": "volume", "Name": tenant["volume_name"], "Destination": "/tenant"}]}
        image_records = {}
        for kind, identity in images.items():
            tag = ("suxiaoyou-online-runtime:" if kind == "runtime" else "suxiaoyou-cloud-" + kind + ":") + (verifier.WEB_VERSION if kind == "web" else verifier.VERSION)
            value = {"Id": identity, "RepoTags": [tag], "Os": "linux", "Architecture": "amd64",
                     "Config": {"kind": kind}, "RootFS": {"Layers": [kind]}}
            image_records[tag] = image_records[identity] = value
        schema_calls = []
        def docker(*args, content=None, maximum=None):
            if args[:2] == ("image", "inspect"):
                return json.dumps([image_records[args[2]]]).encode()
            if args[0] == "ps":
                return (runtime_name + "\n").encode()
            if args[0] == "exec" and args[-1] == verifier.SOURCE_PROBE:
                payload = json.loads(content)
                return json.dumps(verifier.source_expectation(payload["files"], payload["root"])[1]).encode()
            if args[0] == "exec" and args[-1] == verifier.SCHEMA_PROBE:
                schema_calls.append(json.loads(content))
                return b'{"ok":true,"migration_file":true,"head_0013":true,"projection_table":true}'
            self.fail("Unexpected command")
        compiled = {"ok": True, "checks": {"default_deliverables": True}, "assets": []}
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory).resolve()
            release = app / "releases" / verifier.VERSION
            release.mkdir(parents=True)
            (app / "current").symlink_to(release)
            config = app / "shared/config" / verifier.VERSION
            pinfile = app / "pins.json"
            reads = {pinfile: pins_raw, config / "upgrade-state.json": json.dumps(state).encode(),
                     release / "RELEASE-MANIFEST.json": manifest_raw,
                     app / "incoming/upgrade-result-permission.py": controller,
                     app / "incoming/result-permission-runtime.py": helper,
                     **{release / name: raw for name, raw in sources.items()}}
            with patch.object(verifier, "APP", app), patch.object(verifier, "RELEASE", release), patch.object(verifier, "CONFIG", config), \
                 patch.object(verifier.os, "getuid", return_value=0), patch.object(verifier.os, "geteuid", return_value=0), \
                 patch.object(verifier, "read", side_effect=lambda path, *args, **kwargs: reads[path]), \
                 patch.object(verifier, "container", side_effect=lambda name: containers[name]), patch.object(verifier, "docker", side_effect=docker), \
                 patch.object(verifier, "compiled_probe", return_value=compiled), \
                 patch.object(sys, "argv", ["verify-result-permission.py", "--pins", str(pinfile), "--pins-sha256", hashlib.sha256(pins_raw).hexdigest()]):
                result = verifier.main()
        self.assertTrue(result["ok"])
        self.assertTrue(result["web_proxy_db_preserved"])
        self.assertTrue(result["permission_patch_only"])
        self.assertEqual(schema_calls, [{"migration_sha256": verifier.MIGRATION_SHA}])
        self.assertNotIn(verifier.MIGRATION, pins["changes"])


if __name__ == "__main__":
    unittest.main()
