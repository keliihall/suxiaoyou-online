"""Offline safety proofs for Web preview provenance; no Docker or provider."""
import copy
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("verify_preview", Path(__file__).with_name("verify-file-preview.py"))
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


class PinTests(unittest.TestCase):
    def pins(self):
        return {**{name: "a" * 64 for name in ("archive_sha256", "source_sha256", "manifest_sha256", "controller_sha256")},
                "images": {"api": verifier.API_IMAGE, "runtime": verifier.RUNTIME_IMAGE, "web": "sha256:" + "b" * 64},
                "changes": {"frontend/src/lib/file-preview-links.ts": "c" * 64},
                "compiled_markers": ["file-preview-link"]}

    def test_exact_frontend_source_and_unchanged_backends_are_required(self):
        verifier.validate_pins(self.pins())
        for change in (lambda p: p.update(source_sha256="FINALIZE"),
                       lambda p: p["images"].update(api="sha256:" + "d" * 64),
                       lambda p: p["images"].update(runtime="sha256:" + "e" * 64),
                       lambda p: p.update(changes={}),
                       lambda p: p["changes"].update({"backend/app/processor.py": "c" * 64}),
                       lambda p: p["changes"].update({"frontend/src/../private": "d" * 64}),
                       lambda p: p.update(compiled_markers=[]),
                       lambda p: p.update(compiled_markers=["../private"]),
                       lambda p: p.update(compiled_markers=["file-preview-link", "file-preview-link"])):
            pins = self.pins()
            change(pins)
            with self.subTest(pins=pins), self.assertRaises(verifier.Failure):
                verifier.validate_pins(pins)

    def test_duplicate_json_keys_are_rejected(self):
        with self.assertRaises(verifier.Failure):
            verifier.decode('{"images":{},"images":{}}')

    def test_deployment_requires_zero_task_auth_db_and_model_operations(self):
        state = {name: False for name in ("schema_changes", "cloud_schema_changes", "dependencies_changed", "credentials_changed", "automatic_rollback")}
        state.update(tenant_schema_migration=None, api_runtime_version=verifier.OLD_VERSION, old_web_version=verifier.OLD_WEB_VERSION,
                     non_web_containers_preserved=True, tenant_containers_preserved=True,
                     **{name: 0 for name in ("model_requests", "runtime_restarts", "database_operations", "auth_operations", "task_operations")})
        verifier.deployment_contract(state)
        for name, value in (("schema_changes", True), ("model_requests", 1), ("runtime_restarts", 1),
                            ("database_operations", 1), ("auth_operations", 1), ("task_operations", 1),
                            ("tenant_schema_migration", "0014"), ("api_runtime_version", verifier.VERSION)):
            wrong = dict(state)
            wrong[name] = value
            with self.subTest(name=name), self.assertRaises(verifier.Failure):
                verifier.deployment_contract(wrong)


class PreservationTests(unittest.TestCase):
    def services(self):
        values = {name: {"id": name, "name": name, "image": name + ":same", "image_id": "sha256:" + "a" * 64,
                         "started_at": "unchanged", "mounts": [name], "running": True, "oom": False, "health": "healthy"}
                  for name in verifier.SERVICES}
        for name in ("api", "worker"):
            values[name].update(image="suxiaoyou-cloud-api:" + verifier.OLD_VERSION, image_id=verifier.API_IMAGE)
        return values

    def test_all_non_web_identities_and_health_are_unchanged(self):
        before = self.services()
        after = copy.deepcopy(before)
        after["web"].update(id="web-new", image="suxiaoyou-cloud-web:" + verifier.VERSION, image_id="sha256:" + "b" * 64)
        state = {"original_services": before, "activation_services": before, "services_after": copy.deepcopy(after)}
        images = {"api": verifier.API_IMAGE, "web": "sha256:" + "b" * 64}
        verifier.preserved_services(after, state, images)
        for name, key, value in (("api", "id", "recreated"), ("worker", "started_at", "restarted"),
                                 ("proxy", "id", "recreated"), ("db", "mounts", []),
                                 ("web", "image_id", "sha256:" + "c" * 64), ("web", "health", "starting")):
            wrong = copy.deepcopy(after)
            wrong[name][key] = value
            with self.subTest(name=name, key=key), self.assertRaises(verifier.Failure):
                verifier.preserved_services(wrong, state, images)

    def test_tenant_restart_removal_volume_or_running_state_changes_are_rejected(self):
        tenant = {"id": "same", "name": "tenant", "image": "runtime:same", "image_id": verifier.RUNTIME_IMAGE,
                  "started_at": "unchanged", "mounts": ["volume"], "running": True}
        state = {name: {"tenant": copy.deepcopy(tenant)} for name in ("original_tenants", "activation_tenants", "tenants_after")}
        verifier.preserved_tenants({"tenant": tenant}, state)
        for key, value in (("id", "changed"), ("started_at", "new"), ("mounts", []), ("running", False)):
            wrong = copy.deepcopy(tenant)
            wrong[key] = value
            with self.subTest(key=key), self.assertRaises(verifier.Failure):
                verifier.preserved_tenants({"tenant": wrong}, state)
        with self.assertRaises(verifier.Failure):
            verifier.preserved_tenants({}, state)


class CompiledTests(unittest.TestCase):
    CONTENT = ('throw new Error("Message history changed during refresh");'
               'const state={fileScope:"deliverables",fileRequestRevision:0};'
               'const scope="?scope=",button="chat-primary-action",preview="file-preview-link";')

    def build(self, folder, content=None):
        root = Path(folder)
        (root / "static/chunks").mkdir(parents=True)
        (root / "static/chunks/public.js").write_text(self.CONTENT if content is None else content)
        (root / "BUILD_ID").write_text("offline-build")
        return root

    def probe(self, root):
        return verifier.compiled_probe(["node", "-e", verifier.WEB_PROBE, str(root)], ["file-preview-link"])

    def test_compiled_new_preview_and_old_integrity_contracts_expose_only_public_assets(self):
        with tempfile.TemporaryDirectory() as folder:
            result = self.probe(self.build(folder))
            self.assertTrue(result["ok"])
            self.assertTrue(all(result["checks"].values()))
            self.assertEqual(result["assets"], [{"url": verifier.ORIGIN + "/_next/static/chunks/public.js",
                                                 "sha256": hashlib.sha256(self.CONTENT.encode()).hexdigest()}])

    def test_missing_new_preview_or_previous_integrity_feature_is_rejected(self):
        for marker in ("Message history changed during refresh", "fileScope", "fileRequestRevision", "?scope=", "chat-primary-action", "file-preview-link"):
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as folder:
                root = self.build(folder, self.CONTENT.replace(marker, "old"))
                with self.assertRaises(verifier.Failure):
                    self.probe(root)

    def test_missing_build_id_and_symlinked_asset_are_rejected(self):
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


if __name__ == "__main__":
    unittest.main()
