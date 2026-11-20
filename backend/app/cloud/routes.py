"""Legacy cloud control-plane resources; complete desktop APIs use the tenant runtime proxy."""
from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import re
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.cloud.database import get_db
from app.cloud.identity import require_admin, require_user
from app.cloud.models import (
    AuditEvent, Conversation, FileAsset, Job, JobEvent, Message, ModelRoute,
    QuotaBucket, RequestRecord, User, Workspace, new_id, utcnow,
)
from app.cloud import metering
from app.cloud.storage import StorageError, media_type, safe_filename

router = APIRouter()
Db = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(require_user)]
Admin = Annotated[User, Depends(require_admin)]
TERMINAL = {"completed", "failed", "cancelled", "needs_review"}


def fail(code: str, message: str, status: int = 400):
    raise HTTPException(status, detail={"code": code, "message": message})


def fields(obj: Any, names: str) -> dict:
    return {name: getattr(obj, name) for name in names.split()}


def file_view(obj: FileAsset) -> dict:
    return fields(obj, "id workspace_id filename content_type size sha256 created_at")


def job_view(obj: Job) -> dict:
    return fields(obj, "id user_id conversation_id state prompt model_id max_output_tokens file_ids answer error cancel_requested created_at updated_at attempt_id")


def admin_job_view(obj: Job) -> dict:
    # Platform operations do not imply permission to read users' documents.
    return fields(obj, "id user_id conversation_id state model_id max_output_tokens cancel_requested created_at updated_at attempt_id")


def model_view(obj: ModelRoute, admin: bool = False) -> dict:
    result = fields(obj, "id name provider model kind owner_user_id image_price_microusd capabilities enabled strict_quota input_price_microusd_per_million output_price_microusd_per_million")
    if admin:
        result.update(fields(obj, "api_key_env base_url"))
        managed = bool(obj.api_key_encrypted)
        environment = bool(obj.api_key_env and os.environ.get(obj.api_key_env))
        result["key_configured"] = managed or environment
        result["credential_source"] = "managed" if managed else "environment" if environment else "none"
        custom = bool(obj.custom_headers_encrypted)
        result["custom_headers_configured"] = custom
        result["allow_unauthenticated"] = bool(obj.allow_unauthenticated)
        result["authentication_mode"] = (
            "api_key_and_headers" if (managed or environment) and custom
            else "api_key" if managed or environment else "headers" if custom
            else "none" if obj.allow_unauthenticated else "missing")
        result["authentication_configured"] = result["authentication_mode"] != "missing"
    return result


async def owned(db: AsyncSession, model, item_id: str, user_id: str):
    stmt = select(model).where(model.id == item_id, model.user_id == user_id)
    if model is Conversation:
        stmt = stmt.where(Conversation.deleted_at.is_(None))
    obj = await db.scalar(stmt)
    if obj is None:
        fail("not_found", "资源不存在", 404)
    return obj


def audit(db: AsyncSession, actor: str, action: str, details: dict, target: str | None = None):
    db.add(AuditEvent(id=new_id(), actor_user_id=actor, target_user_id=target,
                      action=action, details=details))


async def lock_user(db: AsyncSession, user: User):
    user_id = user.id
    if db.get_bind().dialect.name == "sqlite" and db.in_transaction():
        await db.rollback()
    await db.execute(update(User).where(User.id == user_id).values(token_limit=User.token_limit))
    await db.refresh(user)
    if not user.is_active:
        fail("account_disabled", "账号已停用", 403)


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkspaceCreate(Body):
    name: str = Field(min_length=1, max_length=100)


class ConversationCreate(Body):
    workspace_id: str
    title: str = Field(default="新会话", min_length=1, max_length=160)


class JobCreate(Body):
    conversation_id: str
    prompt: str = Field(min_length=1, max_length=60000)
    model_id: str = Field(min_length=1, max_length=120)
    max_output_tokens: int = Field(default=2048, ge=16, le=8192)
    file_ids: list[str] = Field(default_factory=list, max_length=10)
    idempotency_key: str = Field(min_length=8, max_length=120)


