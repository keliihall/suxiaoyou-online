"""Offline safety contracts for a web-only composer control release."""
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

SPEC = importlib.util.spec_from_file_location("composer_controller", Path(__file__).with_name("upgrade-composer-controls.py"))
controller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(controller)
ACTION = "frontend/src/components/chat/chat-actions.tsx"


class ComposerControllerTests(unittest.TestCase):
    def test_unfinalized_controller_refuses_before_host_io(self):
        with patch.object(sys, "argv", ["upgrade-composer-controls.py", "build", "--source-sha256", "a" * 64]), \
                patch.object(controller, "SOURCE_SHA256", "FINALIZE"), \
                patch.object(controller, "environment") as environment, patch.object(controller, "command") as command, \
                patch.object(controller, "write") as write, patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(controller.main(), 1)
        self.assertEqual(json.loads(output.getvalue())["failure"], "release_pins_not_finalized")
        environment.assert_not_called()
        command.assert_not_called()
        write.assert_not_called()

    def test_baseline_pins_and_original_controller_are_preserved(self):
        self.assertEqual(controller.OLD_VERSION, "20261117-incident2")
        self.assertEqual(controller.VERSION, "20261117-composer")
        self.assertEqual(controller.OLD_SOURCE_SHA256, "0021fdaa6e0099df84b77f68d70f21339a3521e4a3f9a8f33885f0aa68b5ec03")
        self.assertEqual(controller.BASE_IDS[controller.API], "sha256:cc0d81fd35c73648b24ff2de58b58191f2ed2c606d358e3dcce3915353bcec08")
        self.assertEqual(controller.BASE_IDS[controller.RUNTIME], "sha256:009335107e865474a823d19615a26d31b60783497d104a4b7941e7caee9de4d7")
        self.assertEqual(controller.BASE_IDS[controller.OLD_WEB], "sha256:48bbdd122b426d0283a799cbd0ab408293d524bb3608925e285114f7ae08cf82")
        old = Path(__file__).with_name("upgrade-web-defaults.py")
        self.assertEqual(hashlib.sha256(old.read_bytes()).hexdigest(), "406a7b4f3d1b43e13104503e9a8d9026b1ed554f7e203957d3c90ad90423daaf")

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
