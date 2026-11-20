"""Install central accounting on original user-owned provider instances."""
import asyncio
from contextlib import suppress
import os
from types import SimpleNamespace

import anyio
import httpx

from app.cloud.providers import normalize_usage
from app.cloud.gateway_errors import admission_chunk
from app.schemas.provider import StreamChunk

LOCAL_PROVIDERS = {"openai-subscription", "ollama", "rapid-mlx"}


class ProviderAdmissionError(RuntimeError):
    def __init__(self, data):
        super().__init__(data["message"])
        self.data = data


def install_tenant_provider_metering():
    from app.provider.registry import ProviderRegistry
    if getattr(ProviderRegistry, "_cloud_metering_installed", False):
        return
    original_register = ProviderRegistry.register
    gateway = os.environ["CLOUD_ENGINE_GATEWAY_URL"].rstrip("/").removesuffix("/inference/v1") + "/tenant-provider"
    capability = os.environ["CLOUD_ENGINE_CAPABILITY"]

    def register(registry, provider):
        if provider.id in LOCAL_PROVIDERS and not getattr(provider, "_cloud_metered", False):
            # The processor owns retries so that each actual SDK attempt gets its
            # own admission and receipt. An SDK-internal retry would otherwise
            # hide a possibly dispatched attempt behind the final response.
            from openai import AsyncOpenAI
            sdk_client = getattr(provider, "_client", None)
            if isinstance(sdk_client, AsyncOpenAI):
                sdk_client.max_retries = 0
            original_stream = provider.stream_chat
            async def stream_chat(model, messages, **kwargs):
                request_id, receipt, heartbeat = None, None, None
                execution = asyncio.current_task()
                async with httpx.AsyncClient(trust_env=False, timeout=30,
                    headers={"Authorization": "Bearer " + capability}) as client:
                    async def post(path, payload):
                        response = await client.post(gateway + path, json=payload)
                        if response.status_code != 200:
                            # No provider body or credential is echoed here.
                            try:
                                body = response.json()
                            except ValueError:
                                body = {}
                            problem = body.get("detail", body.get("error", body)) if isinstance(body, dict) else {}
                            rejected = admission_chunk(SimpleNamespace(body={"error": problem}))
                            if rejected is not None:
                                raise ProviderAdmissionError(rejected)
                            raise RuntimeError("云端模型计量服务不可用")
                        return response.json()
                    async def renew():
                        try:
                            while True:
                                await asyncio.sleep(15)
                                await post("/heartbeat", {"request_id": request_id})
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            execution.cancel()
                    try:
                        payload = {"provider_id": provider.id, "model": model, "messages": messages,
                            "max_tokens": kwargs.get("max_tokens") or 8192,
                            "system": kwargs.get("system"), "tools": kwargs.get("tools")}
                        request_id = (await post("/admit", payload))["request_id"]
                        heartbeat = asyncio.create_task(renew())
                        stream = original_stream(model, messages, **kwargs)
                        try:
                            async for chunk in stream:
                                if chunk.type == "usage":
                                    try:
                                        receipt = normalize_usage(provider.id, chunk.data)
                                    except (ValueError, TypeError):
                                        receipt = None
                                yield chunk
                        finally:
                            close = getattr(stream, "aclose", None)
                            if close:
                                await close()
                    except ProviderAdmissionError as exc:
                        yield StreamChunk(type="error", data=exc.data)
                    except (httpx.HTTPError, RuntimeError):
                        yield StreamChunk(type="error", data={"message": "云端模型用量校验未通过，请检查额度和连接"})
                    finally:
                        if heartbeat:
                            heartbeat.cancel()
                            with suppress(asyncio.CancelledError):
                                await heartbeat
                        if request_id:
                            # A disconnect must not erase a reported receipt.
                            async def finalize():
                                with suppress(httpx.HTTPError, RuntimeError):
                                    await post("/finish", {"request_id": request_id, "usage": receipt})
                            with anyio.CancelScope(shield=True):
                                await finalize()
            provider.stream_chat = stream_chat
            provider._cloud_metered = True
        return original_register(registry, provider)

    ProviderRegistry.register = register
    ProviderRegistry._cloud_metering_installed = True
