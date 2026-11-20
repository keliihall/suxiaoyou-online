"""Real SDK/ASGI wire boundaries; no network, credentials or model calls."""
import base64
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.cloud import inference, inference_payload
from app.cloud.config import CloudSettings
from app.cloud.gateway_errors import admission_chunk
from app.cloud.gateway_provider import GatewayProvider
from app.cloud.main import create_app

PATH = "/api/cloud/internal/inference/v1/chat/completions"


def response_stream(request):
    frame = {"id": "offline", "object": "chat.completion.chunk", "created": 1, "model": "bounded",
             "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          content="data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n")


def sdk_provider(monkeypatch, sent):
    # Preserve the production client's actual event hooks while replacing only
    # its network transport. This exercises SDK encoding and exception wrapping.
    original = httpx.AsyncClient.__init__

    def initialize(client, *args, **kwargs):
        def receive(request):
            sent.append(request.content)
            return response_stream(request)
        kwargs["transport"] = httpx.MockTransport(receive)
        original(client, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", initialize)
    return GatewayProvider(api_key="synthetic-capability", provider_id="custom_cloud",
                           base_url="https://gateway.invalid/v1")


async def chunks(provider, messages, **kwargs):
    return [chunk async for chunk in provider.stream_chat("bounded", messages, **kwargs)]


async def test_original_read_twenty_mib_image_is_preserved_by_actual_sdk(monkeypatch):
    assert inference_payload.MAX_INFERENCE_BODY_BYTES == 32 * 1024 * 1024
    uri = "data:image/png;base64," + base64.b64encode(b"\0" * (20 * 1024 * 1024)).decode("ascii")
    messages = [{"role": "user", "content": [{"type": "text", "text": "检查原图"},
                 {"type": "image_url", "image_url": {"url": uri}}]}]
    sent = []
    provider = sdk_provider(monkeypatch, sent)
    try:
        result = await chunks(provider, messages)
        assert any(row.type == "text-delta" for row in result)
        assert len(sent) == 1 and len(sent[0]) < inference_payload.MAX_INFERENCE_BODY_BYTES
        assert json.loads(sent[0])["messages"] == messages
    finally:
        await provider._client.close()


async def test_five_report_sized_images_are_kept_for_comparison(monkeypatch):
    image_lengths = [1104366, 697814, 1103622, 672650, 669094]
    prefix = "data:image/png;base64,"
    messages = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": prefix + "A" * (size - len(prefix))}}
        for size in image_lengths
    ]}]
    sent = []
    provider = sdk_provider(monkeypatch, sent)
    try:
        await chunks(provider, messages)
        assert len(sent) == 1 and len(sent[0]) > 4 * 1024 * 1024
        assert json.loads(sent[0])["messages"] == messages
    finally:
        await provider._client.close()