class RouteCreate(Body):
    id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9._-]+$")
    name: str = Field(min_length=1, max_length=120)
    provider: str = Field(min_length=1, max_length=80)
    kind: Literal["chat", "image"] = "chat"
    image_price_microusd: int = Field(default=0, ge=0, le=10**12)
    capabilities: dict[str, Any] = Field(default_factory=dict)

    @field_validator("capabilities")
    @classmethod
    def model_capabilities(cls, value):
        from app.cloud.model_metadata import validated_overrides
        return validated_overrides(value)
    model: str = Field(min_length=1, max_length=200)
    enabled: bool = True
    api_key_env: str = ""
    api_key: SecretStr | None = None
    clear_api_key: bool = False
    custom_headers: dict[str, SecretStr] | None = None
    allow_unauthenticated: bool = False
    base_url: str | None = None
    strict_quota: bool = False
    input_price_microusd_per_million: int = Field(default=0, ge=0, le=10**12)
    output_price_microusd_per_million: int = Field(default=0, ge=0, le=10**12)

    @field_validator("custom_headers", mode="before")
    @classmethod
    def private_headers(cls, value):
        if value is None:
            return None
        from app.cloud.custom_headers import validate_headers
        return validate_headers(value)

    @field_validator("api_key_env")
    @classmethod
    def env_name(cls, value):
        if value and not re.fullmatch(r"SUXIAOYOU_MODEL_[A-Z0-9_]+", value):
            raise ValueError("模型密钥变量必须以 SUXIAOYOU_MODEL_ 开头")
        return value

    @field_validator("base_url")
    @classmethod
    def endpoint(cls, value):
        if value:
            from urllib.parse import urlparse
            import ipaddress
            parsed = urlparse(value)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("模型接口必须为不含凭证的 HTTPS URL")
            if parsed.hostname.lower() in {"localhost", "localhost.localdomain", "metadata.google.internal"}:
                raise ValueError("禁止本地或元数据地址")
            try:
                address = ipaddress.ip_address(parsed.hostname)
                if not address.is_global:
                    raise ValueError("禁止私有或元数据地址")
            except ValueError as exc:
                if "禁止" in str(exc):
                    raise
        return value


class Reconcile(Body):
    action: str = Field(pattern="^(release|settle)$")
    usage: dict[str, Any] | None = None
    reason: str = Field(min_length=3, max_length=500)


@router.get("/workspaces")
async def workspaces(user: CurrentUser, db: Db):
    rows = (await db.scalars(select(Workspace).where(Workspace.user_id == user.id).order_by(Workspace.created_at))).all()
    return [fields(row, "id name revision created_at") for row in rows]


@router.post("/workspaces", status_code=201)
async def create_workspace(body: WorkspaceCreate, user: CurrentUser, db: Db):
    row = Workspace(id=new_id(), user_id=user.id, name=body.name.strip())
    if not row.name:
        fail("invalid_name", "请输入工作区名称")
    db.add(row)
    await db.commit()
    return fields(row, "id name revision created_at")


@router.get("/conversations")
async def conversations(user: CurrentUser, db: Db, workspace_id: str | None = None):
    stmt = select(Conversation).where(Conversation.user_id == user.id, Conversation.deleted_at.is_(None))
    if workspace_id:
        await owned(db, Workspace, workspace_id, user.id)
        stmt = stmt.where(Conversation.workspace_id == workspace_id)
    rows = (await db.scalars(stmt.order_by(Conversation.updated_at.desc()).limit(200))).all()
    return [fields(row, "id workspace_id title created_at updated_at") for row in rows]


