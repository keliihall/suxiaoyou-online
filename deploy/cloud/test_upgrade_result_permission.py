"""Offline backend-only release contracts; no Docker, network or model calls."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parent


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


base = module("result_permission_base_tests", "test_upgrade_incident.py")
results = module("result_permission_transcript_tests", "test_upgrade_result_integrity.py")
controller = module("result_permission_controller", "upgrade-result-permission.py")
runtime = module("result_permission_runtime", "result-permission-runtime.py")
PROCESSOR = "backend/app/session/processor.py"
MIGRATION = (ROOT.parent.parent / controller.SQLITE_MIGRATION).read_bytes()


class ControllerTests(base.IncidentControllerTests):
    def setUp(self):
        p = patch.object(base, "controller", controller)
        p.start()
        self.addCleanup(p.stop)

    def pins(self):
        return patch.multiple(controller, SOURCE_SHA256="a" * 64,
                              OLD_OVERRIDE_SHA256="b" * 64,
                              EXPECTED_SOURCE_CHANGES={PROCESSOR: "c" * 64})

    def source(self, changed=None):
        before = {"compose.cloud.yaml": b"same-compose", PROCESSOR: b"before", controller.SQLITE_MIGRATION: MIGRATION}
        after = dict(before, **{PROCESSOR: b"after"})
        if changed:
            after.update(changed)
        def validate(path, _sha):
            content = before if path == controller.OLD_SOURCE else after
            return {"source_sha256": "before" if content is before else "after"}, [(n, b, 0o644) for n, b in content.items()]
        return SimpleNamespace(validate_package=validate), lambda path, _max: before[path.relative_to(controller.OLD_RELEASE).as_posix()]

    def test_mixed_previous_release_images_are_pinned(self):
        self.assertEqual(controller.OLD_VERSION, "20261117-results")
        self.assertEqual(controller.OLD_CONFIG, controller.OLD_BACKEND_CONFIG)
        self.assertEqual(controller.VERSION, "20261117-results2")
        self.assertEqual(controller.IMAGES["web"], controller.OLD_IMAGES["web"])
        self.assertEqual(controller.BASE_IDS[controller.OLD_IMAGES["web"]], "sha256:a5c24e6872cbf840c8edd0fd606bddd677c98867a8ed7bf47b8bf24de19e71d1")
        self.assertEqual(controller.BASE_IDS[controller.OLD_IMAGES["api"]], "sha256:9af18c46e6334f4330acab45884e5ca9b0f03db4708d668915f223c7d8b20656")
        self.assertEqual(controller.BASE_IDS[controller.OLD_IMAGES["runtime"]], runtime.OLD_RUNTIME_ID)

    def test_only_reviewed_source_bytes_are_accepted(self):
        master, read = self.source()
        with patch.object(controller, "read", side_effect=read), patch.object(controller, "EXPECTED_SOURCE_CHANGES", {PROCESSOR: controller.digest(b"after")}):
            self.assertEqual(controller.verify_source(master, "a" * 64)[2], [PROCESSOR])

    def test_unreviewed_source_and_dependency_changes_are_rejected(self):
        for changed, expected in [({"frontend/src/unreviewed.ts": b"extra"}, "source_change_allowlist_mismatch"),
                                  ({"compose.cloud.yaml": b"changed"}, "dependencies_or_schema_changed"),
                                  ({controller.SQLITE_MIGRATION: b"changed"}, "dependencies_or_schema_changed"),
                                  ({"backend/alembic/versions/0014_unsafe.py": b"extra"}, "dependencies_or_schema_changed")]:
            master, read = self.source(changed)
            with self.subTest(changed=changed), patch.object(controller, "read", side_effect=read), patch.object(controller, "EXPECTED_SOURCE_CHANGES", {PROCESSOR: controller.digest(b"after")}):
                with self.assertRaises(controller.Failure) as failure:
                    controller.verify_source(master, "a" * 64)
                self.assertEqual(failure.exception.code, expected)

    def test_final_allowlist_cannot_widen_beyond_one_processor_file(self):
        with self.pins(), patch.object(controller, "EXPECTED_SOURCE_CHANGES", {"frontend/src/unreviewed.ts": "c" * 64}):
            with self.assertRaises(controller.Failure) as failure:
                controller.finalized_pins()
            self.assertEqual(failure.exception.code, "source_allowlist_not_finalized")

    def test_only_api_and_runtime_overlay_one_app_layer_without_migration_copy(self):
        recipes = controller.overlay_recipes()
        self.assertEqual(set(recipes), {"api", "runtime"})
        for name, raw in recipes.items():
            self.assertTrue(raw.startswith(("FROM " + controller.OLD_IMAGES[name] + "\n").encode()))
            self.assertEqual(raw.count(b"COPY "), 1)
            self.assertNotIn(b"alembic", raw)
            self.assertNotIn(b"RUN ", raw)

    def test_override_preserves_web_recipe_and_changes_only_backend_references(self):
        api_path = str(controller.OLD_BACKEND_CONFIG / "build/Dockerfile.api")
        web_path = str(controller.OLD_CONFIG / "build/Dockerfile.web")
        raw = f'api: {controller.OLD_IMAGES["api"]}\nruntime: {controller.OLD_IMAGES["runtime"]}\nweb: {controller.OLD_IMAGES["web"]}\nmigrate: {controller.OLD_IMAGES["api"]}\napi_recipe: {api_path}\nweb_recipe: {web_path}\n'
        result = controller.updated_override(raw, controller.CONFIG / "build")
        self.assertIn(web_path, result)
        self.assertIn(controller.OLD_IMAGES["web"], result)
        self.assertNotIn(str(controller.CONFIG / "build/Dockerfile.web"), result)
        expected = raw.replace(controller.OLD_IMAGES["api"], controller.IMAGES["api"]).replace(controller.OLD_IMAGES["runtime"], controller.IMAGES["runtime"]).replace(api_path, str(controller.CONFIG / "build/Dockerfile.api"))
        self.assertEqual(result, expected)

    def test_web_restart_or_proxy_recreation_is_rejected(self):
        before = inventory()
        current = copy.deepcopy(before)
        current["proxy"]["state"]["StartedAt"] = "after-proxy-start"
        controller.no_volume_change(before, current)
        for name, field, value in [("web", "id", "new"), ("proxy", "id", "new"), ("proxy", "image_id", "sha256:new")]:
            with self.subTest(name=name, field=field):
                after = copy.deepcopy(before)
                after[name][field] = value
                with self.assertRaises(controller.Failure):
                    controller.no_volume_change(before, after)
        after = copy.deepcopy(before)
        after["web"]["state"]["StartedAt"] = "restarted"
        with self.assertRaises(controller.Failure):
            controller.no_volume_change(before, after)

    def test_blind_rollback_fails_before_host_access(self):
        with patch.object(controller, "read") as read, patch.object(controller, "command") as command:
            with self.assertRaises(controller.Failure) as failure:
                controller.activate(None, "a" * 64, rollback=True)
        self.assertEqual(failure.exception.code, "schema_rollback_requires_reviewed_offline_projection_downgrade")
        read.assert_not_called()
        command.assert_not_called()


def inventory():
    rows = {}
    for index, name in enumerate(controller.SERVICES):
        image = controller.OLD_IMAGES["api"] if name == "worker" else controller.OLD_IMAGES.get(name, "fixed-" + name)
        rows[name] = {"id": str(index) * 64, "image": image,
                      "image_id": controller.BASE_IDS.get(image, "sha256:" + str(index) * 64),
                      "mounts": [{"Name": "volume-" + name}], "state": {"Running": True, "StartedAt": "original", "Health": {"Status": "healthy"}}}
    return rows


class ActivationTests(unittest.TestCase):
    def exercise(self, *, changed_second_snapshot=False):
        with tempfile.TemporaryDirectory() as folder:
            app = Path(folder).resolve()
            old_release = app / "old"
            old_release.mkdir()
            (app / "current").symlink_to(old_release)
            original = inventory()
            current = copy.deepcopy(original)
            events = []
            state_path = app / "state.json"
            built = {"api": "sha256:" + "a" * 64, "runtime": "sha256:" + "b" * 64,
                     "web": controller.BASE_IDS[controller.OLD_IMAGES["web"]]}
            state = {"version": controller.VERSION, "source_archive_sha256": "c" * 64,
                     "controller_sha256": controller.digest(b"controller"), "runtime_helper_sha256": controller.HELPER_SHA256,
                     "phase": "built", "fixed_inputs": {}, "built_images": built, "original_services": original}
            snapshot = {"billing": {"settled": 1, "reserved": 0}, "web_auth_sessions": "unchanged"}
            calls = 0
            def runtime_call(mode, _state, **kwargs):
                nonlocal calls
                events.append(mode)
                calls += 1
                result = copy.deepcopy(snapshot)
                if changed_second_snapshot and calls == 2:
                    result["billing"]["settled"] += 1
                return result
            def command(arguments, **_kwargs):
                if "stop" in arguments:
                    events.append("stop-proxy")
                    current["proxy"]["state"]["Running"] = False
                elif "up" in arguments:
                    self.assertEqual(arguments[arguments.index("--wait-timeout") + 2:], ["api", "worker"])
                    events.append("replace-api-worker")
                    for name in ("api", "worker"):
                        current[name].update(id="f" * 64, image=controller.IMAGES["api"], image_id=built["api"])
                elif "start" in arguments:
                    self.assertEqual(arguments[-2:], ["start", "proxy"])
                    events.append("start-original-proxy")
                    current["proxy"]["state"].update(Running=True, StartedAt="after-start")
                else:
                    self.fail("unexpected host command")
                return b""
            identities = {**controller.BASE_IDS, **{controller.IMAGES[name]: value for name, value in built.items()}}
            def read(path, *_args, **_kwargs):
                return json.dumps(state).encode() if path == state_path else b"controller"
            def save(value, phase=None):
                if phase:
                    value["phase"] = phase
            with patch.multiple(controller, APP=app, OLD_RELEASE=old_release, STATE=state_path), \
                    patch.object(controller, "read", side_effect=read), patch.object(controller, "save", side_effect=save), \
                    patch.object(controller, "fixed_inputs", return_value={}), patch.object(controller, "verify_source", return_value=({}, [], [])), \
                    patch.object(controller, "checked_release"), patch.object(controller, "helper_bytes"), patch.object(controller, "compose_contract"), \
                    patch.object(controller, "image", side_effect=lambda tag: {"Id": identities[tag]}), \
                    patch.object(controller, "runtime", side_effect=runtime_call), patch.object(controller, "command", side_effect=command), \
                    patch.object(controller, "services", side_effect=lambda: copy.deepcopy(current)), \
                    patch.object(controller, "service", side_effect=lambda name: copy.deepcopy(current[name])), \
                    patch.object(controller, "backup", side_effect=lambda *_: events.append("private-backup")), \
                    patch.object(controller, "internal_ready", side_effect=lambda **_: events.append("internal-ready")), \
                    patch.object(controller, "https_ready"), patch.object(controller, "switch_current"), patch.object(controller, "atomic"):
                if changed_second_snapshot:
                    with self.assertRaises(controller.Failure) as failure:
                        controller.activate(None, "c" * 64)
                    self.assertEqual(failure.exception.code, "idle_state_changed_after_ingress_closed")
                else:
                    self.assertEqual(controller.activate(None, "c" * 64)["phase"], "active")
            return events

    def test_double_idle_backup_backend_replace_and_original_proxy_start_order(self):
        self.assertEqual(self.exercise(), ["snapshot", "stop-proxy", "snapshot", "private-backup", "replace-api-worker", "refresh", "verify", "internal-ready", "start-original-proxy"])

    def test_changed_second_idle_snapshot_never_replaces_services_or_starts_ingress(self):
        self.assertEqual(self.exercise(changed_second_snapshot=True), ["snapshot", "stop-proxy", "snapshot"])


class TranscriptTests(results.TranscriptTests):
    def setUp(self):
        p = patch.object(results, "runtime", runtime)
        p.start()
        self.addCleanup(p.stop)

    def request(self):
        return {"mode": "snapshot", "operation_id": runtime.OPERATION, "origin": runtime.ORIGIN,
                "api_image_id": "sha256:" + "a" * 64, "runtime_image": runtime.OLD_RUNTIME_TAG,
                "images": {runtime.OLD_RUNTIME_TAG: runtime.OLD_RUNTIME_ID, runtime.NEW_RUNTIME_TAG: "sha256:" + "b" * 64},
                "baseline": None, "rollback": False}

    def test_direct_runtime_helper_also_rejects_blind_rollback(self):
        request = dict(self.request(), mode="refresh", rollback=True, baseline={})
        with patch.dict(runtime.__dict__, {"REQUEST": request}):
            with self.assertRaises(runtime.Failure) as failure:
                runtime.request_values()
        self.assertEqual(failure.exception.code, "schema_rollback_requires_reviewed_offline_projection_downgrade")

    def test_only_pinned_schema_0013_baseline_and_new_runtime_are_allowed(self):
        request = self.request()
        with patch.dict(runtime.__dict__, {"REQUEST": request}):
            self.assertEqual(runtime.request_values(), request)
        for change in ("old-tag", "old-id"):
            invalid = copy.deepcopy(request)
            if change == "old-tag":
                invalid["images"]["suxiaoyou-online-runtime:20261117-incident2"] = invalid["images"].pop(runtime.OLD_RUNTIME_TAG)
                invalid["runtime_image"] = "suxiaoyou-online-runtime:20261117-incident2"
            else:
                invalid["images"][runtime.OLD_RUNTIME_TAG] = "sha256:" + "c" * 64
            with self.subTest(change=change), patch.dict(runtime.__dict__, {"REQUEST": invalid}):
                with self.assertRaises(runtime.Failure) as failure:
                    runtime.request_values()
                self.assertEqual(failure.exception.code, "schema_0013_runtime_baseline_required")

    def test_new_namespace_and_full_transcript_probe_are_bound_to_new_helper_hash(self):
        self.assertEqual(runtime.OPERATION, controller.VERSION)
        self.assertEqual(runtime.TRANSCRIPT_PROBE, results.runtime.TRANSCRIPT_PROBE)
        self.assertEqual(controller.HELPER_SHA256, hashlib.sha256((ROOT / "result-permission-runtime.py").read_bytes()).hexdigest())
        self.assertEqual(hashlib.sha256((ROOT / "upgrade-result-integrity.py").read_bytes()).hexdigest(), "1ad7c164c9648176779183a9d858b50194e47103b4bc3662996adf4c70aa47e7")
        self.assertEqual(hashlib.sha256((ROOT / "result-integrity-runtime.py").read_bytes()).hexdigest(), "3c287e2ad12552f0f7eb792612153ee398ad48d0e455bfd02bb4c0668266b3fc")


if __name__ == "__main__":
    unittest.main()
