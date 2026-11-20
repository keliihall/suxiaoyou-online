"""Offline contract tests; never contact Docker, the server, or a provider."""
from __future__ import annotations

import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


SPEC = importlib.util.spec_from_file_location("upgrade_incident", Path(__file__).with_name("upgrade-incident.py"))
controller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(controller)
RUNTIME_SPEC = importlib.util.spec_from_file_location("incident_runtime", Path(__file__).with_name("incident-runtime.py"))
runtime = importlib.util.module_from_spec(RUNTIME_SPEC)
RUNTIME_SPEC.loader.exec_module(runtime)


class IncidentControllerTests(unittest.TestCase):
    def pins(self):
        return patch.multiple(controller, SOURCE_SHA256="a" * 64,
                              OLD_OVERRIDE_SHA256="b" * 64,
                              EXPECTED_SOURCE_CHANGES={"backend/app/example.py": "c" * 64})

    def test_unfinalized_controller_does_not_reach_host_operations(self):
        with patch.object(sys, "argv", ["upgrade-incident.py", "build", "--source-sha256", "a" * 64]), \
                patch.object(controller, "SOURCE_SHA256", "FINALIZE_SOURCE_ARCHIVE_SHA256"), \
                patch.object(controller, "environment") as environment, \
                patch.object(controller, "command") as command, \
                patch.object(controller, "write") as write, \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(controller.main(), 1)
        self.assertEqual(json.loads(output.getvalue())["failure"], "release_pins_not_finalized")
        environment.assert_not_called()
        command.assert_not_called()
        write.assert_not_called()

    def test_final_pins_require_an_exact_source_allowlist(self):
        with self.pins():
            controller.finalized_pins()
            with patch.object(controller, "EXPECTED_SOURCE_CHANGES", {}):
                with self.assertRaises(controller.Failure) as failure:
                    controller.finalized_pins()
                self.assertEqual(failure.exception.code, "source_allowlist_not_finalized")

    def test_mixed_previous_release_images_are_pinned(self):
        self.assertEqual(controller.OLD_VERSION, "20261116-0235-auto")
        self.assertEqual(controller.OLD_IMAGES["api"], "suxiaoyou-cloud-api:20261116-0205-taskfix")
        self.assertEqual(controller.OLD_IMAGES["runtime"], "suxiaoyou-online-runtime:20261116-0205-taskfix")
        self.assertEqual(controller.OLD_IMAGES["web"], "suxiaoyou-cloud-web:20261116-0235-auto")

    def source(self, changed=None):
        before = {"compose.cloud.yaml": b"same-compose", "backend/app/example.py": b"before"}
        after = dict(before, **{"backend/app/example.py": b"after"})
        if changed:
            after.update(changed)
        def validate(path, _sha):
            content = before if path == controller.OLD_SOURCE else after
            manifest = {"source_sha256": "before" if content is before else "after"}
            return manifest, [(name, value, 0o644) for name, value in content.items()]
        master = SimpleNamespace(validate_package=validate)
        def read(path, _maximum):
            return before[path.relative_to(controller.OLD_RELEASE).as_posix()]
        return master, read

    def test_only_reviewed_source_bytes_are_accepted(self):
        master, read = self.source()
        allowlist = {"backend/app/example.py": controller.digest(b"after")}
        with patch.object(controller, "read", side_effect=read), \
                patch.object(controller, "EXPECTED_SOURCE_CHANGES", allowlist):
            self.assertEqual(controller.verify_source(master, "a" * 64)[2], ["backend/app/example.py"])

    def test_unreviewed_source_and_dependency_changes_are_rejected(self):
        for extra, expected in (({"frontend/src/unreviewed.ts": b"extra"}, "source_change_allowlist_mismatch"),
                                ({"compose.cloud.yaml": b"different"}, "dependencies_or_schema_changed")):
            with self.subTest(expected=expected):
                master, read = self.source(extra)
                with patch.object(controller, "read", side_effect=read), \
                        patch.object(controller, "EXPECTED_SOURCE_CHANGES", {"backend/app/example.py": controller.digest(b"after")}):
                    with self.assertRaises(controller.Failure) as failure:
                        controller.verify_source(master, "a" * 64)
                self.assertEqual(failure.exception.code, expected)

    def test_service_without_optional_health_field_is_supported(self):
        raw = json.dumps({"id": "a" * 64, "image_id": "sha256:" + "b" * 64,
                          "image": "worker", "state": {"Status": "running", "Running": True,
                          "OOMKilled": False, "ExitCode": 0, "StartedAt": "date"}, "mounts": []}).encode()
        with patch.object(controller, "command", return_value=raw):
            value = controller.service("worker")
        self.assertNotIn("Health", value["state"])
        controller.health({"worker": value})

    def compose(self, tags):
        services = {name: {"image": tags["api"], "environment": {}, "volumes": ["same-volume"],
                           "build": {"context": str(controller.RELEASE), "dockerfile": "new-api"}}
                    for name in ("api", "worker", "migrate")}
        services["api"]["environment"]["SUXIAOYOU_CLOUD_RUNTIME_IMAGE"] = tags["runtime"]
        services["web"] = {"image": tags["web"], "build": {"context": str(controller.RELEASE), "dockerfile": "new-web"}}
        services["db"] = {"image": "pinned-db", "volumes": ["preserve-db"]}
        services["proxy"] = {"image": "pinned-proxy", "ports": ["8443:8443"]}
        return {"name": controller.PROJECT, "services": services}

    def test_compose_accepts_only_declared_image_and_recipe_changes(self):
        before, after = self.compose(controller.OLD_IMAGES), self.compose(controller.IMAGES)
        before["services"]["api"]["build"]["dockerfile"] = "old-api"
        with patch.object(controller, "command", side_effect=[json.dumps(before).encode(), json.dumps(after).encode()]):
            controller.compose_contract()

    def test_compose_rejects_changed_mount_or_credential(self):
        before, after = self.compose(controller.OLD_IMAGES), self.compose(controller.IMAGES)
        for service, key, value in (("api", "volumes", ["different-volume"]),
                                    ("worker", "environment", {"PRIVATE_KEY": "changed"})):
            with self.subTest(service=service, key=key):
                changed = copy.deepcopy(after)
                changed["services"][service][key] = value
                with patch.object(controller, "command", side_effect=[json.dumps(before).encode(), json.dumps(changed).encode()]):
                    with self.assertRaises(controller.Failure) as failure:
                        controller.compose_contract()
                self.assertEqual(failure.exception.code, "rendered_compose_contract_changed")