@router.post("/conversations", status_code=201)
async def create_conversation(body: ConversationCreate, user: CurrentUser, db: Db):
    await owned(db, Workspace, body.workspace_id, user.id)
    row = Conversation(id=new_id(), user_id=user.id, workspace_id=body.workspace_id, title=body.title)
    db.add(row)
    await db.commit()
    return fields(row, "id workspace_id title created_at updated_at")


@router.get("/conversations/{item_id}")
async def conversation(item_id: str, user: CurrentUser, db: Db):
    row = await owned(db, Conversation, item_id, user.id)
    messages = (await db.scalars(select(Message).where(Message.conversation_id == item_id,
                            Message.user_id == user.id).order_by(Message.created_at, Message.id))).all()
    return {**fields(row, "id workspace_id title created_at updated_at"),
            "messages": [fields(m, "id role content created_at") for m in messages]}


@router.delete("/conversations/{item_id}")
async def delete_conversation(item_id: str, user: CurrentUser, db: Db):
    await owned(db, Conversation, item_id, user.id)
    # Serialize against submissions so deletion cannot race a new queued job.
    await lock_user(db, user)
    active = await db.scalar(select(Job.id).where(Job.conversation_id == item_id,
                            Job.user_id == user.id, Job.state.in_(["queued", "running"])).limit(1))
    if active:
        fail("conversation_busy", "请先停止会话中的任务", 409)
    await db.execute(delete(Message).where(Message.conversation_id == item_id, Message.user_id == user.id))
    job_ids = select(Job.id).where(Job.conversation_id == item_id, Job.user_id == user.id)
    await db.execute(delete(JobEvent).where(JobEvent.job_id.in_(job_ids), JobEvent.user_id == user.id))
    await db.execute(update(Job).where(Job.conversation_id == item_id, Job.user_id == user.id)
                     .values(prompt="[会话内容已删除]", answer="", file_ids=[]))
    # Keep job/attempt/usage history independently of visible conversations.
    await db.execute(update(Conversation).where(Conversation.id == item_id, Conversation.user_id == user.id)
                     .values(deleted_at=utcnow(), title="已删除会话"))
    await db.commit()
    return {"ok": True}


@router.get("/files")
async def files(user: CurrentUser, db: Db, workspace_id: str | None = None):
    stmt = select(FileAsset).where(FileAsset.user_id == user.id)
    if workspace_id:
        await owned(db, Workspace, workspace_id, user.id)
        stmt = stmt.where(FileAsset.workspace_id == workspace_id)
    return [file_view(row) for row in (await db.scalars(stmt.order_by(FileAsset.created_at.desc()).limit(500))).all()]


@router.post("/files", status_code=201)
async def upload_file(request: Request, file: UploadFile, workspace_id: str, user: CurrentUser, db: Db):
    await owned(db, Workspace, workspace_id, user.id)
    try:
        name = safe_filename(file.filename or "")
        cap = request.app.state.settings.max_upload_bytes
        data = await file.read(cap + 1)
        if len(data) > cap:
            fail("file_too_large", f"文件超过 {cap // 1024 // 1024} MB 限制", 413)
        if not data:
            fail("empty_file", "文件为空")
        # Files are immutable and never deduplicated across users.
        key, checksum = await request.app.state.object_store.put(user.id, workspace_id, data)
        row = FileAsset(id=new_id(), user_id=user.id, workspace_id=workspace_id,
                        filename=name, content_type=media_type(name), size=len(data), sha256=checksum, object_key=key)
        db.add(row)
        try:
            await db.commit()
        except Exception:
            await request.app.state.object_store.delete(key)
            raise
        return file_view(row)
    except StorageError as exc:
        fail("invalid_file", str(exc))
    finally:
        await file.close()


@router.get("/files/{item_id}/download")
async def download_file(item_id: str, request: Request, user: CurrentUser, db: Db):
    row = await owned(db, FileAsset, item_id, user.id)
    try:
        data = await request.app.state.object_store.get(row.object_key)
    except StorageError:
        fail("not_found", "文件不存在", 404)
    from urllib.parse import quote
    return Response(data, media_type="application/octet-stream", headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(row.filename)}",
        "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
    })