async def test_sdk_exact_bytes_pass_and_one_extra_byte_rejects_before_transport(monkeypatch):
    monkeypatch.setattr(inference_payload, "MAX_INFERENCE_BODY_BYTES", 2048)
    sent = []
    provider = sdk_provider(monkeypatch, sent)
    try:
        options = {"system": "固定系统提示", "extra_body": {"thinking": {"type": "enabled"}},
                   "tools": [{"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}]}
        await chunks(provider, [{"role": "user", "content": ""}], **options)
        padding = 2048 - len(sent[0])
        await chunks(provider, [{"role": "user", "content": "x" * padding}], **options)
        assert len(sent[-1]) == 2048
        assert json.loads(sent[-1])["thinking"] == {"type": "enabled"}
        result = await chunks(provider, [{"role": "user", "content": "x" * (padding + 1)}], **options)
        assert len(sent) == 2
        assert len(result) == 1 and result[0].type == "error"
        assert result[0].data == {"code": "model_request_too_large",
                                  "message": inference_payload.MODEL_REQUEST_TOO_LARGE_MESSAGE,
                                  "retryable": False}
    finally:
        await provider._client.close()


async def test_sdk_utf8_size_cannot_bypass_limit_with_small_character_count(monkeypatch):
    monkeypatch.setattr(inference_payload, "MAX_INFERENCE_BODY_BYTES", 1024)
    sent = []
    provider = sdk_provider(monkeypatch, sent)
    try:
        result = await chunks(provider, [{"role": "user", "content": "图" * 400}])
        assert sent == []
        assert result[0].data["code"] == "model_request_too_large"
        assert result[0].data["retryable"] is False
    finally:
        await provider._client.close()


@pytest.fixture
def gateway_app(monkeypatch, tmp_path):
    admitted = []
    route = SimpleNamespace(enabled=True, provider="deepseek", kind="chat", owner_user_id=None)

    class Database:
        async def get(self, *_args):
            return route

    class Attempt:
        def __init__(self, request, body, user, route, provider):
            self.id = None
            self.body = body

        async def reserve(self):
            self.id = "offline"
            admitted.append(self.body)

        async def completion(self):
            return {"accepted": True}

        async def finalize(self):
            pass

    async def database():
        yield Database()

    async def principal():
        return SimpleNamespace(id="offline-user")

    monkeypatch.setattr(inference, "InferenceAttempt", Attempt)
    monkeypatch.setattr(inference, "build_provider", lambda *_args: object())
    app = create_app(CloudSettings(environment="test", database_url="sqlite+aiosqlite://",
                                   data_dir=tmp_path / "cloud", cookie_secure=False))
    app.dependency_overrides[inference.get_db] = database
    app.dependency_overrides[inference.runtime_principal] = principal
    return app, admitted


def wire(content):
    return json.dumps({"model": "bounded", "messages": [{"role": "user", "content": content}]},
                      ensure_ascii=False, separators=(",", ":")).encode("utf-8")


@pytest.mark.parametrize("chunked", [False, True])
async def test_asgi_exact_boundary_and_overlimit_have_same_safe_error(gateway_app, monkeypatch, chunked):
    app, admitted = gateway_app
    monkeypatch.setattr(inference_payload, "MAX_INFERENCE_BODY_BYTES", 1024)
    padding = 1024 - len(wire(""))

    async def content(raw):
        # One byte at a time is unnecessary; split across the cap to exercise
        # cumulative received bytes without relying on Content-Length.
        yield raw[:512]
        yield raw[512:]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offline.invalid") as client:
        accepted = wire("x" * padding)
        rejected = wire("x" * (padding + 1))
        headers = {"Content-Type": "application/json"}
        response = await client.post(PATH, content=content(accepted) if chunked else accepted, headers=headers)
        assert response.status_code == 200 and len(admitted) == 1
        response = await client.post(PATH, content=content(rejected) if chunked else rejected, headers=headers)
        assert response.status_code == 413
        assert response.json() == inference_payload.payload_error_body()
        assert len(admitted) == 1
        assert admission_chunk(SimpleNamespace(body=response.json()))["retryable"] is False
        assert "xxxxx" not in response.text


async def test_handler_without_middleware_checks_raw_utf8_before_admission(gateway_app, monkeypatch):
    source, admitted = gateway_app
    app = FastAPI()
    app.include_router(inference.router)
    app.dependency_overrides = source.dependency_overrides
    app.state.settings = source.state.settings
    monkeypatch.setattr(inference_payload, "MAX_INFERENCE_BODY_BYTES", 1024)
    raw = wire("图" * 400)
    assert len(raw.decode("utf-8")) < 1024 < len(raw)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offline.invalid") as client:
        response = await client.post(PATH, content=raw, headers={"Content-Type": "application/json"})
        assert response.status_code == 413
        assert response.json() == inference_payload.payload_error_body()
        assert admitted == []


def test_exception_cause_and_remote_rejection_never_echo_details():
    secret = "PRIVATE_REQUEST_OR_VENDOR_DETAIL"
    wrapped = RuntimeError(secret)
    wrapped.__cause__ = inference_payload.InferencePayloadTooLarge()
    local = admission_chunk(wrapped)
    remote = admission_chunk(SimpleNamespace(body={"error": {"code": "model_request_too_large", "message": secret}}))
    assert local == remote
    assert local["code"] == "model_request_too_large" and local["retryable"] is False
    assert secret not in str(local)