class PreRefreshRollbackTests(unittest.IsolatedAsyncioTestCase):
    def values(self):
        baseline = {"runtime_image": "old-runtime", "inventory": [["user", 1, "tenant", "container", "running"]],
                    "tenants": [{"owner_id": "owner", "state": "running", "container_id": "container",
                                 "image_id": "old-image", "runtime_image": "old-runtime", "volume_name": "same-volume"}]}
        current = copy.deepcopy(baseline)
        forward = {"stage": "snapshotted", "baseline": baseline, "applied": []}
        request = {"mode": "refresh", "baseline": baseline, "rollback": True, "runtime_image": "old-runtime"}
        return baseline, current, forward, request

    def test_pristine_snapshot_allows_service_only_rollback(self):
        baseline, current, forward, request = self.values()
        runtime.pristine_rollback(forward, baseline, current, request)

    def test_no_refresh_rollback_rejects_any_tenant_change(self):
        for case in ("applied", "inventory", "image", "volume", "runtime", "stage"):
            with self.subTest(case=case):
                baseline, current, forward, request = self.values()
                if case == "applied":
                    forward["applied"] = [{"user_id": "user"}]
                elif case == "inventory":
                    current["inventory"][0][1] = 2
                elif case == "image":
                    current["tenants"][0]["image_id"] = "changed-image"
                elif case == "volume":
                    current["tenants"][0]["volume_name"] = "changed-volume"
                elif case == "runtime":
                    request["runtime_image"] = "changed-runtime"
                elif case == "stage":
                    forward["stage"] = "applying"
                with self.assertRaises(runtime.Failure):
                    runtime.pristine_rollback(forward, baseline, current, request)

    async def test_pre_refresh_rollback_never_starts_a_tenant(self):
        baseline, current, forward, request = self.values()
        settings = SimpleNamespace(environment="production", cookie_secure=True, allow_mock_provider=False,
                                   auto_migrate=False, full_runtime_enabled=True, runtime_network_mode=True,
                                   database_url="postgresql+asyncpg://private", data_dir=str(runtime.DATA),
                                   allowed_origin=runtime.ORIGIN, runtime_image="old-runtime")
        database = SimpleNamespace(initialize=AsyncMock(), close=AsyncMock())
        supervisor = SimpleNamespace(start=AsyncMock())
        office_gateway = Mock(side_effect=AssertionError("tenant refresh must not initialize office signing"))
        modules = {}
        for name, attributes in {
            "app.cloud.config": {"CloudSettings": lambda: settings},
            "app.cloud.database": {"Database": lambda _settings: database},
            "app.cloud.runtime": {"RuntimeSupervisor": lambda _database, _settings: supervisor},
            "app.cloud.office_gateway": {"OfficeGateway": office_gateway},
        }.items():
            module = ModuleType(name)
            module.__dict__.update(attributes)
            modules[name] = module
        public = SimpleNamespace(utcnow=lambda: "now")
        with patch.dict(sys.modules, modules), \
                patch.object(runtime, "image_catalog", new=AsyncMock(return_value={})), \
                patch.object(runtime, "capture", new=AsyncMock(return_value=current)), \
                patch.object(runtime, "baseline_valid"), patch.object(runtime, "unchanged"), \
                patch.object(runtime, "refresh_inventory"), \
                patch.object(runtime, "read_progress", return_value=forward), \
                patch.object(runtime, "save_progress") as save_progress:
            result = await runtime.operate(public, request, 7, {"document": None})
        self.assertEqual(result, (current, 0))
        supervisor.start.assert_not_called()
        office_gateway.assert_not_called()
        database.close.assert_awaited_once()
        document = save_progress.call_args.args[2]
        self.assertEqual(document["stage"], "complete")
        self.assertTrue(document["no_tenant_refresh"])
        self.assertEqual(document["applied"], [])


if __name__ == "__main__":
    unittest.main()
