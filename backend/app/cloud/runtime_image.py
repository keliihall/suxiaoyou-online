"""Adapt the original paid image tool to the platform gateway inside a tenant.

The original per-call approval, workspace transaction and local no-replay ledger
remain intact. The scoped capability here is never a vendor API key.
"""
import asyncio
import base64
import contextvars
import json
from types import SimpleNamespace

import httpx

_operation = contextvars.ContextVar("cloud_image_operation", default=None)


def install_cloud_image_tool(*, gateway, capability):
    from app.tool.builtin import image_generate as original
    from app.image_generation.siliconflow import (
        ImageGenerationBillingUncertain, ImageGenerationCancelled, ImageGenerationError,
        SiliconFlowImageResult, SILICONFLOW_IMAGE_MODEL, _await_or_cancel, _parse_png,
    )

    class GatewayImageClient:
        def __init__(self, _scoped_capability):
            self.client = httpx.AsyncClient(timeout=300, trust_env=False)

        async def aclose(self):
            await self.client.aclose()

        async def generate(self, *, abort_event=None, **parameters):
            operation = _operation.get()
            if not operation:
                raise ImageGenerationError("图片操作缺少运行时身份")
            try:
                response = await _await_or_cancel(self.client.post(gateway.rstrip("/") + "/images/generations",
                    headers={"Authorization": "Bearer " + capability}, json={"model": SILICONFLOW_IMAGE_MODEL,
                        "operation_id": operation, **parameters}), abort_event)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                raise ImageGenerationBillingUncertain("图片请求状态未知，请核对原请求后再确认新操作") from exc
            if response.status_code != 200:
                if response.status_code >= 500 or response.status_code == 409:
                    raise ImageGenerationBillingUncertain("图片操作未完成，请核对云端用量后再确认新请求")
                raise ImageGenerationError("图片服务不可用或额度不足，请检查云端图片模型配置")
            try:
                payload = response.json()
                content = base64.b64decode(payload["data"][0]["b64_json"], validate=True)
                width, height = _parse_png(content)
                return SiliconFlowImageResult(content=content, width=width, height=height,
                                              seed=parameters.get("seed"), trace_id=None)
            except Exception as exc:
                raise ImageGenerationBillingUncertain("图片已生成但接收失败，请核对云端用量") from exc

    execute = getattr(original.ImageGenerateTool, "_cloud_original_execute", original.ImageGenerateTool.execute)
    original.ImageGenerateTool._cloud_original_execute = execute

    async def cloud_execute(self, args, ctx):
        token = _operation.set(ctx.call_id)
        try:
            return await execute(self, args, ctx)
        finally:
            _operation.reset(token)

    def billing_metadata(_ctx=None):
        # The desktop's dated zero-price catalog is not the configured cloud
        # tariff. Keep the per-call image approval, with unknown price rather
        # than a false free estimate; central ledger records its route snapshot.
        return {"provider": "siliconflow", "provider_name": "云端图片", "model": SILICONFLOW_IMAGE_MODEL,
                "estimated_cost": None, "currency": "USD", "pricing_unit": "image", "count": 1,
                "pricing_basis": "platform_route", "cost_status": "unknown",
                "approval_mode": "per_call", "external_billing": True}

    from app.session import processor
    permission_metadata = getattr(processor, "_cloud_original_permission_metadata", processor._permission_metadata)
    processor._cloud_original_permission_metadata = permission_metadata
    def cloud_permission_metadata(tool_name, arguments):
        return {**billing_metadata(), "image_size": arguments.get("image_size", "1024x1024")} if tool_name == "image_generate" else permission_metadata(tool_name, arguments)
    processor._permission_metadata = cloud_permission_metadata
    original._billing_metadata = billing_metadata
    original.ImageGenerateTool.execute = cloud_execute
    original.SiliconFlowImageClient = GatewayImageClient
    # The fixed-provider checks in the original tool now check the gateway
    # capability; the platform itself decides whether an image route exists.
    original.get_provider_registry = lambda: SimpleNamespace(get_provider=lambda _name: object())
    original.get_settings = lambda: SimpleNamespace(siliconflow_api_key=capability)