@router.delete("/files/{item_id}")
async def delete_file(item_id: str, request: Request, user: CurrentUser, db: Db):
    row = await owned(db, FileAsset, item_id, user.id)
    await lock_user(db, user)
    await db.refresh(row)
    active_jobs = (await db.scalars(select(Job).where(Job.user_id == user.id,
                            Job.state.in_(["queued", "running"])))).all()
    if any(item_id in j.file_ids for j in active_jobs):
        fail("file_busy", "文件正被任务使用，请先停止任务", 409)
    key = row.object_key
    await db.delete(row)
    await db.commit()
    # If object cleanup fails the asset is already inaccessible; an orphan may
    # be collected later, never expose it by rolling back authorization.
    await request.app.state.object_store.delete(key)
    return {"ok": True}


@router.get("/models")
async def models(user: CurrentUser, db: Db):
    return [model_view(row) for row in (await db.scalars(select(ModelRoute).where(ModelRoute.enabled.is_(True), ModelRoute.kind == "chat",
        or_(ModelRoute.owner_user_id.is_(None), ModelRoute.owner_user_id == user.id)).order_by(ModelRoute.name))).all()]


@router.post("/jobs", status_code=201)
async def create_job(body: JobCreate, request: Request, user: CurrentUser, db: Db):
    # A row lock / conditional write serializes per-user queue admission on PG
    # and SQLite, including concurrent API instances.
    await lock_user(db, user)
    existing = await db.scalar(select(Job).where(Job.user_id == user.id, Job.idempotency_key == body.idempotency_key))
    if existing:
        if (existing.conversation_id != body.conversation_id or existing.prompt != body.prompt or
                existing.model_id != body.model_id or existing.file_ids != body.file_ids or
                existing.max_output_tokens != body.max_output_tokens):
            fail("idempotency_conflict", "此请求标识已用于其他内容", 409)
        return job_view(existing)
    conv = await owned(db, Conversation, body.conversation_id, user.id)
    route = await db.scalar(select(ModelRoute).where(ModelRoute.id == body.model_id, ModelRoute.enabled.is_(True), ModelRoute.kind == "chat",
        or_(ModelRoute.owner_user_id.is_(None), ModelRoute.owner_user_id == user.id)))
    if route is None:
        fail("model_unavailable", "模型不可用", 400)
    if route.provider == "mock" and not request.app.state.settings.allow_mock_provider:
        fail("model_unavailable", "测试供应商未启用", 400)
    if route.strict_quota and route.provider != "mock":
        fail("strict_quota_unavailable", "此模型尚未提供可验证的严格输入上界", 400)
    active = (await db.scalars(select(Job).where(Job.user_id == user.id, Job.state.in_(["queued", "running"])))).all()
    if any(j.conversation_id == conv.id for j in active):
        fail("conversation_busy", "此会话已有任务，请等待或停止后再发送", 409)
    if len(active) >= user.max_concurrent_jobs:
        fail("concurrency_exceeded", "已达到账号的并发任务上限", 429)
    bucket = await db.scalar(select(QuotaBucket).where(QuotaBucket.user_id == user.id,
                                 QuotaBucket.period == metering.billing_period()))
    unavailable = (bucket.used_tokens + bucket.reserved_tokens) if bucket else 0
    if unavailable >= user.token_limit:
        fail("quota_exceeded", "本月 Token 可用额度不足，请联系管理员调整或等待账期更新", 429)
    for item_id in set(body.file_ids):
        asset = await owned(db, FileAsset, item_id, user.id)
        if asset.workspace_id != conv.workspace_id:
            fail("file_workspace_mismatch", "附件不属于当前工作区", 400)
    if not body.prompt.strip():
        fail("invalid_prompt", "请输入任务内容")
    row = Job(id=new_id(), user_id=user.id, conversation_id=conv.id, prompt=body.prompt,
              model_id=body.model_id, max_output_tokens=body.max_output_tokens,
              file_ids=body.file_ids, idempotency_key=body.idempotency_key, state="queued")
    db.add(row)
    db.add(Message(id=new_id(), user_id=user.id, conversation_id=conv.id, role="user", content=body.prompt))
    conv.updated_at = utcnow()
    if conv.title == "新会话":
        conv.title = body.prompt.strip()[:60]
    await db.flush()
    db.add(JobEvent(job_id=row.id, user_id=user.id, kind="queued", data={"state": "queued"}))
    await db.commit()
    return job_view(row)


