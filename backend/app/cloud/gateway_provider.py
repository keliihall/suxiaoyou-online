"""Tenant-only catalog/stream adapter preserving opaque route capabilities."""
from app.provider.generic_openai import GenericOpenAIProvider
from app.schemas.provider import ModelCapabilities, ModelInfo, ModelPricing, StreamChunk, safe_provider_error_data
from app.cloud.inference_payload import check_sdk_inference_payload


class GatewayProvider(GenericOpenAIProvider):
    def __init__(self, *, api_key, provider_id, base_url, kind="openai_compat_custom", default_headers=None, models_override=None):
        import httpx
        from openai import AsyncOpenAI
        # The internal gateway must not inherit a host proxy, extra transport
        # credentials or SDK retries. Public vendor HTTP stays in the platform.
        self._api_key, self._provider_id, self._kind = api_key, provider_id, kind
        self._models_cache, self._models_override = None, []
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0,
            default_headers={"User-Agent": "Suxiaoyou-Cloud/1.0"}, timeout=httpx.Timeout(300, connect=30),
            http_client=httpx.AsyncClient(trust_env=False,
                event_hooks={"request": [check_sdk_inference_payload]}))

    async def _fetch_api_models(self):
        response = await self._client.models.list()
        models = []
        for item in response.data:
            raw = item.model_dump()
            models.append(ModelInfo(id=item.id, name=raw.get("name") or item.id, provider_id=self.id,
                capabilities=ModelCapabilities(**raw.get("capabilities", {})),
                pricing=ModelPricing(**raw.get("pricing", {})), metadata=raw.get("metadata", {})))
        return models

    async def stream_chat(self, model, messages, **kwargs):
        info = next((row for row in (self._models_cache or []) if row.id == model), None)
        metadata = info.metadata if info else {}
        provider_id, native_model = metadata.get("upstream_provider"), metadata.get("upstream_model")
        # Reasoning history belongs to the upstream transport, not the opaque
        # platform alias. Never send DeepSeek fields to a different provider.
        if provider_id in {"openrouter", "anthropic", "google", "openai", "openai-subscription", "azure"} or native_model == "deepseek-reasoner":
            messages = [{key: value for key, value in row.items() if key != "reasoning_content"} for row in messages]
        if provider_id == "deepseek" and info and info.capabilities.reasoning:
            extra = dict(kwargs.get("extra_body") or {})
            # Capability is not a request to think. Background callers that
            # omit the switch must retain the gateway's non-thinking default.
            enabled = (extra.get("reasoning") or {}).get("enabled") is True
            extra.setdefault("thinking", {"type": "enabled" if enabled else "disabled"})
            if isinstance(extra["thinking"], dict) and extra["thinking"].get("type") == "enabled":
                # Leave room for a final answer within the per-step budget;
                # an explicit caller effort remains authoritative.
                extra.setdefault("reasoning_effort", "low")
            extra.pop("reasoning", None)
            kwargs["extra_body"] = extra
        elif provider_id and provider_id != "deepseek":
            extra = dict(kwargs.get("extra_body") or {})
            if extra.get("reasoning") == {"enabled": True}:
                extra.pop("reasoning")
                kwargs["extra_body"] = extra or None
        # Refresh clears this provider's cache before fetching its next model
        # list, while the registry can retain last-known-good metadata. If the
        # cache is temporarily absent, keep explicit reasoning intent so the
        # platform can interpret it using the authoritative route provider.
        finished, received_usage, failed = False, False, False
        async for chunk in super().stream_chat(model, messages, **kwargs):
            finished = finished or chunk.type == "finish"
            received_usage = received_usage or chunk.type == "usage"
            failed = failed or chunk.type == "error"
            yield chunk
        # HTTP 200 followed by a clean EOF is not a successful empty answer.
        # The upstream attempt may already have spent tokens or executed a
        # tool. Prevent the engine's empty-answer retry from replaying it.
        if not failed and not (finished and received_usage):
            yield StreamChunk(type="error", data=safe_provider_error_data(
                "model_response_incomplete" if received_usage else "model_usage_unconfirmed"))


def install_gateway_provider():
    import os
    from app.provider import factory
    if getattr(factory, "_cloud_gateway_installed", False):
        return
    original = factory.create_provider
    def create(provider_id, api_key, *, base_url=None, models_override=None, extra_headers=None):
        if provider_id == "custom_cloud":
            # Startup credential migration turns custom API keys into opaque
            # desktop vault refs. This is a supervisor-supplied capability, so
            # use the trusted process identity directly and avoid a deferred
            # provider with an empty, never-activated custom model catalog.
            return GatewayProvider(api_key=os.environ["CLOUD_ENGINE_CAPABILITY"], provider_id=provider_id,
                base_url=os.environ["CLOUD_ENGINE_GATEWAY_URL"], kind="openai_compat_custom", models_override=None)
        return original(provider_id, api_key, base_url=base_url, models_override=models_override, extra_headers=extra_headers)
    factory.create_provider = create
    factory._cloud_gateway_installed = True
