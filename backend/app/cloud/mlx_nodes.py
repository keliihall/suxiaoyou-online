"""Per-user pinned Mac nodes: operator config, runtime management and inference.

Only administrators choose a node URL. Node bearer credentials stay encrypted
in the control plane; engines receive only their revocable runtime capability.
Inference is centrally reserved and settled, not a transparent quota bypass.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import ipaddress
import hashlib
import json
import ssl
from types import SimpleNamespace
from urllib.parse import urlsplit

import anyio
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from starlette.background import BackgroundTask

from app.cloud import inference, metering
from app.cloud.database import get_db
from app.cloud.identity import require_admin
from app.cloud.models import AuditEvent, MlxNodeConfig, RequestRecord, User, utcnow
from app.cloud.providers import CloudProvider, _SafeSDKNode, _tokens, input_reservation
from app.schemas.provider import StreamChunk
from app.cloud.runtime_identity import get_runtime_principal
from app.cloud.secrets import ModelSecretVault
from app.cloud.mlx_node_server import (StartBody, CachedBody, RemoveBody, approved_model_catalog,
    bare_rapid_mlx_model, bounded_json, MAX_APPROVED_MODELS)

router = APIRouter()


def validate_url(value):
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError:
        raise ValueError("Invalid node origin") from None
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment or parts.path not in {"", "/"}):
        raise ValueError("Node URL must be a fixed HTTPS origin without credentials, path or query")
    try:
        address = ipaddress.ip_address(parts.hostname)
    except ValueError:
        address = None
    if address and (address.is_link_local or address.is_multicast or address.is_unspecified):
        raise ValueError("This node address is not permitted")
    if port is not None and not 1024 <= port <= 65535:
        raise ValueError("Node port must be unprivileged")
    if any(ord(char) < 33 for char in value) or len(value) > 2048:
        raise ValueError("Invalid node URL")
    hostname = parts.hostname.lower()
    if ":" in hostname:
        hostname = "[" + hostname + "]"
    return "https://" + hostname + (":" + str(port) if port else "")


def certificate_context(pem):
    try:
        if pem.count("-----BEGIN CERTIFICATE-----") != 1:
            raise ValueError()
        certificate = x509.load_pem_x509_certificate(pem.encode())
        if certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
            raise ValueError()
        now = datetime.now(timezone.utc)
        if not certificate.not_valid_before_utc <= now <= certificate.not_valid_after_utc:
            raise ValueError()
        # The sole trust anchor is the non-CA leaf, not a public/root CA. No
        # bearer is sent until hostname + this exact certificate are verified.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True
        context.load_verify_locations(cadata=pem)
        context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
        return context, certificate.fingerprint(hashes.SHA256()).hex()
    except (ValueError, x509.ExtensionNotFound, ssl.SSLError):
        raise ValueError("A current non-CA node leaf certificate PEM is required") from None


class NodeConfigBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    url: str = Field(max_length=2048)
    enabled: bool
    token: str | None = Field(default=None, max_length=1024)
    certificate_pem: str | None = Field(default=None, max_length=8192)


def config_view(config):
    return {"configured": config is not None, "enabled": bool(config and config.enabled),
        "url": config.url if config else "", "token_configured": bool(config and config.token_encrypted),
        "certificate_fingerprint": config.certificate_fingerprint if config else ""}


@router.get("/api/cloud/admin/users/{user_id}/mlx-node")
async def get_config(user_id: str, admin=Depends(require_admin), db=Depends(get_db)):
    if await db.get(User, user_id) is None:
        raise HTTPException(404, "User not found")
    return config_view(await db.get(MlxNodeConfig, user_id))


@router.put("/api/cloud/admin/users/{user_id}/mlx-node")
async def put_config(user_id: str, body: NodeConfigBody, request: Request,
                     admin=Depends(require_admin), db=Depends(get_db)):
    if await db.get(User, user_id) is None:
        raise HTTPException(404, "User not found")
    config = await db.get(MlxNodeConfig, user_id)
    try:
        url = validate_url(body.url)
        pem = (body.certificate_pem or "").strip() or (config.certificate_pem if config else "")
        _, fingerprint = certificate_context(pem)
        token = (body.token or "").strip()
        if token and (len(token) < 32 or any(ord(char) < 33 or ord(char) > 126 for char in token)):
            raise ValueError("Node bearer must contain at least 32 printable characters")
        if not token and config is None:
            raise ValueError("A node bearer is required for the first configuration")
        ciphertext = ModelSecretVault(request.app.state.settings).seal(token) if token else config.token_encrypted
        if config is None:
            config = MlxNodeConfig(user_id=user_id, url=url, certificate_pem=pem,
                certificate_fingerprint=fingerprint, token_encrypted=ciphertext, enabled=body.enabled)
            db.add(config)
        else:
            config.url, config.certificate_pem, config.certificate_fingerprint = url, pem, fingerprint
            config.token_encrypted, config.enabled = ciphertext, body.enabled
            config.version += 1
            config.updated_at = utcnow()
        db.add(AuditEvent(actor_user_id=admin.id, target_user_id=user_id, action="mlx_node.configured",
            details={"enabled": body.enabled, "certificate_fingerprint": fingerprint,
                     "credential_rotated": bool(token)}))
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "This node origin or certificate is already assigned to another user") from None
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    return config_view(config)


async def _config(request, db):
    user = await get_runtime_principal(request, db)
    config = await db.get(MlxNodeConfig, user.id)
    return user, config if config and config.enabled else None


def node_client(config, settings, *, timeout=600):
    context, fingerprint = certificate_context(config.certificate_pem)
    if fingerprint != config.certificate_fingerprint:
        raise ValueError("Node certificate pin does not match its configuration")
    token = ModelSecretVault(settings).unseal(config.token_encrypted)
    return httpx.AsyncClient(verify=context, trust_env=False, follow_redirects=False,
        timeout=httpx.Timeout(timeout, connect=10), headers={"Authorization": "Bearer " + token})


def unavailable():
    return {"configured": False, "platform_supported": False, "binary_installed": False,
        "running": False, "process_running": False, "port": 18080, "base_url": None,
        "version": None, "current_model": "qwen3.5-4b", "executable_path": None,
        "install_commands": [], "unavailable_reason": "node_not_configured"}


def public_status(data):
    # Only the original UI schema and status fields are forwarded. Never expose
    # operator node URLs, filesystem roots or credentials in the tenant UI.
    result = {key: data.get(key, value) for key, value in unavailable().items()}
    result["base_url"] = "/cloud/mlx-node/v1" if result["running"] else None
    result["executable_path"], result["install_commands"] = None, []
    return result


@router.api_route("/api/cloud/internal/mlx-node/rapid-mlx/{operation}", methods=["GET", "POST", "DELETE"])
async def management(operation: str, request: Request, db=Depends(get_db)):
    methods = {"status": "GET", "cached": "POST", "start": "POST", "stop": "POST", "remove": "POST", "uninstall": "DELETE"}
    if methods.get(operation) != request.method:
        raise HTTPException(404, "Unknown node operation")
    user, config = await _config(request, db)
    if config is None:
        if operation == "status":
            return unavailable()
        raise HTTPException(503, "An exclusive Mac node has not been configured for this account")
    payload = None
    try:
        if operation in {"start", "cached", "remove"}:
            schema = {"start": StartBody, "cached": CachedBody, "remove": RemoveBody}[operation]
            payload = schema.model_validate(await bounded_json(request)).model_dump()
        params = None
        if operation == "uninstall":
            raw = request.query_params.get("delete_models", "true")
            if raw not in {"true", "false"} or set(request.query_params) - {"delete_models"}:
                raise HTTPException(400, "Invalid node uninstall parameters")
            params = {"delete_models": raw}
        async with node_client(config, request.app.state.settings, timeout=120) as client:
            result = await client.request(request.method, config.url + "/rapid-mlx/" + operation,
                json=payload, params=params)
            if result.status_code != 200:
                # Node errors are intentionally bounded fixed messages, but a
                # compromised/unrelated endpoint must not echo credential text.
                raise HTTPException(result.status_code if result.status_code in {400, 409, 503} else 502,
                                    "The Mac node rejected this operation")
            data = result.json()
            if operation in {"status", "start", "stop"}:
                return public_status(data)
            if operation in {"cached", "remove"}:
                cached = data.get("cached", {})
                expected = payload.get("aliases", [payload.get("alias")])
                return {"cached": {alias: bool(cached.get(alias)) for alias in expected}}
            return {"stopped": bool(data.get("stopped")), "removed_models": data.get("removed_models", []),
                    "freed_bytes": max(0, int(data.get("freed_bytes", 0))), "errors": [], "binary_install_commands": []}
    except ValidationError:
        raise HTTPException(400, "Invalid node operation payload") from None
    except (httpx.HTTPError, ValueError, TypeError):
        raise HTTPException(502, "The pinned Mac node connection is unavailable") from None


class MacNodeProvider(CloudProvider):
    async def stream_chat(self, model, messages, **kwargs):
        params = {"model": model, "messages": self.delegate._build_messages(kwargs.get("system"), messages),
                  "stream": True, "stream_options": {"include_usage": True}}
        for key in ("tools", "temperature", "max_tokens", "response_format", "extra_body"):
            if kwargs.get(key) is not None:
                params[key] = kwargs[key]
        calls = {}
        stream = await self.delegate._client.chat.completions.create(**params)
        async with stream:
            async for item in stream:
                choice = item.choices[0] if item.choices else None
                delta = choice.delta if choice else None
                if delta:
                    for name, kind in (("content", "text-delta"), ("reasoning_content", "reasoning-delta")):
                        value = getattr(delta, name, None)
                        if value:
                            yield StreamChunk(type=kind, data={"text": value})
                    for call in delta.tool_calls or []:
                        collected = calls.setdefault(call.index, {"id": "", "name": "", "arguments": ""})
                        if call.id:
                            collected["id"] = call.id
                        if call.function:
                            if call.function.name:
                                collected["name"] += call.function.name
                            if call.function.arguments:
                                collected["arguments"] += call.function.arguments
                if item.usage:
                    raw = item.usage.model_dump(mode="json", exclude_none=True)
                    prompt, completion = _tokens(raw.get("prompt_tokens"), "prompt"), _tokens(raw.get("completion_tokens"), "completion")
                    read = _tokens((raw.get("prompt_tokens_details") or {}).get("cached_tokens", 0), "cache_read")
                    reasoning = _tokens((raw.get("completion_tokens_details") or {}).get("reasoning_tokens", 0), "reasoning")
                    total = _tokens(raw.get("total_tokens"), "total")
                    yield StreamChunk(type="usage", data={"input_total": prompt, "output_total": completion,
                        "cache_read": read, "cache_write": 0, "reasoning": reasoning, "total_tokens": prompt + completion,
                        "provider_total": total, "usage_status": "complete" if total == prompt + completion else "partial",
                        "raw_usage": {"receipt_source": "mac_node_proxy", "provider_receipt": raw}})
                if choice and choice.finish_reason:
                    for index, call in sorted(calls.items()):
                        try:
                            arguments = json.loads(call["arguments"] or "{}")
                        except ValueError:
                            arguments = {"_raw": call["arguments"]}
                        yield StreamChunk(type="tool-call", data={"id": call["id"], "name": call["name"],
                            "arguments": arguments, "arguments_json": call["arguments"]})
                    calls.clear()
                    yield StreamChunk(type="finish", data={"reason": choice.finish_reason})


def inference_provider(config, settings):
    from app.provider.generic_openai import GenericOpenAIProvider
    client = node_client(config, settings)
    token = ModelSecretVault(settings).unseal(config.token_encrypted)
    delegate = GenericOpenAIProvider.__new__(GenericOpenAIProvider)
    delegate._provider_id, delegate._api_key, delegate._kind = "rapid-mlx", token, "openai_compat_custom"
    delegate._models_cache, delegate._models_override = None, []
    delegate._client = _SafeSDKNode(AsyncOpenAI(api_key=token, base_url=config.url + "/v1", http_client=client,
        max_retries=0, timeout=httpx.Timeout(600, connect=10)))
    return MacNodeProvider(delegate, "rapid-mlx")


class MacNodeAttempt(inference.InferenceAttempt):
    async def reserve(self):
        async with self.database.sessions() as db:
            maximum = (await db.execute(update(User).where(User.id == self.user.id, User.is_active.is_(True))
                .values(token_limit=User.token_limit).returning(User.max_concurrent_jobs))).scalar_one_or_none()
            if maximum is None:
                raise metering.QuotaError("account_disabled", "账号已停用", 403)
            active = await db.scalar(select(func.count()).select_from(RequestRecord).where(
                RequestRecord.user_id == self.user.id, RequestRecord.state.in_(("reserved", "dispatched"))))
            if active >= maximum:
                raise metering.QuotaError("concurrency_exceeded", "模型并发请求已达上限", 429)
            amount = input_reservation(self.body.messages, system=self.body.system, tools=self.body.tools,
                max_output_tokens=self.body.output_limit) + len(json.dumps(self.body.model_dump()).encode())
            record = await metering.reserve(db, self.user.id, self.route.id, None, amount,
                source="mlx-node", lease_owner=inference._API_OWNER,
                lease_until=utcnow() + timedelta(seconds=inference._LEASE_SECONDS))
            self.id = record.id

    async def _principal(self, db):
        user = await super()._principal(db)
        current = await db.get(MlxNodeConfig, user.id)
        if not current or not current.enabled or current.version != self.route.node_version:
            raise HTTPException(409, "Mac node configuration was revoked")
        return user


async def node_approved_models(config, settings):
    """Only the pinned operator node can authorize new model identifiers."""
    try:
        async with node_client(config, settings, timeout=10) as client:
            catalog = await client.get(config.url + "/rapid-mlx/catalog")
            if catalog.status_code != 200 or len(catalog.content) > 128 * 1024:
                raise HTTPException(503, "The Mac node approved catalog is unavailable; update its service")
            mapping = approved_model_catalog(catalog.json())
            result = await client.get(config.url + "/v1/models")
            if result.status_code != 200 or len(result.content) > 128 * 1024:
                raise HTTPException(503, "The node model is not ready")
            data = result.json()
        aliases, repos = set(mapping), set(mapping.values())
        rows = data.get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list) or len(rows) > MAX_APPROVED_MODELS:
            raise ValueError("Invalid node models response")
        visible = [{"id": row["id"], "object": "model", "created": 0,
            "owned_by": "exclusive-mac-node"} for row in data.get("data", [])
            if isinstance(row, dict) and isinstance(row.get("id"), str)
            and len(row["id"]) <= 120 and row["id"] in aliases | repos]
        running_repos = {mapping.get(row["id"], row["id"]) for row in visible}
        allowed = {alias for alias, repo in mapping.items() if repo in running_repos} | running_repos
        return visible, allowed
    except (httpx.HTTPError, ValueError, TypeError):
        raise HTTPException(502, "The pinned Mac node connection is unavailable") from None


@router.get("/api/cloud/internal/mlx-node/v1/models")
async def models(request: Request, db=Depends(get_db)):
    _, config = await _config(request, db)
    if config is None:
        return {"object": "list", "data": []}
    visible, _ = await node_approved_models(config, request.app.state.settings)
    return {"object": "list", "data": visible}


@router.post("/api/cloud/internal/mlx-node/v1/chat/completions")
async def completions(body: inference.ChatCompletionRequest, request: Request, db=Depends(get_db)):
    user, config = await _config(request, db)
    if config is None:
        raise HTTPException(503, "An exclusive Mac node has not been configured for this account")
    model_id = "tenant/rapid-mlx/" + body.model
    if len(model_id) > 120:
        model_id = model_id[:85] + "/" + hashlib.sha256(model_id.encode()).hexdigest()[:32]
    route = SimpleNamespace(id=model_id, provider="rapid-mlx", model=body.model,
                            node_version=config.version)
    attempt = None
    try:
        _, approved = await node_approved_models(config, request.app.state.settings)
        if bare_rapid_mlx_model(body.model) not in approved:
            return JSONResponse(status_code=400, content={"error": {
                "message": "模型不在专属节点的已批准运行列表", "type": "invalid_request_error",
                "code": "model_unavailable"}})
        provider = inference_provider(config, request.app.state.settings)
        attempt = MacNodeAttempt(request, body, user, route, provider)
        reservation = asyncio.create_task(attempt.reserve())
        try:
            await asyncio.shield(reservation)
        except asyncio.CancelledError:
            with anyio.CancelScope(shield=True):
                await reservation
                await attempt.finalize()
            raise
        if body.stream:
            return StreamingResponse(attempt.sse(), media_type="text/event-stream",
                headers={"Cache-Control": "no-store", "X-Request-ID": attempt.id},
                background=BackgroundTask(attempt.finalize))
        return JSONResponse(await attempt.completion(), headers={"X-Request-ID": attempt.id})
    except asyncio.CancelledError:
        raise
    except Exception as error:
        return inference._error_response(error)
    finally:
        if attempt is not None and (not body.stream or attempt.id is None):
            await attempt.finalize()