@router.get("/jobs")
async def jobs(user: CurrentUser, db: Db):
    return [job_view(row) for row in (await db.scalars(select(Job).where(Job.user_id == user.id).order_by(Job.created_at.desc()).limit(200))).all()]


@router.get("/jobs/{item_id}")
async def job(item_id: str, user: CurrentUser, db: Db):
    return job_view(await owned(db, Job, item_id, user.id))


async def cancel(db: AsyncSession, row: Job, actor_id: str, admin: bool = False):
    if row.state not in TERMINAL:
        result = await db.execute(update(Job).where(Job.id == row.id, Job.state.in_(["queued", "running"]))
            .values(cancel_requested=True, updated_at=utcnow(),
                    state=case((Job.state == "queued", "cancelled"), else_=Job.state))
            .returning(Job.state))
        updated_state = result.scalar_one_or_none()
        if updated_state == "cancelled":
            db.add(JobEvent(job_id=row.id, user_id=row.user_id, kind="cancelled", data={"state": "cancelled"}))
        audit(db, actor_id, "admin.job.cancel" if admin else "job.cancel", {"job_id": row.id}, row.user_id)
        await db.commit()
    await db.refresh(row)
    return job_view(row)


@router.post("/jobs/{item_id}/cancel")
async def cancel_job(item_id: str, user: CurrentUser, db: Db):
    return await cancel(db, await owned(db, Job, item_id, user.id), user.id)


@router.get("/jobs/{item_id}/events")
async def events(item_id: str, request: Request, user: CurrentUser, db: Db, after: int = Query(0, ge=0)):
    await owned(db, Job, item_id, user.id)
    try:
        cursor = max(after, int(request.headers.get("last-event-id", "0")))
    except ValueError:
        fail("invalid_cursor", "事件序号无效")
    user_id = user.id
    database = request.app.state.database
    async def stream():
        nonlocal cursor
        idle = 0
        while not await request.is_disconnected():
            async with database.sessions() as stream_db:
                # Recheck session revocation/disabled accounts on every poll.
                try:
                    authenticated = await require_user(request, stream_db)
                except HTTPException:
                    return
                if authenticated.id != user_id:
                    return
                batch = (await stream_db.scalars(select(JobEvent).where(JobEvent.job_id == item_id,
                          JobEvent.user_id == user_id, JobEvent.id > cursor).order_by(JobEvent.id).limit(100))).all()
                current = await stream_db.scalar(select(Job.state).where(Job.id == item_id, Job.user_id == user_id))
                for event in batch:
                    cursor = event.id
                    yield f"id: {event.id}\nevent: {event.kind}\ndata: {json.dumps(jsonable_encoder(event.data), ensure_ascii=False)}\n\n"
                if current in TERMINAL and len(batch) < 100:
                    return
            idle += 1
            if idle % 20 == 0:
                yield ": heartbeat\n\n"
            await asyncio.sleep(0.25)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-store", "X-Accel-Buffering": "no", "Connection": "keep-alive",
    })


@router.get("/usage")
async def usage(user: CurrentUser, db: Db):
    return await metering.usage_summary(db, user.id)


