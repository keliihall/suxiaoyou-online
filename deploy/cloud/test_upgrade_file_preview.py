"""Offline safety contracts for a web-only clickable file preview release."""
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("preview_controller", Path(__file__).with_name("upgrade-file-preview.py"))
controller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(controller)
ACTION = "frontend/src/components/chat/chat-actions.tsx"


class PreviewControllerTests(unittest.TestCase):
    def test_unfinalized_controller_refuses_before_host_io(self):
        with patch.object(sys, "argv", ["upgrade-file-preview.py", "build", "--source-sha256", "a" * 64]), \
                patch.object(controller, "SOURCE_SHA256", "FINALIZE"), \
                patch.object(controller, "environment") as environment, patch.object(controller, "command") as command, \
                patch.object(controller, "write") as write, patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(controller.main(), 1)
        self.assertEqual(json.loads(output.getvalue())["failure"], "release_pins_not_finalized")
        environment.assert_not_called()
        command.assert_not_called()
        write.assert_not_called()

    def test_baseline_pins_and_original_controller_are_preserved(self):
        self.assertEqual(controller.OLD_VERSION, "20261117-results2")
        self.assertEqual(controller.VERSION, "20261117-preview")
        self.assertEqual(controller.OLD_SOURCE_SHA256, "f7c4bfb210c246baa2e911cc4782dcf30a0e60c8ad3b1787930d8fa433ffec37")
        self.assertEqual(controller.BASE_IDS[controller.API], "sha256:d8f9c761513c7f34bbbd6f123d198f69d7e565f56bc1ad73e97b6ed0c1c286ed")
        self.assertEqual(controller.BASE_IDS[controller.RUNTIME], "sha256:724eb22b285089ed453b2f443b80ee2ebd9222d245b7a90a5e8d837f50649ee8")
        self.assertEqual(controller.BASE_IDS[controller.OLD_WEB], "sha256:a5c24e6872cbf840c8edd0fd606bddd677c98867a8ed7bf47b8bf24de19e71d1")
        self.assertEqual(controller.OLD_WEB_VERSION, "20261117-results")
        self.assertEqual(controller.WEB_RECIPE_CONFIG, controller.SHARED / "config/20261117-results")
        self.assertEqual(controller.OLD_CONFIG, controller.SHARED / "config/20261117-results2")
        old = Path(__file__).with_name("upgrade-composer-controls.py")
        self.assertEqual(hashlib.sha256(old.read_bytes()).hexdigest(), "8ea0d4763f718d4f7121529a5a928a6279f2aa156fa778fffdd2e38ccf6f925e")

    def check_source(self, extra=None, wrong_hash=False):
        before = {ACTION: b"before", "backend/app/cloud/inference.py": b"same-backend", "compose.cloud.yaml": b"same-compose"}
        after = {**before, ACTION: b"after", **(extra or {})}
        def validate(path, _sha):
            data = before if path == controller.OLD_SOURCE else after
            return {"source_sha256": "old" if data is before else "new"}, [(name, raw, 0o644) for name, raw in data.items()]
        def read(path, _maximum):
            return before[path.relative_to(controller.OLD_RELEASE).as_posix()]
        with patch.object(controller, "read", side_effect=read), \
                patch.object(controller, "EXPECTED_SOURCE_CHANGES", {ACTION: "a" * 64 if wrong_hash else controller.digest(b"after")}):
            return controller.verify_source(SimpleNamespace(validate_package=validate), "b" * 64)

    def test_only_exact_reviewed_ui_bytes_are_accepted(self):
        self.assertEqual(self.check_source()[2], [ACTION])
        for extra, wrong_hash in (({"backend/app/cloud/inference.py": b"changed"}, False),
                                   ({"frontend/src/unreviewed.ts": b"extra"}, False),
                                   ({"compose.cloud.yaml": b"changed"}, False), ({}, True)):
            with self.subTest(extra=list(extra), wrong_hash=wrong_hash), self.assertRaises(controller.Failure) as failure:
                self.check_source(extra, wrong_hash)
            self.assertEqual(failure.exception.code, "source_change_allowlist_mismatch")

    def test_new_frontend_source_is_accepted_only_when_individually_pinned(self):
        addition = "frontend/src/lib/file-preview-links.ts"
        before = {ACTION: b"before", "backend/app/cloud/inference.py": b"same"}
        after = {**before, ACTION: b"after", addition: b"new helper"}
        def validate(path, _sha):
            value = before if path == controller.OLD_SOURCE else after
            return {"source_sha256": "old" if value is before else "new"}, [(n, b, 0o644) for n, b in value.items()]
        pins = {name: controller.digest(after[name]) for name in (ACTION, addition)}
        with patch.object(controller, "read", side_effect=lambda p, m: before[p.relative_to(controller.OLD_RELEASE).as_posix()]), \
                patch.object(controller, "EXPECTED_SOURCE_CHANGES", pins):
            self.assertEqual(controller.verify_source(SimpleNamespace(validate_package=validate), "a" * 64)[2], sorted(pins))

    def test_removed_source_fails_before_build(self):
        before = {ACTION: b"before", "backend/app/keep.py": b"keep"}
        after = {ACTION: b"after"}
        def validate(path, _sha):
            data = before if path == controller.OLD_SOURCE else after
            return {"source_sha256": "old" if data is before else "new"}, [(n, b, 0o644) for n, b in data.items()]
        with patch.object(controller, "read", side_effect=lambda p, m: before[p.relative_to(controller.OLD_RELEASE).as_posix()]), \
                patch.object(controller, "EXPECTED_SOURCE_CHANGES", {ACTION: controller.digest(b"after")}):
            with self.assertRaises(controller.Failure) as failure:
                controller.verify_source(SimpleNamespace(validate_package=validate), "a" * 64)
        self.assertEqual(failure.exception.code, "unexpected_source_file_removal")

    def test_invalid_allowlist_is_rejected_before_host_io(self):
        for pins in ({}, {"backend/app/cloud/secret.py": "a" * 64},
                     {"frontend/src/../private.ts": "a" * 64}, {ACTION: "FINALIZE"}):
            with self.subTest(pins=pins), patch.object(controller, "SOURCE_SHA256", "a" * 64), \
                    patch.object(controller, "OLD_OVERRIDE_SHA256", "b" * 64), \
                    patch.object(controller, "EXPECTED_SOURCE_CHANGES", pins):
                with self.assertRaises(controller.Failure):
                    controller.finalized_pins()

    def test_build_can_only_target_web_with_no_network_or_pull(self):
        import ast
        source = Path(controller.__file__).read_text()
        tree = ast.parse(source)
        forbidden = ("database", "migrat", "seed", "refresh", "cancel", "acceptance", "prompt")
        arguments = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                     and isinstance(node.func, ast.Name) and node.func.id == "compose"]
        mutation = [node for node in arguments if node.args and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == "up"]
        self.assertEqual(len(mutation), 1)
        self.assertEqual([x.value for x in mutation[0].args if isinstance(x, ast.Constant)][-1], "web")
        self.assertIn("'--network', 'none', '--pull=false'", source)
        self.assertNotIn("docker', 'stop'", source)
        self.assertNotIn("docker', 'rm'", source)
        self.assertNotIn("docker', 'restart'", source)

    def compose(self, new=False):
        services = {name: {"image": controller.API, "environment": {"RUNTIME_IMAGE": controller.RUNTIME},
                           "volumes": ["same-volume"], "build": {"context": "new" if new else "old", "dockerfile": "same-api"}}
                    for name in ("api", "worker", "migrate")}
        services["web"] = {"image": controller.WEB if new else controller.OLD_WEB,
                           "build": {"context": "new" if new else "old", "dockerfile": "new-web" if new else "old-web"}}
        services["db"] = {"image": "same-db", "volumes": ["same-db-volume"]}
        services["proxy"] = {"image": "same-proxy", "ports": ["8443:8443"]}
        return {"name": controller.PROJECT, "services": services}

    def test_compose_allows_only_web_image_recipe_and_source_context_changes(self):
        before, after = self.compose(), self.compose(True)
        with patch.object(controller, "command", side_effect=[json.dumps(before).encode(), json.dumps(after).encode()]):
            controller.compose_contract()
        after["services"]["api"]["environment"]["RUNTIME_IMAGE"] = "changed-runtime"
        with patch.object(controller, "command", side_effect=[json.dumps(before).encode(), json.dumps(after).encode()]):
            with self.assertRaises(controller.Failure) as failure:
                controller.compose_contract()
        self.assertEqual(failure.exception.code, "non_web_compose_contract_changed")

    def rows(self):
        rows = {}
        for index, name in enumerate(controller.SERVICES):
            api = name in {"api", "worker"}
            rows[name] = {"id": str(index) * 64, "name": name, "image": controller.API if api else name,
                          "image_id": controller.BASE_IDS[controller.API] if api else "sha256:" + "a" * 64,
                          "started_at": "same", "mounts": [name], "running": True, "oom_killed": False,
                          "health": "healthy" if name in {"api", "web", "db"} else None}
        return rows

    def test_non_web_container_restart_is_rejected(self):
        before = self.rows()
        after = copy.deepcopy(before)
        after["web"]["id"] = "b" * 64
        with patch.object(controller, "tenant_inventory", return_value={}):
            controller.preserved(before, after, {})
            after["worker"]["id"] = "c" * 64
            with self.assertRaises(controller.Failure) as failure:
                controller.preserved(before, after, {})
        self.assertEqual(failure.exception.code, "non_web_container_changed")

    def test_original_tenant_identity_and_running_state_are_preserved(self):
        rows = self.rows()
        tenant = copy.deepcopy(rows["worker"])
        current = {"tenant": copy.deepcopy(tenant)}
        with patch.object(controller, "tenant_inventory", return_value=current):
            controller.preserved(rows, rows, {"tenant": tenant})
            current["tenant"]["started_at"] = "restarted"
            with self.assertRaises(controller.Failure) as failure:
                controller.preserved(rows, rows, {"tenant": tenant})
        self.assertEqual(failure.exception.code, "tenant_container_changed")

    def test_missing_optional_health_field_is_supported(self):
        raw = json.dumps({"id": "a" * 64, "name": "worker", "image": controller.API,
                          "image_id": controller.BASE_IDS[controller.API], "mounts": [],
                          "state": {"StartedAt": "same", "Running": True, "OOMKilled": False}}).encode()
        with patch.object(controller, "command", return_value=raw):
            value = controller.container("worker")
        self.assertIsNone(value["health"])
        self.assertTrue(value["running"])


if __name__ == "__main__":
    unittest.main()
