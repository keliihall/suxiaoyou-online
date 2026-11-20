"""Offline SDK wire checks for the API-owned reasoning-intent canary."""
import hashlib
import importlib.util
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
import time

import httpx
import pytest


ROOT = Path(__file__).resolve().parents[3]


def load(name):
    path = ROOT / "deploy/cloud" / name
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


original = load("acceptance-incident.py")
helper = load("acceptance-incident-v2.py")
RID = "17aaf027e6154d11b89b3ec439ad6cc9"


def test_original_helper_is_immutable_and_runtime_mode_is_identical():
    assert hashlib.sha256((ROOT / "deploy/cloud/acceptance-incident.py").read_bytes()).hexdigest() == (
        "5728998d71cf3c7dbf211e3214e91b89d22d47be6be0f972615b7b98da435c83")
    for function in ["run_runtime", "build_prompt", "claim_once", "private_json", "runtime_token"]:
        assert inspect.getsource(getattr(original, function)) == inspect.getsource(getattr(helper, function))


def test_probe_does_not_decide_thinking_mode_or_effort_for_the_api():
    body = helper.gateway_probe_options("deepseek-office")
    assert body["extra_body"] == {"reasoning": {"enabled": True}}
    assert body["max_tokens"] == 32
    assert "thinking" not in body and "reasoning_effort" not in body
    assert "tools" not in body and body["stream_options"] == {"include_usage": True}


@pytest.mark.parametrize("reasoning", [0, 32])
async def test_real_sdk_wire_contains_only_reasoning_hint_and_requires_positive_receipt(monkeypatch, tmp_path, reasoning):
    sent = []
    def receive(request):
        sent.append(request)
        if request.method == "GET":
            assert request.url.path.endswith("/models")
            return httpx.Response(200, json={"object": "list", "data": [{"id": "deepseek-office", "object": "model",
                "created": 0, "owned_by": "suxiaoyou", "metadata": {"upstream_provider": "deepseek"},
                "capabilities": {"reasoning": True}}]})
        wire = json.loads(request.content)
        assert wire["reasoning"] == {"enabled": True}
        assert "thinking" not in wire and "reasoning_effort" not in wire and "tools" not in wire
        assert wire["max_tokens"] == 32 and wire["model"] == "deepseek-office"
        frame = {"id": "synthetic", "object": "chat.completion.chunk", "created": 1, "model": "deepseek-office",
            "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 32, "total_tokens": 42,
                                      "completion_tokens_details": {"reasoning_tokens": reasoning}}}
        error = {"error": {"code": "model_output_exhausted", "type": "model_output_exhausted",
            "message": "PRIVATE_DIAGNOSTIC", "request_id": RID, "retryable": False}}
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=(
            "data: " + json.dumps(frame) + "\n\ndata: " + json.dumps(error) + "\n\ndata: [DONE]\n\n"))
    initialize = httpx.AsyncClient.__init__
    def install_transport(client, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(receive)
        initialize(client, *args, **kwargs)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", install_transport)
    monkeypatch.setenv("CLOUD_ENGINE_GATEWAY_URL", "https://gateway.invalid/api/cloud/internal/inference/v1")
    monkeypatch.setenv("CLOUD_ENGINE_CAPABILITY", "synthetic-capability-only-for-this-offline-test")
    state_path, report, claimed = helper.claim_once(tmp_path, RID, "gateway")
    assert claimed
    result = await helper.run_gateway(SimpleNamespace(route="deepseek-office"), report, state_path, time.monotonic() + 10)
    assert result["ok"] is (reasoning > 0)
    assert result["gateway_model_dispatches"] == 1
    assert result["request_id"] == RID
    assert result["usage"]["reasoning"] == reasoning
    assert result["terminal_codes"] == ["model_output_exhausted"]
    assert len([request for request in sent if request.method == "POST"]) == 1
    assert "PRIVATE_DIAGNOSTIC" not in json.dumps(result)


def test_v1_guard_also_blocks_v2_replay_of_the_same_run_identity(tmp_path):
    path, report, claimed = original.claim_once(tmp_path, RID, "gateway")
    assert claimed
    report.update(gateway_model_dispatches=1, phase="gateway_dispatched_unknown")
    original.private_json(path, report)
    _, same, claimed = helper.claim_once(tmp_path, RID, "gateway")
    assert not claimed and same["gateway_model_dispatches"] == 1
