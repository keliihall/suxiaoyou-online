"""Network-free model metadata defaults with explicit per-route overrides."""
from app.schemas.provider import ModelCapabilities


def validated_overrides(value):
    allowed = set(ModelCapabilities.model_fields)
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("Unknown model capability field")
    for key, field in value.items():
        if key in {"max_context", "max_output"}:
            if field is None and key == "max_output":
                continue
            if isinstance(field, bool) or not isinstance(field, int) or not 1 <= field <= 2_000_000:
                raise ValueError("Invalid model token limit")
        elif not isinstance(field, bool):
            raise ValueError("Model capability flags must be boolean")
    return value


def public_model(route):
    from app.provider.generic_openai import _infer_context_from_name
    from app.provider.vision_allowlist import model_supports_vision
    caps = ModelCapabilities(function_calling=True, vision=model_supports_vision(route.model),
        reasoning="reasoner" in route.model.lower() or "thinking" in route.model.lower(),
        max_context=_infer_context_from_name(route.model), max_output=8192).model_dump()
    try:
        from app.provider.models_dev import models_dev
        cached = next((row for row in models_dev.get_models_cached(route.provider) if row["id"] == route.model), None)
        if cached:
            caps.update(cached.get("capabilities", {}))
    except (ImportError, ValueError, KeyError):
        pass
    try:
        from yakagent.provider.catalog import PROVIDERS
        provider = PROVIDERS.get(route.provider)
        known = provider.models.get(route.model) if provider else None
        if known:
            caps.update(vision=bool(known.get("vision", caps["vision"])), reasoning=bool(known.get("reasoning", caps["reasoning"])),
                        max_context=known.get("context", caps["max_context"]), max_output=known.get("output", caps["max_output"]))
    except ImportError:
        pass
    # Current official DeepSeek model details, checked 2026-11-15:
    # https://api-docs.deepseek.com/quick_start/pricing/
    if route.provider == "deepseek" and route.model in {"deepseek-flash", "deepseek-v4-pro", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp"}:
        caps.update(vision=route.model != "deepseek-v4-pro", reasoning=True, function_calling=True,
                    json_output=True, prompt_caching=True, max_context=1_000_000, max_output=131072)
    caps.update(validated_overrides(route.capabilities or {}))
    caps["max_output"] = min(caps.get("max_output") or 8192, 131072)
    return {"id": route.id, "object": "model", "created": 0, "owned_by": "suxiaoyou", "name": route.name,
        "capabilities": ModelCapabilities(**caps).model_dump(),
        "pricing": {"prompt": route.input_price_microusd_per_million / 1_000_000,
                    "completion": route.output_price_microusd_per_million / 1_000_000},
        "metadata": {"upstream_provider": route.provider, "upstream_model": route.model, "managed": True}}
