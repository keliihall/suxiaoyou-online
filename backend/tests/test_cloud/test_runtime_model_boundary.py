"""Runtime keeps complete tools while replacing paid inference boundaries."""
import base64
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.cloud.runtime_entry import _ManagedModelBoundary


async def test_paid_byok_and_tunnel_settings_cannot_bypass_cloud_routes():
    app = FastAPI()
    @app.api_route("/{rest:path}", methods=["GET", "POST", "DELETE", "PATCH"])
    async def echo(rest):
        return {"accepted": True}
    app.add_middleware(_ManagedModelBoundary)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://runtime") as client:
        for path in ("/api/config/api-key", "/api/config/providers/deepseek/key", "/api/config/custom", "/api/config/custom/secret", "/api/config/local", "/api/remote/start"):
            response = await client.post(path, json={"api_key": "synthetic-do-not-forward"})
            assert response.status_code == 409
            assert response.json()["detail"]["code"] == "cloud_managed_models"
            assert "synthetic-do-not-forward" not in response.text
        for path in ("/api/config/ollama", "/api/rapid-mlx/start", "/api/openai/auth-start", "/api/connectors/custom/connect"):
            assert (await client.post(path)).status_code == 200


async def test_original_image_tool_uses_gateway_and_reuses_verified_file(tmp_path, monkeypatch):
    pytest.importorskip("mcp", reason="Requires original engine runtime dependencies")
    from app.cloud import runtime_image
    from app.tool.builtin import image_generate as tool_module
    from app.session import processor
    from tests.test_tool.test_image_generate import PNG_1X1, _ctx
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("SUXIAOYOU_PRIVATE_DATA_DIR", str(tmp_path / "private"))
    # Save original surfaces so process-local installation cannot affect later
    # desktop baseline tests within this pytest process.
    monkeypatch.setattr(tool_module.ImageGenerateTool, "execute", tool_module.ImageGenerateTool.execute)
    monkeypatch.setattr(tool_module.ImageGenerateTool, "_cloud_original_execute", tool_module.ImageGenerateTool.execute, raising=False)
    for name in ("get_settings", "get_provider_registry", "SiliconFlowImageClient", "_billing_metadata"):
        monkeypatch.setattr(tool_module, name, getattr(tool_module, name))
    monkeypatch.setattr(processor, "_permission_metadata", processor._permission_metadata)
    monkeypatch.setattr(processor, "_cloud_original_permission_metadata", processor._permission_metadata, raising=False)
    sent = []
    def response(request):
        sent.append(request)
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG_1X1).decode()}]})
    original_client = httpx.AsyncClient
    monkeypatch.setattr(runtime_image.httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(response), **kwargs))
    runtime_image.install_cloud_image_tool(gateway="https://gateway.test/v1", capability="runtime-capability-only")
    tool = tool_module.ImageGenerateTool()
    context = _ctx(workspace)
    assert tool.requires_approval is True
    args = {"prompt": "cat", "output_path": "cat.png"}
    result = await tool.execute(args, context)
    assert result.success, result.error
    assert (workspace / "suxiaoyou_written/cat.png").read_bytes() == PNG_1X1
    assert len(sent) == 1 and str(sent[0].url) == "https://gateway.test/v1/images/generations"
    assert sent[0].headers["Authorization"] == "Bearer runtime-capability-only"
    data = json.loads(sent[0].content)
    assert data["operation_id"] == context.call_id and data["model"] == "Kwai-Kolors/Kolors"
    assert result.metadata["pricing_unit"] == "image" and result.metadata["estimated_cost"] is None
    assert processor._permission_metadata("image_generate", {})["approval_mode"] == "per_call"
    repeat = await tool.execute(args, context)
    assert repeat.success and len(sent) == 1
