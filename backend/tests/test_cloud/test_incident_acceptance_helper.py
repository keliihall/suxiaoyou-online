"""Pure acceptance-helper contracts; never run its CLI or request a model."""
import hashlib
import importlib.util
import json
from pathlib import Path
from collections import Counter

import pytest


SOURCE = Path(__file__).resolve().parents[3] / "deploy/cloud/acceptance-incident.py"
spec = importlib.util.spec_from_file_location("incident_acceptance_helper", SOURCE)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
RID = "17aaf027e6154d11b89b3ec439ad6cc9"


def test_once_guard_survives_unknown_dispatch_and_never_reclaims(tmp_path):
    path, report, claimed = helper.claim_once(tmp_path, RID, "runtime")
    assert claimed and report["prompt_dispatches"] == 0
    report.update(prompt_dispatches=1, phase="prompt_dispatched_unknown")
    helper.private_json(path, report)
    same_path, same_report, claimed = helper.claim_once(tmp_path, RID, "runtime")
    assert same_path == path and not claimed
    assert same_report["prompt_dispatches"] == 1 and same_report["phase"] == "prompt_dispatched_unknown"
    assert path.stat().st_mode & 0o777 == 0o600
    assert not path.with_name(path.name + ".next").exists()


def test_prompt_authorizes_only_unique_file_not_commands_or_other_files(tmp_path):
    from app.schemas.agent import Ruleset
    body = helper.build_prompt(RID, tmp_path, "deepseek-office")
    assert body["reasoning"] is True and body["agent"] == "build"
    assert body["permission_presets"] == {"file_changes": True, "run_commands": False}
    rules = Ruleset(rules=body["permission_rules"])
    assert rules.evaluate("write", str(tmp_path / "acceptance.txt")) == "allow"
    for tool, path in [("bash", "*"), ("code_execute", "*"), ("web_fetch", "*"), ("task", "*"),
                       ("write", "/tenant/state/session_token.json"), ("write", str(tmp_path / "other.txt"))]:
        assert rules.evaluate(tool, path) == "deny"
    assert body["client_request_id"] == "incident-" + RID
    assert "session_id" not in body


def test_artifact_is_exact_checked_without_following_links(tmp_path):
    path = tmp_path / "acceptance.txt"
    expected = helper.expected_artifact(RID)
    assert helper.artifact_digest(path, expected) == {"file_exists": False, "file_exact": False}
    path.write_bytes(expected)
    assert helper.artifact_digest(path, expected) == {"file_exists": True, "file_exact": True,
        "file_sha256": hashlib.sha256(expected).hexdigest()}
    link = tmp_path / "linked.txt"
    link.symlink_to(path)
    with pytest.raises(OSError):
        helper.artifact_digest(link, expected)


def test_summary_never_includes_event_bodies_and_deduplicates_receipts():
    report = {"event_counts": Counter(), "usage": {}, "tool_names": set(), "terminal_codes": [], "write_results": 0}
    seen = set()
    for _ in range(2):
        helper.apply_event(report, "step-finish", {"tokens": {"input": 100, "reasoning": 8192, "total": 8292}}, "3", seen)
    helper.apply_event(report, "agent-error", {"code": "model_output_exhausted", "error_message": "PRIVATE",
        "request_id": RID, "upstream_status": True}, "4", seen)
    report["private_vendor_payload"] = "PRIVATE"
    summary = helper.plain_report(report)
    assert summary["usage"]["total"] == 8292
    assert summary["terminal_codes"] == ["model_output_exhausted"]
    assert summary["request_id"] == RID and "upstream_status" not in summary
    assert "PRIVATE" not in json.dumps(summary)


@pytest.mark.parametrize("run_id", ["", "../x", RID.upper(), RID + "\n", "x" * 32])
def test_invalid_identity_is_rejected_before_claiming(tmp_path, run_id):
    with pytest.raises(helper.AcceptanceFailure):
        helper.claim_once(tmp_path, run_id, "runtime")
    assert not list(tmp_path.iterdir())