def request_view(row: RequestRecord):
    return fields(row, "id user_id job_id model_id source state reserved_tokens total_tokens cost_microusd price_snapshot normalized_usage error created_at dispatched_at settled_at")


@router.get("/usage/requests")
async def requests_usage(user: CurrentUser, db: Db):
    return [request_view(row) for row in (await db.scalars(select(RequestRecord).where(RequestRecord.user_id == user.id).order_by(RequestRecord.created_at.desc()).limit(200))).all()]


@router.get("/admin/jobs")
async def admin_jobs(admin: Admin, db: Db):
    return [admin_job_view(row) for row in (await db.scalars(select(Job).order_by(Job.created_at.desc()).limit(300))).all()]


@router.post("/admin/jobs/{item_id}/cancel")
async def admin_cancel(item_id: str, admin: Admin, db: Db):
    row = await db.get(Job, item_id)
    if row is None:
        fail("not_found", "任务不存在", 404)
    await cancel(db, row, admin.id, True)
    return admin_job_view(row)


@router.get("/admin/usage")
async def admin_usage(admin: Admin, db: Db):
    rows = (await db.scalars(select(User).order_by(User.email))).all()
    users = [{"user_id": row.id, "email": row.email, **await metering.usage_summary(db, row.id)} for row in rows]
    return {"users": users, "total_tokens": sum(u["used_tokens"] for u in users),
            "total_cost_microusd": sum(u["total_cost_microusd"] for u in users)}


@router.get("/admin/usage/requests")
async def admin_request_usage(admin: Admin, db: Db, state: str | None = None, user_id: str | None = None):
    stmt = select(RequestRecord)
    if state:
        stmt = stmt.where(RequestRecord.state == state)
    if user_id:
        stmt = stmt.where(RequestRecord.user_id == user_id)
    return [request_view(row) for row in (await db.scalars(stmt.order_by(RequestRecord.created_at.desc()).limit(500))).all()]


