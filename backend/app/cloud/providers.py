"""Cloud-only usage correction and explicit single-attempt providers.

Desktop adapters remain untouched. An input estimate is useful for admission,
but is deliberately not labelled a bound on real provider tokenization/billing.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
from typing import Any, AsyncIterator, Mapping

from app.schemas.provider import SAFE_PROVIDER_ERRORS, StreamChunk, safe_provider_error_data


class ProviderConfigurationError(ValueError):
    pass


class ProviderRequestError(ValueError):
    """A safe summary: vendor exception text may include request credentials."""

    def __init__(self, message: str, status_code: int | None = None, *, code: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.upstream_status = status_code
        self.code = code if isinstance(code, str) and code in SAFE_PROVIDER_ERRORS else _provider_error_code(self, status_code)
        # A failed dispatched request may already be billable. Recovery of a
        # known context rejection uses compaction, never ordinary HTTP retry.
        self.retryable = False


def _provider_status(error: Exception) -> int | None:
    status = getattr(error, "status_code", None)
    if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
        return None
    return status


def _provider_error_code(error: Exception, status: int | None = None) -> str:
    """Classify in memory before sanitization; no vendor text is forwarded."""
    if isinstance(error, ProviderRequestError):
        existing = getattr(error, "code", None)
        if isinstance(existing, str) and existing in SAFE_PROVIDER_ERRORS:
            return existing
    status = _provider_status(error) if status is None else status
    if status == 400:
        body = getattr(error, "body", None)
        body = body.get("error", body) if isinstance(body, dict) else None
        if isinstance(body, dict):
            if isinstance(body.get("code"), str) and body["code"] in {"context_length_exceeded", "context_window_exceeded", "input_too_long"}:
                return "model_context_too_long"
            detail = body.get("message")
        else:
            detail = None
        # Some compatible SDKs expose only their safe-to-inspect exception
        # string. Do not treat max_tokens/unsupported parameters as overflow.
        detail = detail if isinstance(detail, str) else str(error)
        if re.search(r"\b(?:context_length_exceeded|maximum context length|context length exceeded|input (?:is )?too long)\b",
                     detail[:8192], re.I):
            return "model_context_too_long"
        return "model_request_rejected"
    if status in {401, 403}:
        return "model_authentication_failed"
    if status == 402:
        return "model_payment_required"
    if status == 429:
        return "model_rate_limited"
    if status in {413, 422}:
        return "model_request_rejected"
    import httpx
    from openai import APIConnectionError, APITimeoutError
    if isinstance(error, (TimeoutError, httpx.TimeoutException, APITimeoutError)):
        return "model_timeout"
    if isinstance(error, (ConnectionError, httpx.TransportError, APIConnectionError)):
        return "model_connection_interrupted"
    # Anthropic may use httpx2, while the OpenAI transport uses httpx.
    if type(error).__module__.split(".")[0] in {"httpx2", "httpcore"}:
        if "Timeout" in type(error).__name__:
            return "model_timeout"
        if type(error).__name__ in {"ReadError", "WriteError", "ConnectError", "RemoteProtocolError", "LocalProtocolError"}:
            return "model_connection_interrupted"
    return "model_service_error"


def _safe_provider_exception(error: Exception) -> ProviderRequestError:
    return ProviderRequestError(_safe_provider_message(error), _provider_status(error),
                                code=_provider_error_code(error))


def _safe_provider_message(error: Exception) -> str:
    status = _provider_status(error)
    if status is None:
        return "模型服务调用失败，请检查模型配置或网络"
    messages = {
        400: "模型服务不接受本次请求，请检查模型配置",
        401: "模型服务身份验证失败，请管理员更新 API Key",
        402: "模型服务余额不足，请充值后重试",
        403: "模型服务拒绝访问，请检查账号或模型权限",
        429: "模型服务请求受限，请稍后重试",
    }
    return f"{messages.get(status, '模型服务调用失败')}（HTTP {status}）"


class _SafeSDKStream:
    def __init__(self, target: Any):
        self.target, self.closed = target, False

    async def __aiter__(self):
        try:
            async for item in self.target:
                yield item
        except Exception as exc:
            raise _safe_provider_exception(exc) from None
        finally:
            await self.aclose()

    async def aclose(self):
        if self.closed:
            return
        self.closed = True
        close = getattr(self.target, "aclose", None) or getattr(self.target, "close", None)
        if callable(close):
            try:
                result = close()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                raise _safe_provider_exception(exc) from None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        await self.aclose()


class _SafeSDKNode:
    """Sanitize before legacy adapters log SDK errors, without changing desktop.

    Only the resource chains used by cloud streams are wrapped. All ordinary
    SDK configuration/close attributes retain their existing behavior.
    """
    def __init__(self, target: Any, *, omit_authorization: bool = False):
        self.target, self.omit_authorization = target, omit_authorization

    def __getattr__(self, name: str):
        value = getattr(self.target, name)
        if name in {"chat", "completions", "messages", "aio", "models"}:
            return _SafeSDKNode(value, omit_authorization=self.omit_authorization)
        if name == "with_options":
            def options(*args, **kwargs):
                try:
                    return _SafeSDKNode(value(*args, **kwargs), omit_authorization=self.omit_authorization)
                except Exception as exc:
                    raise ProviderConfigurationError("模型服务配置无效，请管理员检查") from None
            return options
        if name in {"create", "list", "generate_content_stream"}:
            async def invoke(*args, **kwargs):
                try:
                    if self.omit_authorization:
                        # The SDK requires omission on the request itself;
                        # a default_headers Omit alone fails its auth check.
                        from openai import Omit
                        kwargs["extra_headers"] = {**(kwargs.get("extra_headers") or {}), "Authorization": Omit()}
                    result = value(*args, **kwargs)
                    if inspect.isawaitable(result):
                        result = await result
                except Exception as exc:
                    raise _safe_provider_exception(exc) from None
                return _SafeSDKStream(result) if hasattr(result, "__aiter__") else result
            return invoke
        return value


def _tokens(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Invalid provider token field: {name}")
    return value


def normalize_usage(provider_id: str, usage: Mapping[str, Any]) -> dict[str, Any]:
    """Convert the existing adapter's usage into non-overlapping totals.

    Google prompt includes cache reads; Anthropic prompt excludes cache creation.
    OpenAI-compatible adapters have already subtracted reasoning and cache reads.
    Complete usage snapshots are receipts, never increments to be summed.
    """
    if not isinstance(usage, Mapping) or not usage:
        raise ValueError("Provider did not supply usage")
    if "input_total" in usage and "output_total" in usage:
        result = dict(usage)
        result.setdefault("raw_usage", dict(usage))
        return result
    get = lambda key: _tokens(usage.get(key, 0), key)
    ordinary, output = get("input"), get("output")
    reasoning, read, write = get("reasoning"), get("cache_read"), get("cache_write")
    if provider_id in {"google", "gemini"}:
        if read > ordinary:
            raise ValueError("Google cache read exceeds total prompt")
        input_total, output_total = ordinary, output + reasoning
        uncached = ordinary - read
    elif provider_id == "anthropic":
        input_total, output_total = ordinary + read + write, output + reasoning
        uncached = ordinary
    elif provider_id in {"openai-subscription", "openai_subscription"}:
        input_total, output_total = ordinary, output
        uncached = ordinary - read - write
    else:
        # The compatible adapter input excludes cache hits, but may include
        # writes. Cache-write is a diagnostic subset, not an extra prompt.
        input_total, output_total = ordinary + read, output + reasoning
        uncached = ordinary - write
    if min(uncached, output_total - reasoning) < 0:
        raise ValueError("Provider usage has overlapping or inconsistent token categories")
    total = input_total + output_total
    provider_total = get("total") if "total" in usage else None
    # Anthropic's desktop adapter synthesizes an incomplete total (omitting
    # cache creation); it is retained for diagnostics, not treated as authority.
    status = "complete"
    if provider_id != "anthropic" and provider_total is not None and provider_total != total:
        status = "partial"
    return {
        "input_total": input_total, "input_uncached": uncached,
        "output_total": output_total, "output_visible": output_total - reasoning,
        "cache_read": read, "cache_write": write, "reasoning": reasoning,
        "total_tokens": total, "provider_total": provider_total,
        "usage_status": status, "normalization_version": 1,
        "raw_usage": dict(usage),
    }


def input_reservation(
    messages: list[dict[str, Any]], *, system: Any = None,
    tools: list[dict[str, Any]] | None = None, max_output_tokens: int = 1024,
) -> int:
    """Conservative text bytes plus a bounded, explicitly estimated image budget.

    Compute after final system/history/tools assembly and freeze those inputs.
    Only verified static inline image_url blocks receive image accounting.
    Unknown, malformed, animated or expensive-to-inspect images keep their
    original UTF-8 byte charge; these inspection bounds are not request limits.
    Every other field retains its UTF-8 byte charge, including remote URLs.
    Image estimates are not vendor token counts or a hard upper bound. Actual
    provider receipts remain authoritative, and real routes use soft quota.
    """
    if isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int) or max_output_tokens < 1:
        raise ValueError("max_output_tokens must be positive")

    import base64
    from io import BytesIO
    import warnings

    # Match the runtime Read tool's 20 MiB original-image allowance. The whole
    # JSON request remains subject to the gateway's separate wire-byte limit.
    # If any inspection budget is exceeded, retain the original byte estimate.
    max_image_bytes, max_request_image_bytes = 20 * 1024 * 1024, 24 * 1024 * 1024
    max_encoded_bytes = 4 * ((max_image_bytes + 2) // 3)
    image_budget, image_count, decoded_bytes, decoded_pixels = 0, 0, 0, 0
    estimated_messages = []
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            estimated_messages.append(message)
            continue
        estimated_content = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "image_url":
                estimated_content.append(block)
                continue
            image_count += 1
            if image_count > 32:
                estimated_content.append(block)
                continue
            image = block.get("image_url")
            if (not isinstance(image, dict) or not isinstance(image.get("url"), str)
                    or image.get("detail", "auto") not in ("auto", "low", "high")):
                estimated_content.append(block)
                continue
            url = image["url"]
            if url.lower().startswith("data:"):
                try:
                    from PIL import Image
                    header, separator, encoded = url.partition(",")
                    mime = re.fullmatch(r"data:(image/(?:png|jpe?g|webp|gif));base64", header, re.I)
                    if not separator or mime is None or not encoded or len(encoded) > max_encoded_bytes:
                        raise ValueError()
                    raw = base64.b64decode(encoded, validate=True)
                    decoded_bytes += len(raw)
                    if not raw or len(raw) > max_image_bytes or decoded_bytes > max_request_image_bytes:
                        raise ValueError()
                    formats = {"image/png": "PNG", "image/jpeg": "JPEG", "image/jpg": "JPEG",
                               "image/webp": "WEBP", "image/gif": "GIF"}
                    # Do not decode a decompression bomb or silently count an
                    # animated raster's many frames as one image.
                    with warnings.catch_warnings():
                        warnings.simplefilter("error", Image.DecompressionBombWarning)
                        with Image.open(BytesIO(raw)) as picture:
                            width, height = picture.size
                            decoded_pixels += width * height
                            if (picture.format != formats[mime[1].lower()] or getattr(picture, "is_animated", False)
                                    or width < 1 or height < 1 or max(width, height) > 16384
                                    or width * height > 16_777_216 or decoded_pixels > 33_554_432):
                                raise ValueError()
                            picture.verify()
                        # verify() alone does not fully decode JPEG pixels;
                        # loading within the bounds rejects truncated payloads.
                        with Image.open(BytesIO(raw)) as picture:
                            picture.load()
                except Exception:
                    # Unknown/malformed data cannot obtain a smaller budget.
                    # Do not log the URI, decoder error or image metadata.
                    estimated_content.append(block)
                    continue
                # Reserve a base allowance plus 512px tiles, capped at an 8x8
                # grid. This is a platform heuristic, independent of detail
                # hints, compression ratio, metadata, or the provider tokenizer.
                columns, rows = (min(width, 4096) + 511) // 512, (min(height, 4096) + 511) // 512
                image_budget += 2048 + 1024 * columns * rows
                estimated_content.append({**block, "image_url": {**image, "url": "[validated inline image]"}})
            else:
                # Never fetch remote URLs or trust caller-supplied dimensions.
                estimated_content.append(block)
        estimated_messages.append({**message, "content": estimated_content})
    payload = json.dumps({"messages": estimated_messages, "system": system, "tools": tools},
                         ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return len(payload.encode("utf-8")) + image_budget + 256 + max_output_tokens


class CloudProvider:
    """One adapter call, no retry loop, and a closed stream on cancellation."""
    def __init__(self, delegate: Any, provider_id: str):
        self.delegate, self.id = delegate, provider_id

    async def stream_chat(self, model: str, messages: list[dict[str, Any]], **kwargs: Any) -> AsyncIterator[StreamChunk]:
        stream = self.delegate.stream_chat(model, messages, **kwargs)
        try:
            async for chunk in stream:
                if chunk.type == "usage":
                    yield StreamChunk(type="usage", data=normalize_usage(self.id, chunk.data))
                elif chunk.type == "error":
                    # The guarded SDK transports produce this fixed HTTP
                    # marker. Forward only a safe status summary, even if a
                    # delegate adds unrelated diagnostic text to its error.
                    match = re.search(r"HTTP ([1-5][0-9]{2})", str(chunk.data.get("message", "")))
                    status = chunk.data.get("upstream_status", chunk.data.get("status_code"))
                    if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
                        status = int(match.group(1)) if match else None
                    safe_error = ProviderRequestError("", status_code=status,
                        code=chunk.data.get("code"))
                    yield StreamChunk(type="error", data={
                        "message": _safe_provider_message(safe_error), "status_code": status,
                        **{key: value for key, value in safe_provider_error_data(safe_error.code,
                            upstream_status=status).items() if key != "message"},
                    })
                else:
                    yield chunk
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                await close()

    async def aclose(self) -> None:
        client = getattr(self.delegate, "_client", None)
        if client is None:
            return
        # Google's public async client owns the async connection pool.
        aio = getattr(client, "aio", None)
        if aio is not None and callable(getattr(aio, "aclose", None)):
            await aio.aclose()
        close = getattr(client, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result


def normalize_deepseek_usage(usage: Mapping[str, Any]) -> dict[str, Any]:
    """Keep DeepSeek's native cache fields and full output totals intact.

    https://api-docs.deepseek.com/api/create-chat-completion/
    Prompt includes cache hits; completion includes reasoning. These are
    diagnostic subsets, never extra billable tokens added on top of totals.
    """
    prompt = _tokens(usage.get("prompt_tokens"), "prompt_tokens")
    completion = _tokens(usage.get("completion_tokens"), "completion_tokens")
    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    read = _tokens(usage.get("prompt_cache_hit_tokens", prompt_details.get("cached_tokens", 0)), "prompt_cache_hit_tokens")
    reasoning = _tokens(completion_details.get("reasoning_tokens", 0), "reasoning_tokens")
    if read > prompt or reasoning > completion:
        raise ValueError("DeepSeek usage categories exceed reported totals")
    normalized = normalize_usage("deepseek", {
        "input": prompt - read, "output": completion - reasoning, "reasoning": reasoning,
        "cache_read": read, "cache_write": 0,
        "total": _tokens(usage.get("total_tokens"), "total_tokens"),
    })
    if "prompt_cache_miss_tokens" in usage:
        misses = _tokens(usage["prompt_cache_miss_tokens"], "prompt_cache_miss_tokens")
        if misses + read != prompt:
            normalized["usage_status"] = "partial"
    if "cached_tokens" in prompt_details and _tokens(prompt_details["cached_tokens"], "cached_tokens") != read:
        normalized["usage_status"] = "partial"
    normalized["raw_usage"] = dict(usage)
    return normalized


class DeepSeekCloudProvider(CloudProvider):
    """Cloud Chat Completions with exact native receipts and function tools.

    The desktop compatible adapter discards native cache fields. Reading SDK
    chunks here preserves receipts and complete function calls. Functions are
    executed by the isolated tenant runtime, never inside the gateway.
    """

    async def stream_chat(self, model: str, messages: list[dict[str, Any]], **kwargs: Any) -> AsyncIterator[StreamChunk]:
        params: dict[str, Any] = {
            "model": model, "messages": self.delegate._build_messages(kwargs.get("system"), messages),
            "stream": True, "stream_options": {"include_usage": True},
            # DeepSeek defaults to high-effort thinking. The cloud UI consumes
            # final text and has a bounded output allowance, so opt out unless
            # a trusted caller explicitly requests thinking via extra_body.
            "extra_body": {"thinking": {"type": "disabled"}, **(kwargs.get("extra_body") or {})},
        }
        for key in ("temperature", "max_tokens", "response_format", "tools"):
            if kwargs.get(key) is not None:
                params[key] = kwargs[key]
        has_text, finish_reason = False, None
        tool_calls: dict[int, dict[str, str]] = {}
        stream = await self.delegate._client.chat.completions.create(**params)
        async with stream:
            async for chunk in stream:
                choice = chunk.choices[0] if chunk.choices else None
                delta = choice.delta if choice else None
                if delta is not None:
                    text = delta.content
                    if text:
                        has_text = has_text or bool(text.strip())
                        yield StreamChunk(type="text-delta", data={"text": text})
                    reasoning = getattr(delta, "reasoning_content", None)
                    if reasoning:
                        yield StreamChunk(type="reasoning-delta", data={"text": reasoning})
                    for call in getattr(delta, "tool_calls", None) or []:
                        acc = tool_calls.setdefault(call.index, {"id": "", "name": "", "arguments": ""})
                        if call.id:
                            acc["id"] = call.id
                        if call.function:
                            if call.function.name:
                                acc["name"] += call.function.name
                            if call.function.arguments:
                                acc["arguments"] += call.function.arguments
                # Receipt must reach the worker before a terminal error: even
                # an empty/refused/truncated answer can incur a real charge.
                if chunk.usage is not None:
                    raw = chunk.usage.model_dump(mode="json", exclude_none=True)
                    yield StreamChunk(type="usage", data=normalize_deepseek_usage(raw))
                if choice and choice.finish_reason:
                    finish_reason = choice.finish_reason
        if finish_reason not in {"stop", "length", "tool_calls"}:
            yield StreamChunk(type="error", data=safe_provider_error_data("model_response_incomplete"))
        elif finish_reason == "tool_calls" and not tool_calls:
            yield StreamChunk(type="error", data=safe_provider_error_data("model_response_incomplete"))
        elif tool_calls:
            for index, call in sorted(tool_calls.items()):
                try:
                    arguments = json.loads(call["arguments"] or "{}")
                except (ValueError, TypeError):
                    arguments = {"_raw": call["arguments"]}
                yield StreamChunk(type="tool-call", data={
                    "id": call["id"], "name": call["name"], "arguments": arguments,
                    "arguments_json": call["arguments"], "index": index,
                })
            yield StreamChunk(type="finish", data={"reason": finish_reason})
        elif not has_text:
            yield StreamChunk(type="error", data=safe_provider_error_data(
                "model_output_exhausted" if finish_reason == "length" else "model_response_incomplete"))
        else:
            yield StreamChunk(type="finish", data={"reason": finish_reason})


class MockProvider:
    """Deterministic, network-free provider; explicitly development/test only."""
    id = "mock"

    def __init__(self, delay_seconds: float = 0.01, chunk_size: int = 12):
        if delay_seconds < 0 or chunk_size < 1:
            raise ValueError("Invalid mock stream settings")
        self.delay_seconds, self.chunk_size = delay_seconds, chunk_size

    async def stream_chat(self, model: str, messages: list[dict[str, Any]], **kwargs: Any) -> AsyncIterator[StreamChunk]:
        maximum = kwargs.get("max_tokens") or 1024
        prompt = " ".join(str(m.get("content", "")) for m in messages if m.get("role") == "user")
        answer = ("Mock response: " + prompt).encode("utf-8")[:maximum].decode("utf-8", errors="ignore")
        for offset in range(0, len(answer), self.chunk_size):
            await asyncio.sleep(self.delay_seconds)
            yield StreamChunk(type="text-delta", data={"text": answer[offset:offset + self.chunk_size]})
        # Mock defines its own exact token unit: UTF-8 bytes. Its input stays
        # below input_reservation; output is limited by UTF-8 bytes as well.
        output_bytes = len(answer.encode("utf-8"))
        payload = json.dumps({"messages": messages, "system": kwargs.get("system"),
                              "tools": kwargs.get("tools")}, ensure_ascii=False,
                             sort_keys=True, separators=(",", ":"))
        raw = {"input": len(payload.encode("utf-8")), "output": output_bytes,
               "reasoning": 0, "cache_read": 0, "cache_write": 0}
        raw["total"] = raw["input"] + raw["output"]
        yield StreamChunk(type="usage", data=normalize_usage("mock", raw))
        yield StreamChunk(type="finish", data={"reason": "stop"})

    async def aclose(self) -> None:
        pass


def build_provider(route: Any, settings: Any) -> CloudProvider | MockProvider:
    try:
        return _build_provider(route, settings)
    except ProviderConfigurationError:
        raise
    except Exception:
        # SDK constructors can echo malformed credentials in validation errors.
        raise ProviderConfigurationError("模型服务配置无效，请管理员检查") from None


def _build_provider(route: Any, settings: Any) -> CloudProvider | MockProvider:
    if not route.enabled:
        raise ProviderConfigurationError("Model route is disabled")
    if route.provider == "mock":
        if not settings.allow_mock_provider:
            raise ProviderConfigurationError("Mock provider is not enabled")
        return MockProvider(delay_seconds=getattr(settings, "mock_delay_seconds", 0.01))
    if route.strict_quota:
        raise ProviderConfigurationError("Real providers require a verified input-bound contract; use soft quota")
    api_key = route_api_key(route, settings)
    headers = route_custom_headers(route, settings)
    from app.provider.factory import create_provider
    # The desktop release catalog deliberately omits OpenAI's direct API. Its
    # existing custom compatible adapter still supplies the same transport.
    factory_id = "custom_cloud_openai" if route.provider == "openai" else route.provider
    base_url = route.base_url or ("https://api.openai.com/v1" if route.provider == "openai" else None)
    factory_options = {"base_url": base_url}
    if headers:
        factory_options["extra_headers"] = headers
    provider = create_provider(factory_id, api_key, **factory_options)
    client = getattr(provider, "_client", None)
    if route.provider == "google":
        # Factory still owns adapter selection. Replace only its SDK transport
        # using public constructor options: attempts includes the first request.
        from google import genai
        from google.genai import types
        replacement = genai.Client(api_key=api_key, http_options=types.HttpOptions(
            retry_options=types.HttpRetryOptions(attempts=1)))
        client.close()
        provider._client = replacement
    elif callable(getattr(client, "with_options", None)):
        options = {"max_retries": 0}
        if not api_key:
            # The legacy compatible adapter supplies a placeholder key when
            # empty. Header-only/public routes must not send that fake bearer.
            from openai import Omit
            options["api_key"] = ""
            options["default_headers"] = {"Authorization": Omit(), **headers}
        provider._client = client.with_options(**options)
    else:
        raise ProviderConfigurationError("Provider transport cannot disable implicit retries")
    provider._cloud_gateway_managed = True
    provider._client = _SafeSDKNode(provider._client,
        omit_authorization=not api_key and "Authorization" not in headers)
    wrapper = DeepSeekCloudProvider if route.provider == "deepseek" else CloudProvider
    return wrapper(provider, route.provider)


def route_api_key(route: Any, settings: Any) -> str:
    """Resolve a platform-owned credential; callers must never return it."""
    encrypted = getattr(route, "api_key_encrypted", None)
    if encrypted is not None:
        try:
            from app.cloud.secrets import ModelSecretVault
            api_key = ModelSecretVault(settings).unseal(encrypted)
            if not isinstance(api_key, str) or not api_key.strip():
                raise ValueError("Empty managed credential")
        except Exception:
            # Never fall back to another account after failed rotation/decrypt.
            raise ProviderConfigurationError("托管模型凭据无法读取，请管理员重新配置 API Key") from None
    else:
        api_key_env = getattr(route, "api_key_env", "") or ""
        if not api_key_env:
            from app.cloud.custom_headers import supports_custom_headers
            if supports_custom_headers(route.provider, getattr(route, "kind", "chat")) and (
                getattr(route, "custom_headers_encrypted", None)
                or (route.provider.startswith("custom_") and getattr(route, "allow_unauthenticated", False))):
                return ""
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", api_key_env):
            raise ProviderConfigurationError("Model route must reference a valid secret environment variable")
        api_key = os.environ.get(api_key_env, "")
        if not api_key:
            raise ProviderConfigurationError("Model route secret is unavailable")
    if api_key.startswith("suxiaoyou-credential://"):
        raise ProviderConfigurationError("云端模型凭据必须为本路由的直接认证值")
    return api_key


def route_custom_headers(route: Any, settings: Any) -> dict[str, str]:
    encrypted = getattr(route, "custom_headers_encrypted", None)
    if encrypted is None:
        return {}
    try:
        from app.cloud.custom_headers import decode_headers, supports_custom_headers
        from app.cloud.secrets import ModelSecretVault
        if not supports_custom_headers(route.provider, getattr(route, "kind", "chat")):
            raise ValueError("Unsupported header transport")
        return decode_headers(ModelSecretVault(settings).unseal(encrypted))
    except Exception:
        # Failed decrypt/rotation must never silently drop auth or resolve a
        # different user's credential through a desktop vault reference.
        raise ProviderConfigurationError("托管自定义请求头无法读取，请重新配置") from None
