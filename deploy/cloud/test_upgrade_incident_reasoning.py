"""Reuse the incident controller contracts for the isolated reasoning follow-up."""
import hashlib
import importlib.util
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).parent


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


base = module("base_incident_controller_tests", "test_upgrade_incident.py")
controller = module("reasoning_controller", "upgrade-incident-reasoning.py")
runtime = module("reasoning_runtime", "incident-runtime-v2.py")


class ReasoningControllerTests(base.IncidentControllerTests):
    def setUp(self):
        self.controller_patch = patch.object(base, "controller", controller)
        self.controller_patch.start()
        self.addCleanup(self.controller_patch.stop)

    def test_mixed_previous_release_images_are_pinned(self):
        self.assertEqual(controller.OLD_VERSION, "20261117-incident")
        self.assertEqual(controller.OLD_BACKEND_VERSION, "20261117-incident")
        self.assertEqual(controller.VERSION, "20261117-incident2")
        self.assertEqual(controller.OLD_CONFIG, controller.OLD_BACKEND_CONFIG)
        self.assertEqual(controller.OLD_IMAGES, {
            "api": "suxiaoyou-cloud-api:20261117-incident",
            "web": "suxiaoyou-cloud-web:20261117-incident",
            "runtime": "suxiaoyou-online-runtime:20261117-incident",
        })
        self.assertEqual(controller.BASE_IDS[controller.OLD_IMAGES["api"]],
                         "sha256:c3190ce2b01fd4907dbdfe56018d92cde2053ade5cc97bb495fb70d29d0a3e17")
        self.assertEqual(controller.BASE_IDS[controller.OLD_IMAGES["runtime"]],
                         "sha256:9f724829f86b261560030e11203aeb6c38e77bda7231cc33062f5cc67419859f")
        self.assertEqual(controller.BASE_IDS[controller.OLD_IMAGES["web"]],
                         "sha256:efe25a1d23943efc073b10bf9f873dee7b20677f73c6d6f21b7d0fe2fcf76614")

    def test_runtime_helper_only_changes_its_operation_namespace(self):
        old = (ROOT / "incident-runtime.py").read_bytes()
        new = (ROOT / "incident-runtime-v2.py").read_bytes()
        self.assertEqual(hashlib.sha256(old).hexdigest(),
                         "2982b138814c81712de88a1969d00c90ad13d61aadd73a23bd2a6cb972d25a03")
        self.assertEqual(new, old.replace(b'OPERATION = "20261117-incident"',
                                          b'OPERATION = "20261117-incident2"'))
        self.assertEqual(hashlib.sha256(new).hexdigest(), controller.HELPER_SHA256)
        self.assertEqual(runtime.OPERATION, controller.VERSION)
        self.assertEqual(controller.HELPER.name, "incident-runtime-v2.py")
        self.assertEqual(runtime.progress_name(False), "20261117-incident2-forward.json")
        self.assertEqual(runtime.progress_name(True), "20261117-incident2-rollback.json")

    def test_followup_tag_prefix_does_not_look_like_the_old_image(self):
        old = "\n".join("  image: " + tag for tag in controller.OLD_IMAGES.values())
        new = "\n".join("  image: " + tag for tag in controller.IMAGES.values())
        for tag in controller.OLD_IMAGES.values():
            self.assertTrue(controller.has_image_tag(old, tag))
            self.assertTrue(controller.has_image_tag('image: "' + tag + '"', tag))
            self.assertFalse(controller.has_image_tag(new, tag))

    def test_source_baseline_is_the_frozen_first_incident_release(self):
        self.assertEqual(controller.OLD_SOURCE.name, "incident-20261117.tar.gz")
        self.assertEqual(controller.OLD_SOURCE_SHA256,
                         "657c0e84901b429c4f3e9851d36e318869d8c8e72cc414d2bb4993b5a311d0e3")
        self.assertEqual(controller.SOURCE.name, "incident2-20261117.tar.gz")
        self.assertEqual(hashlib.sha256((ROOT / "upgrade-incident.py").read_bytes()).hexdigest(),
                         "3577adca6ac2f0422a538b49d138a4f71f4dcd28b62d5a37e705adbc7807890c")
        self.assertEqual(hashlib.sha256((ROOT / "verify-incident-v2.py").read_bytes()).hexdigest(),
                         "614ea635ec7846c7991afa4833c4490f5fa4535b66bd769420c65e09b46e3e53")
