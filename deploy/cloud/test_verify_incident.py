"""Offline scope/aggregate tests for the read-only incident verifier."""
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest


SPEC = importlib.util.spec_from_file_location("verify_incident_v2", Path(__file__).with_name("verify-incident-v2.py"))
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


class SourceScopeTests(unittest.TestCase):
    def source(self):
        return {"data/fonts/font.ttf": "a" * 64, "session/processor.py": "b" * 64,
                "database.py": "c" * 64, "data_model.py": "d" * 64}

    def test_api_obeys_pinned_data_directory_exclusion(self):
        expected, proof = verifier.source_expectation(self.source(), "/app/app")
        self.assertEqual(set(expected), {"session/processor.py", "database.py", "data_model.py"})
        self.assertEqual(proof["files"], 3)

    def test_tenant_verifies_bundled_data_and_shared_code(self):
        source = self.source()
        expected, tenant_proof = verifier.source_expectation(source, "/opt/backend/app")
        api, api_proof = verifier.source_expectation(source, "/app/app")
        self.assertEqual(expected, source)
        self.assertTrue(all(expected[name] == value for name, value in api.items()))
        self.assertEqual(tenant_proof["files"], 4)
        self.assertNotEqual(tenant_proof["sha256"], api_proof["sha256"])

    def test_each_scope_uses_exact_deterministic_manifest_aggregate(self):
        source = self.source()
        expected, proof = verifier.source_expectation(source, "/app/app")
        reverse, reverse_proof = verifier.source_expectation(dict(reversed(list(source.items()))), "/app/app")
        raw = json.dumps([[name, value] for name, value in sorted(expected.items())], separators=(",", ":")).encode()
        self.assertEqual(proof, reverse_proof)
        self.assertEqual(expected, reverse)
        self.assertEqual(proof["sha256"], hashlib.sha256(raw).hexdigest())

    def test_scope_refuses_unrecognized_root_and_empty_api(self):
        for source, root in ((self.source(), "/tenant"), ({"data/file": "a" * 64}, "/app/app")):
            with self.subTest(root=root), self.assertRaises(verifier.Failure):
                verifier.source_expectation(source, root)


if __name__ == "__main__":
    unittest.main()