@router.get("/admin/usage/export")
async def admin_export(admin: Admin, db: Db):
    rows = (await db.scalars(select(RequestRecord).order_by(RequestRecord.created_at.desc()).limit(10000))).all()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    names = "id user_id job_id model_id source state reserved_tokens total_tokens cost_microusd created_at".split()
    writer.writerow(names)
    for row in rows:
        # Prevent spreadsheet formula injection from configurable model aliases.
        values = [str(getattr(row, name) or "") for name in names]
        writer.writerow(["'" + value if value.startswith(("=", "+", "-", "@", "\t", "\r")) else value for value in values])
    audit(db, admin.id, "usage.export", {"rows": len(rows)})
    await db.commit()
    return Response("\ufeff" + buffer.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=usage.csv", "Cache-Control": "no-store"})


@router.post("/admin/requests/{item_id}/reconcile")
async def reconcile(item_id: str, body: Reconcile, admin: Admin, db: Db):
    row = await db.get(RequestRecord, item_id)
    if row is None:
        fail("not_found", "请求不存在", 404)
    if row.state != "unknown":
        fail("not_unknown", "只能核对未知用量请求", 409)
    audit_context = {"actor_user_id": admin.id, "reason": body.reason, "action": "usage.reconcile"}
    if row.source == "image":
        from app.cloud.image_metering import finish
        if body.action == "settle" and (body.usage or {}).get("count") != 1:
            fail("image_count_required", "请将已确认图片数量填写为 1")
        result = await finish(db, item_id, release=body.action == "release", allow_unknown=True, audit_context=audit_context)
    elif body.action == "release":
        result = await metering.release(db, item_id, allow_unknown=True, audit_context=audit_context)
    else:
        if not body.usage:
            fail("usage_required", "请输入核对后的用量")
        result = await metering.settle(db, item_id, body.usage, audit_context=audit_context)
    return request_view(result)


@router.get("/admin/models")
async def admin_models(admin: Admin, db: Db):
    return [model_view(row, True) for row in (await db.scalars(select(ModelRoute).order_by(ModelRoute.name))).all()]


async def save_model(body: RouteCreate, request: Request, admin: User, db: AsyncSession, exists: bool, *, private: bool = False):
    from app.cloud.custom_headers import encode_headers, supports_custom_headers
    if body.custom_headers and not supports_custom_headers(body.provider, body.kind):
        fail("custom_headers_unsupported", "自定义请求头仅适用于自定义或OpenAI兼容聊天接口")
    if body.allow_unauthenticated and (body.kind != "chat" or not body.provider.startswith("custom_")):
        fail("unauthenticated_unsupported", "无需认证仅适用于自定义聊天接口")
    if body.kind == "image" and (body.provider != "siliconflow" or body.model != "Kwai-Kolors/Kolors" or body.base_url not in (None, "", "https://api.siliconflow.cn/v1")):
        fail("invalid_image_model", "图片模型需为 SiliconFlow Kwai-Kolors/Kolors")
    if private and (body.api_key_env or body.provider == "mock"):
        fail("invalid_private_credential", "个人模型请填写自己的 API Key，不能引用服务端环境变量")
    if body.provider == "mock" and not request.app.state.settings.allow_mock_provider:
        fail("mock_disabled", "生产环境不允许测试供应商")
    if body.strict_quota and body.provider != "mock":
        fail("strict_quota_unavailable", "真实模型暂不支持已证明的严格输入上界")
    from app.provider.catalog import PROVIDER_CATALOG
    if body.provider not in {"mock", "openai"} and body.provider not in PROVIDER_CATALOG and not body.provider.startswith("custom_"):
        fail("invalid_provider", "不支持的模型供应商")
    row = await db.scalar(select(ModelRoute).where(ModelRoute.id == body.id).with_for_update())
    if private and row is not None and row.owner_user_id != admin.id:
        fail("not_found", "个人模型不存在", 404)
    if not private and row is not None and row.owner_user_id is not None:
        fail("private_model", "个人模型由拥有者管理", 403)
    if bool(row) != exists:
        fail("model_conflict", "模型不存在" if exists else "模型标识已存在", 404 if exists else 409)
    secret = body.api_key.get_secret_value().strip() if body.api_key is not None else ""
    if secret and (len(secret) > 8192 or secret.startswith("suxiaoyou-credential://")
                   or any(char.isspace() or ord(char) < 32 for char in secret)):
        fail("invalid_api_key", "API Key 格式不正确")
    if secret and body.clear_api_key:
        fail("invalid_api_key", "替换密钥与删除密钥不能同时执行")
    if secret and body.provider == "mock":
        fail("invalid_api_key", "测试供应商不接受真实 API Key")
    encrypted = row.api_key_encrypted if row is not None else None
    headers_encrypted = row.custom_headers_encrypted if row is not None else None
    if secret:
        from app.cloud.secrets import ModelSecretVault
        try:
            encrypted = ModelSecretVault(request.app.state.settings).seal(secret)
        except Exception:
            # Never include cryptographic exceptions, request bodies or key
            # material in a response or audit event.
            fail("credential_store_unavailable", "服务端密钥存储不可用，请联系管理员检查主密钥和目录权限", 503)
    elif body.clear_api_key:
        encrypted = None
    if body.custom_headers is not None:
        if body.custom_headers:
            from app.cloud.secrets import ModelSecretVault
            try:
                headers_encrypted = ModelSecretVault(request.app.state.settings).seal(encode_headers(
                    {name: value.get_secret_value() for name, value in body.custom_headers.items()}))
            except Exception:
                fail("credential_store_unavailable", "服务端密钥存储不可用，请联系管理员检查主密钥和目录权限", 503)
        else:
            headers_encrypted = None
    if headers_encrypted and not supports_custom_headers(body.provider, body.kind):
        fail("custom_headers_unsupported", "请先清除自定义请求头再切换供应商")
    if body.allow_unauthenticated and (encrypted or body.api_key_env or headers_encrypted):
        fail("unauthenticated_credentials_conflict", "选择无需认证前请明确清除API Key、环境变量与自定义请求头")
    if body.provider != "mock" and not encrypted and not body.api_key_env and not headers_encrypted and not body.allow_unauthenticated and body.enabled:
        fail("key_env_required", "请配置API Key、自定义请求头或明确选择无需认证；未配置的模型请先停用")
    values = body.model_dump(exclude={"api_key", "clear_api_key", "custom_headers"})
    if row is None:
        row = ModelRoute(**values, owner_user_id=admin.id if private else None, api_key_encrypted=encrypted,
            custom_headers_encrypted=headers_encrypted, credential_version=1 if encrypted or headers_encrypted or body.allow_unauthenticated else 0)
        db.add(row)
    else:
        if (encrypted != row.api_key_encrypted or headers_encrypted != row.custom_headers_encrypted
                or body.allow_unauthenticated != row.allow_unauthenticated):
            row.credential_version += 1
        for key, value in values.items():
            setattr(row, key, value)
        row.api_key_encrypted = encrypted
        row.custom_headers_encrypted = headers_encrypted
    if secret or body.clear_api_key:
        audit(db, admin.id, "model.credential_replaced" if secret else "model.credential_cleared",
              {"model_id": body.id, "credential_version": row.credential_version})
    if body.custom_headers is not None:
        audit(db, admin.id, "model.headers_replaced" if body.custom_headers else "model.headers_cleared",
              {"model_id": body.id, "credential_version": row.credential_version})
    audit(db, admin.id, "model.update" if exists else "model.create", {"model_id": body.id})
    await db.commit()
    return model_view(row, True)


@router.post("/admin/models", status_code=201)
async def create_model(body: RouteCreate, request: Request, admin: Admin, db: Db):
    return await save_model(body, request, admin, db, False)


@router.put("/admin/models/{item_id}")
async def update_model(item_id: str, body: RouteCreate, request: Request, admin: Admin, db: Db):
    if item_id != body.id:
        fail("invalid_model_id", "不能修改模型标识")
    return await save_model(body, request, admin, db, True)


@router.get("/admin/audit")
async def admin_audit(admin: Admin, db: Db):
    return [fields(row, "id actor_user_id target_user_id action details created_at") for row in
            (await db.scalars(select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(300))).all()]


@router.get("/user/models")
async def private_models(user: CurrentUser, db: Db):
    return [model_view(row, True) for row in (await db.scalars(select(ModelRoute).where(ModelRoute.owner_user_id == user.id).order_by(ModelRoute.name))).all()]


@router.post("/user/models", status_code=201)
async def create_private_model(body: RouteCreate, request: Request, user: CurrentUser, db: Db):
    # Global IDs are namespaced by authenticated user, never request owner data.
    body = body.model_copy(update={"id": "user." + user.id + "." + body.id[:60]})
    return await save_model(body, request, user, db, False, private=True)


@router.put("/user/models/{item_id}")
async def update_private_model(item_id: str, body: RouteCreate, request: Request, user: CurrentUser, db: Db):
    if item_id != body.id:
        fail("invalid_model_id", "不能修改模型标识")
    return await save_model(body, request, user, db, True, private=True)


@router.delete("/user/models/{item_id}")
async def disable_private_model(item_id: str, user: CurrentUser, db: Db):
    row = await db.scalar(select(ModelRoute).where(ModelRoute.id == item_id, ModelRoute.owner_user_id == user.id).with_for_update())
    if row is None:
        fail("not_found", "个人模型不存在", 404)
    # Retain route and usage identities; erasure revokes future credential use.
    row.enabled, row.api_key_encrypted, row.custom_headers_encrypted = False, None, None
    row.credential_version += 1
    audit(db, user.id, "model.private_deleted", {"model_id": row.id})
    await db.commit()
    return {"ok": True}
