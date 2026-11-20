"""Account for user-owned subscriptions and local inference nodes.

These providers execute in the private engine using the user's own credential
or hardware. Receipts are explicitly runtime-reported; this API cannot settle
or release server-paid gateway requests. Shared and BYOK API routes instead
execute at the central inference gateway with server-observed receipts.
"""
from datetime import timedelta
import hashlib
import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update

from app.cloud import metering
from app.cloud.database import get_db
from app.cloud.inference import ChatCompletionRequest
from app.cloud.models import RequestRecord, User, utcnow
from app.cloud.providers import input_reservation
from app.cloud.runtime_identity import get_runtime_principal

router = APIRouter(prefix="/api/cloud/internal/tenant-provider")
SOURCE = "tenant-provider"


class Admission(ChatCompletionRequest):
    provider_id: Literal["openai-subscription", "ollama", "rapid-mlx"]


class Receipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=32, max_length=32)
    usage: dict | None = None


async def _principal(request, db):
    user = await get_runtime_principal(request, db)
    capability = hashlib.sha256(request.headers["authorization"][7:].strip().encode()).hexdigest()
    return user, capability


@router.post("/admit")
async def admit(body: Admission, request: Request, db=Depends(get_db)):
    user, capability = await _principal(request, db)
    maximum = (await db.execute(update(User).where(User.id == user.id, User.is_active.is_(True))
        .values(token_limit=User.token_limit).returning(User.max_concurrent_jobs))).scalar_one_or_none()
    if maximum is None:
        raise HTTPException(403, detail="账号已停用")
    active = await db.scalar(select(func.count()).select_from(RequestRecord).where(
        RequestRecord.user_id == user.id, RequestRecord.state.in_(("reserved", "dispatched"))))
    if active >= maximum:
        raise metering.QuotaError("concurrency_exceeded", "模型并发请求已达上限", 429)
    amount = input_reservation(body.messages, system=body.system, tools=body.tools,
                               max_output_tokens=body.output_limit)
    model_id = f"tenant/{body.provider_id}/{body.model}"
    if len(model_id) > 120:
        model_id = model_id[:85] + "/" + hashlib.sha256(model_id.encode()).hexdigest()[:32]
    record = await metering.reserve(db, user.id, model_id, None, amount, source=SOURCE,
        lease_owner=capability, lease_until=utcnow() + timedelta(seconds=60))
    # Commit permission to send before replying. If the engine disconnects
    # before seeing this result, recovery conservatively retains the quota.
    await metering.mark_dispatched(db, record.id)
    return {"request_id": record.id, "lease_seconds": 60}


async def _owned(body, request, db):
    user, capability = await _principal(request, db)
    record = await db.scalar(select(RequestRecord).where(RequestRecord.id == body.request_id,
        RequestRecord.user_id == user.id, RequestRecord.source == SOURCE,
        RequestRecord.lease_owner == capability))
    if record is None:
        raise HTTPException(404, detail="用量请求不存在")
    return record


@router.post("/heartbeat")
async def heartbeat(body: Receipt, request: Request, db=Depends(get_db)):
    record = await _owned(body, request, db)
    result = await db.execute(update(RequestRecord).where(RequestRecord.id == record.id,
        RequestRecord.source == SOURCE, RequestRecord.lease_owner == record.lease_owner,
        RequestRecord.lease_until > utcnow(), RequestRecord.state == "dispatched").values(
            lease_until=utcnow() + timedelta(seconds=60)).execution_options(synchronize_session=False))
    await db.commit()
    if not result.rowcount:
        raise HTTPException(409, detail="模型调用租约已失效")
    return {"status": "active"}


@router.post("/finish")
async def finish(body: Receipt, request: Request, db=Depends(get_db)):
    record = await _owned(body, request, db)
    if body.usage is not None and body.usage.get("usage_status", "complete") == "complete":
        # Mark provenance without claiming a cloud provider observed this
        # private subscription/local-node receipt or has a known USD price.
        usage = dict(body.usage)
        usage["raw_usage"] = {"receipt_source": "tenant_runtime", "provider_receipt":
            usage.get("raw_usage", dict(usage))}
        record = await metering.settle(db, record.id, usage)
    elif record.state == "dispatched":
        record = await metering.mark_unknown(db, record.id, "用户自有模型未返回完整用量")
    return {"status": record.state}
