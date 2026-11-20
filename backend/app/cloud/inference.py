"""Tenant-runtime-only OpenAI inference gateway with central per-attempt metering.

It executes no tools. Function schemas/results pass between the original Agent
runtime and the selected server-owned model route. Vendor keys never enter the
runtime. Each SDK attempt is a separate durable request, including background
memory, compaction, delegated Agents and Goal continuations.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from datetime import timedelta
from typing import Annotated, Any, Literal

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from app.cloud import metering
from app.cloud import inference_payload
from app.cloud.database import get_db
from app.cloud.models import ModelRoute, RequestRecord, User, new_id, utcnow
from app.cloud.providers import (
    ProviderConfigurationError, ProviderRequestError, _provider_error_code,
    build_provider, input_reservation,
)
from app.schemas.provider import safe_provider_error_data, safe_provider_error_metadata

router = APIRouter(prefix="/api/cloud/internal/inference/v1", tags=["runtime-inference"])
Db = Annotated[AsyncSession, Depends(get_db)]
_API_OWNER = new_id()
_LEASE_SECONDS = 60
logger = logging.getLogger(__name__)


async def runtime_principal(request: Request, db: Db) -> User:
    # Import lazily so the gateway does not own/duplicate runtime identity.
    from app.cloud.runtime_identity import get_runtime_principal
    return await get_runtime_principal(request, db)


Principal = Annotated[User, Depends(runtime_principal)]


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model: str = Field(min_length=1, max_length=120)
    messages: list[dict[str, Any]] = Field(min_length=1, max_length=2048)
    stream: bool = False
    stream_options: dict[str, Any] | None = None
    max_tokens: int | None = Field(default=None, ge=1, le=131072)
    max_completion_tokens: int | None = Field(default=None, ge=1, le=131072)
    tools: list[dict[str, Any]] | None = Field(default=None, max_length=256)
    tool_choice: str | dict[str, Any] | None = None
    parallel_tool_calls: bool | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    response_format: dict[str, Any] | None = None
    reasoning_effort: str | None = None
    reasoning: dict[str, Any] | None = None
    thinking: dict[str, Any] | None = None
    stop: str | list[str] | None = None
    top_p: float | None = Field(default=None, gt=0, le=1)
    n: Literal[1] = 1
    system: str | list[dict[str, Any]] | None = None

    @model_validator(mode="after")
    def validate_shapes(self):
        if self.max_tokens is not None and self.max_completion_tokens is not None:
            raise ValueError("Set one output token limit")
        if self.stream_options and set(self.stream_options) - {"include_usage"}:
            raise ValueError("Unsupported streaming option")
        if self.stream_options and not isinstance(self.stream_options.get("include_usage", True), bool):
            raise ValueError("include_usage must be a boolean")
        for message in self.messages:
            if message.get("role") not in {"system", "developer", "user", "assistant", "tool"}:
                raise ValueError("Unsupported message role")
            content = message.get("content")
            if content is not None and not isinstance(content, (str, list)):
                raise ValueError("Invalid message content")
            if message.get("role") == "tool" and not isinstance(message.get("tool_call_id"), str):
                raise ValueError("Tool results require tool_call_id")
            if content is None and not message.get("tool_calls"):
                raise ValueError("Message content or function calls required")
        names = set()
        for tool in self.tools or []:
            function = tool.get("function")
            if tool.get("type") != "function" or not isinstance(function, dict):
                raise ValueError("Only runtime-executed function tools are supported")
            name = function.get("name")
            if not isinstance(name, str) or not name or len(name) > 128 or name in names:
                raise ValueError("Function names must be nonempty and unique")
            names.add(name)
        if isinstance(self.tool_choice, str) and self.tool_choice not in {"auto", "none", "required"}:
            raise ValueError("Invalid tool choice")
        if isinstance(self.tool_choice, dict):
            chosen = self.tool_choice.get("function")
            if self.tool_choice.get("type") != "function" or not isinstance(chosen, dict) or chosen.get("name") not in names:
                raise ValueError("Tool choice must reference a declared function")
        if self.tool_choice not in (None, "none") and not self.tools:
            raise ValueError("Tool choice requires function tools")
        return self

    @property
    def output_limit(self):
        return self.max_tokens or self.max_completion_tokens or 8192

    def provider_kwargs(self, provider_id: str):
        extra = {}
        for name in ("tool_choice", "parallel_tool_calls", "thinking", "reasoning_effort", "stop", "top_p"):
            if (value := getattr(self, name)) is not None:
                extra[name] = value
        if self.reasoning is not None:
            if provider_id == "deepseek":
                enabled = self.reasoning.get("enabled")
                if self.thinking is None and isinstance(enabled, bool):
                    extra["thinking"] = {"type": "enabled" if enabled else "disabled"}
            elif self.reasoning != {"enabled": True}:
                # Match the known-provider tenant adapter when its cache was
                # unavailable: a generic enable hint is not a vendor option.
                extra["reasoning"] = self.reasoning
        if provider_id == "deepseek" and (extra.get("thinking") or {}).get("type") == "enabled":
            # A tenant model-catalog refresh may lose its local metadata.
            # Resolve intent here against the actual server-owned route, and
            # preserve an explicitly supplied thinking mode or effort.
            extra.setdefault("reasoning_effort", "low")
        return {"system": self.system, "tools": self.tools, "temperature": self.temperature,
                "max_tokens": self.output_limit, "response_format": self.response_format,
                "extra_body": extra or None}


def openai_usage(usage: dict[str, Any]):
    return {"prompt_tokens": usage["input_total"], "completion_tokens": usage["output_total"],
            "total_tokens": usage["total_tokens"],
            "prompt_tokens_details": {"cached_tokens": usage.get("cache_read", 0)},
            "completion_tokens_details": {"reasoning_tokens": usage.get("reasoning", 0)}}


def _safe_error(error: BaseException):
    if isinstance(error, metering.QuotaError):
        return error.status_code, error.code, error.message
    if isinstance(error, ProviderRequestError):
        data = safe_provider_error_data(error.code)
        return 502, data["code"], data["message"]
    if isinstance(error, ProviderConfigurationError):
        return 503, "model_unavailable", "模型配置不可用，请管理员检查"
    if isinstance(error, HTTPException):
        return error.status_code, "runtime_unavailable", "运行实例身份已失效"
    data = safe_provider_error_data(_provider_error_code(error))
    return 502, data["code"], data["message"]


def _error_data(error, request_id=None):
    status, code, message = _safe_error(error)
    metadata = safe_provider_error_metadata(error)
    # Correlate only with our platform ledger identity. Vendor request IDs and
    # arbitrary SDK headers may contain data outside this public contract.
    metadata.update(safe_provider_error_metadata({"request_id": request_id}))
    return status, {"message": message, "type": code, "code": code,
                    "retryable": False, **metadata}


def _error_response(error, request_id=None):
    status, data = _error_data(error, request_id)
    logger.warning("Cloud inference failed request_id=%s code=%s upstream_status=%s",
                   data.get("request_id"), data["code"], data.get("upstream_status"))
    headers = {"X-Request-ID": data["request_id"]} if "request_id" in data else None
    return JSONResponse(status_code=status, content={"error": data}, headers=headers)


class InferenceAttempt:
    def __init__(self, request, body, user, route, provider):
        self.request, self.body, self.user, self.route, self.provider = request, body, user, route, provider
        self.database = request.app.state.database
        self.id = None
        self.usage = None
        self._finalized = False
        self._finalize_lock = asyncio.Lock()
        self.finish_reason = None
        self.chars = 0
        self.tool_index = 0
        self.heartbeat = None
        self.response_id = "chatcmpl-" + new_id()
        self.created = int(time.time())

    async def reserve(self):
        async with self.database.sessions() as db:
            # Same user-row lock as metering.reserve; admission and active
            # request count stay in one transaction, including parallel Agents.
            maximum = (await db.execute(update(User).where(User.id == self.user.id, User.is_active.is_(True))
                        .values(token_limit=User.token_limit).returning(User.max_concurrent_jobs))).scalar_one_or_none()
            if maximum is None:
                raise metering.QuotaError("account_disabled", "账号已停用", 403)
            active = await db.scalar(select(func.count()).select_from(RequestRecord).where(
                RequestRecord.user_id == self.user.id, RequestRecord.state.in_(("reserved", "dispatched"))))
            if active >= maximum:
                raise metering.QuotaError("concurrency_exceeded", "模型并发请求已达上限", 429)
            amount = input_reservation(self.body.messages, system=self.body.system, tools=self.body.tools,
                                       max_output_tokens=self.body.output_limit)
            # Additional wire fields (reasoning, schemas/formats, etc.) are
            # included in admission; this is an estimate, never a token bound.
            amount += len(json.dumps(self.body.provider_kwargs(self.route.provider), ensure_ascii=False).encode())
            record = await metering.reserve(db, self.user.id, self.route.id, None, amount,
                                            expected_route=self.route, source="agent", lease_owner=_API_OWNER,
                                            lease_until=utcnow() + timedelta(seconds=_LEASE_SECONDS))
            self.id = record.id

    async def _principal(self, db):
        from app.cloud.runtime_identity import get_runtime_principal
        return await get_runtime_principal(self.request, db)

    async def _renew(self, execution):
        try:
            while True:
                await asyncio.sleep(_LEASE_SECONDS / 4)
                async with self.database.sessions() as db:
                    await self._principal(db)
                    changed = await db.execute(update(RequestRecord).where(RequestRecord.id == self.id,
                        RequestRecord.lease_owner == _API_OWNER, RequestRecord.lease_until > utcnow(),
                        RequestRecord.state.in_(("reserved", "dispatched"))).values(
                            lease_until=utcnow() + timedelta(seconds=_LEASE_SECONDS)))
                    await db.commit()
                    if not changed.rowcount:
                        raise RuntimeError("Inference lease lost")
        except asyncio.CancelledError:
            raise
        except Exception:
            # A revoked credential, account or lost lease must close the
            # transport, not keep spending until its output timeout expires.
            execution.cancel()

    async def _dispatch(self):
        async with self.database.sessions() as db:
            await self._principal(db)
            owned = await db.execute(update(RequestRecord).where(RequestRecord.id == self.id,
                RequestRecord.lease_owner == _API_OWNER, RequestRecord.lease_until > utcnow(),
                RequestRecord.state == "reserved").values(lease_until=utcnow() + timedelta(seconds=_LEASE_SECONDS)))
            if not owned.rowcount:
                raise RuntimeError("Inference lease lost")
            if not await metering.mark_dispatched(db, self.id):
                raise RuntimeError("Attempt was already dispatched")

    async def finalize(self):
        # Starlette cancels the response task on a socket disconnect. Shield
        # the durable state transition and transport close from that scope.
        with anyio.CancelScope(shield=True):
            async with self._finalize_lock:
                if self._finalized:
                    return
                try:
                    if self.heartbeat:
                        self.heartbeat.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await self.heartbeat
                    if self.id:
                        async with self.database.sessions() as db:
                            record = await db.get(RequestRecord, self.id)
                            if record.state == "reserved":
                                await metering.release(db, self.id)
                            elif record.state in {"dispatched", "unknown"}:
                                if self.usage and self.usage.get("usage_status", "complete") == "complete":
                                    await metering.settle(db, self.id, self.usage)
                                else:
                                    await metering.mark_unknown(db, self.id, "Agent 模型请求结束但用量未完整确认")
                    self._finalized = True
                finally:
                    with contextlib.suppress(Exception):
                        await self.provider.aclose()

    async def chunks(self):
        try:
            self.heartbeat = asyncio.create_task(self._renew(asyncio.current_task()))
            await self._dispatch()
            async with asyncio.timeout(300):
                async for chunk in self.provider.stream_chat(self.route.model, self.body.messages,
                                                             **self.body.provider_kwargs(self.route.provider)):
                    kind, data = chunk.type, chunk.data
                    if kind == "usage":
                        # Cumulative receipts may be repeated; only one event
                        # settles the attempt, never a sum of snapshots.
                        self.usage = dict(data)
                    elif kind == "finish":
                        self.finish_reason = data.get("reason") or "stop"
                    elif kind == "error":
                        raise ProviderRequestError(data.get("message", "模型服务调用失败"),
                            data.get("upstream_status", data.get("status_code")), code=data.get("code"))
                    elif kind in {"text-delta", "reasoning-delta"}:
                        value = str(data.get("text", ""))
                        self.chars += len(value)
                        if self.chars > 1000000:
                            raise RuntimeError("Output limit exceeded")
                        yield {"content" if kind == "text-delta" else "reasoning_content": value}
                    elif kind == "tool-call":
                        arguments = data.get("arguments_json")
                        if arguments is None:
                            arguments = json.dumps(data.get("arguments", {}), ensure_ascii=False)
                        call = {"index": self.tool_index, "id": data.get("id") or "call_" + new_id(),
                                "type": "function", "function": {"name": data.get("name", ""), "arguments": arguments}}
                        self.tool_index += 1
                        self.chars += len(arguments)
                        if self.chars > 1000000:
                            raise RuntimeError("Output limit exceeded")
                        yield {"tool_calls": [call]}
            if not self.usage or self.usage.get("usage_status", "complete") != "complete":
                raise ProviderRequestError("Incomplete model usage", code="model_usage_unconfirmed")
            if self.finish_reason is None:
                raise ProviderRequestError("Incomplete model finish", code="model_response_incomplete")
        finally:
            await self.finalize()

    def envelope(self, delta=None, *, finish=None, usage=None):
        return {"id": self.response_id, "object": "chat.completion.chunk", "created": self.created,
                "model": self.body.model, "choices": [] if usage else [
                    {"index": 0, "delta": delta or {}, "finish_reason": finish}], "usage": usage}

    async def sse(self):
        def encode(data):
            return "data: " + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n\n"
        usage_sent = False
        include_usage = (self.body.stream_options or {}).get("include_usage", False)
        try:
            yield encode(self.envelope({"role": "assistant"}))
            async for delta in self.chunks():
                yield encode(self.envelope(delta))
            yield encode(self.envelope(finish=self.finish_reason))
            if include_usage:
                usage_sent = True
                yield encode(self.envelope(usage=openai_usage(self.usage)))
            yield "data: [DONE]\n\n"
        except (asyncio.CancelledError, GeneratorExit):
            raise
        except Exception as error:
            # A terminal content/finish failure can follow a complete billing
            # receipt. Preserve that receipt before the error so the engine
            # records the spend instead of showing zero tokens. Unknown or
            # partial receipts remain in the platform's reconciliation ledger.
            if include_usage and not usage_sent and self.usage and self.usage.get("usage_status", "complete") == "complete":
                yield encode(self.envelope(usage=openai_usage(self.usage)))
            _, data = _error_data(error, self.id)
            logger.warning("Cloud inference failed request_id=%s code=%s upstream_status=%s",
                           data.get("request_id"), data["code"], data.get("upstream_status"))
            yield encode({"error": data})
            yield "data: [DONE]\n\n"
        finally:
            await self.finalize()

    async def completion(self):
        message = {"role": "assistant", "content": ""}
        tools = []
        async for delta in self.chunks():
            for name in ("content", "reasoning_content"):
                if name in delta:
                    message[name] = message.get(name, "") + delta[name]
            tools.extend({k: v for k, v in call.items() if k != "index"} for call in delta.get("tool_calls", []))
        if tools:
            message["tool_calls"] = tools
            if not message["content"]:
                message["content"] = None
        return {"id": self.response_id, "object": "chat.completion", "created": self.created,
                "model": self.body.model, "choices": [{"index": 0, "message": message,
                    "finish_reason": self.finish_reason}], "usage": openai_usage(self.usage)}


@router.get("/models")
async def list_models(user: Principal, db: Db):
    routes = (await db.scalars(select(ModelRoute).where(ModelRoute.enabled.is_(True), ModelRoute.provider != "mock", ModelRoute.kind == "chat",
                               or_(ModelRoute.owner_user_id.is_(None), ModelRoute.owner_user_id == user.id))
                               .order_by(ModelRoute.id))).all()
    from app.cloud.model_metadata import public_model
    return {"object": "list", "data": [public_model(row) for row in routes]}


@router.post("/chat/completions")
async def chat_completions(body: ChatCompletionRequest, request: Request, user: Principal, db: Db):
    # FastAPI caches the received body while parsing the validated request.
    # Check the same UTF-8 wire bytes as the middleware, not a second JSON
    # representation with different whitespace/defaults or character counts.
    if len(await request.body()) > inference_payload.MAX_INFERENCE_BODY_BYTES:
        return JSONResponse(status_code=413, content=inference_payload.payload_error_body())
    route = await db.get(ModelRoute, body.model)
    if route is None or not route.enabled or route.provider == "mock" or route.kind != "chat" or route.owner_user_id not in (None, user.id):
        return JSONResponse(status_code=404, content={"error": {"code": "model_not_found", "type": "model_not_found",
                                                               "message": "模型不存在或未开放"}})
    attempt = None
    try:
        provider = build_provider(route, request.app.state.settings)
        attempt = InferenceAttempt(request, body, user, route, provider)
        reservation = asyncio.create_task(attempt.reserve())
        try:
            await asyncio.shield(reservation)
        except asyncio.CancelledError:
            # reserve may have committed before its result returned. Observe
            # completion and compensate using its known durable identity.
            with anyio.CancelScope(shield=True):
                await reservation
                await attempt.finalize()
            raise
        if body.stream:
            return StreamingResponse(attempt.sse(), media_type="text/event-stream",
                headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no", "X-Request-ID": attempt.id},
                background=BackgroundTask(attempt.finalize))
        result = await attempt.completion()
        return JSONResponse(result, headers={"X-Request-ID": attempt.id, "Cache-Control": "no-store"})
    except asyncio.CancelledError:
        raise
    except Exception as error:
        return _error_response(error, attempt.id if attempt is not None else None)
    finally:
        if attempt is not None and not body.stream:
            await attempt.finalize()
        elif attempt is not None and attempt.id is None:
            await attempt.finalize()


async def recover_inference_requests(database):
    """Fence only expired gateway attempts; live API workers are untouched.

    The expired request is claimed before recovery. A concurrent old stream
    may still report a receipt, which settle accepts idempotently from unknown.
    No recovered attempt is ever automatically replayed or refunded after send.
    """
    recovered = 0
    async with database.sessions() as db:
        ids = (await db.scalars(select(RequestRecord.id).where(RequestRecord.source.in_(("agent", "image", "tenant-provider", "mlx-node")),
            RequestRecord.state.in_(("reserved", "dispatched")), RequestRecord.lease_until < utcnow()))).all()
    for request_id in ids:
        async with database.sessions() as db:
            state = (await db.execute(update(RequestRecord).where(RequestRecord.id == request_id,
                RequestRecord.source.in_(("agent", "image", "tenant-provider", "mlx-node")), RequestRecord.state.in_(("reserved", "dispatched")),
                RequestRecord.lease_until < utcnow()).values(lease_owner="recovery-" + new_id(),
                lease_until=utcnow() + timedelta(seconds=_LEASE_SECONDS)).returning(RequestRecord.state))).scalar_one_or_none()
            await db.commit()
            if state is None:
                continue
            if state == "reserved":
                record = await db.get(RequestRecord, request_id)
                if record.source == "image":
                    from app.cloud.image_metering import finish
                    await finish(db, request_id, release=True)
                else:
                    await metering.release(db, request_id)
            else:
                await metering.mark_unknown(db, request_id, "推理网关租约过期，在途请求用量待核对")
            recovered += 1
    return recovered
