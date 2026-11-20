"""Atomic image-count quotas. Image operations never pretend to be Tokens."""
from sqlalchemy import func, select, update

from app.cloud import metering
from app.cloud.models import AuditEvent, ImageQuotaBucket, ImageReservation, ModelRoute, RequestRecord, UsageEvent, User, new_id, utcnow


async def reserve(db, user_id, route, operation_key, payload_sha256, *, lease_owner, lease_until):
    try:
        limit = (await db.execute(update(User).where(User.id == user_id, User.is_active.is_(True))
                    .values(image_limit=User.image_limit).returning(User.image_limit))).scalar_one_or_none()
        if limit is None:
            raise metering.QuotaError("account_disabled", "账号已停用", 403)
        maximum = await db.scalar(select(User.max_concurrent_jobs).where(User.id == user_id))
        active = await db.scalar(select(func.count()).select_from(RequestRecord).where(RequestRecord.user_id == user_id,
                            RequestRecord.state.in_(("reserved", "dispatched"))))
        if active >= maximum:
            raise metering.QuotaError("concurrency_exceeded", "模型并发请求已达上限", 429)
        existing = await db.scalar(select(ImageReservation).where(ImageReservation.user_id == user_id,
                                                                  ImageReservation.operation_key == operation_key))
        if existing:
            if existing.payload_sha256 != payload_sha256:
                raise metering.QuotaError("idempotency_conflict", "此图片操作标识已用于其他内容", 409)
            raise metering.QuotaError("image_already_submitted", "此图片请求已经提交，请核对原操作；不会自动重复生成", 409)
        current = await db.get(ModelRoute, route.id, populate_existing=True)
        if current is None or not current.enabled or current.kind != "image" or current.owner_user_id not in (None, user_id):
            raise metering.QuotaError("model_unavailable", "图片模型不可用", 404)
        if current.credential_version != route.credential_version or current.model != route.model or current.image_price_microusd != route.image_price_microusd:
            raise metering.QuotaError("route_changed", "图片配置已更新，请重新提交", 409)
        period = metering.billing_period()
        bucket = await db.scalar(select(ImageQuotaBucket).where(ImageQuotaBucket.user_id == user_id, ImageQuotaBucket.period == period))
        if bucket is None:
            bucket = ImageQuotaBucket(user_id=user_id, period=period, limit_images=limit, used_images=0, reserved_images=0)
            db.add(bucket)
            await db.flush()
        admitted = await db.execute(update(ImageQuotaBucket).where(ImageQuotaBucket.id == bucket.id,
                ImageQuotaBucket.used_images < limit,
                ImageQuotaBucket.reserved_images < limit - ImageQuotaBucket.used_images).values(
                    limit_images=limit, reserved_images=ImageQuotaBucket.reserved_images + 1,
                    revision=ImageQuotaBucket.revision + 1))
        if admitted.rowcount != 1:
            raise metering.QuotaError("image_quota_exceeded", "本月图片额度已用尽", 429)
        record = RequestRecord(user_id=user_id, model_id=route.id, job_id=None, source="image", state="reserved",
            reserved_tokens=0, lease_owner=lease_owner, lease_until=lease_until,
            price_snapshot={"version": 1, "unit": "image", "currency": "USD", "model": route.model,
                            "image_price_microusd": route.image_price_microusd})
        db.add(record)
        await db.flush()
        db.add(ImageReservation(request_id=record.id, bucket_id=bucket.id, user_id=user_id,
                                operation_key=operation_key, payload_sha256=payload_sha256, state="reserved"))
        await db.commit()
        return record
    except BaseException:
        await db.rollback()
        raise


async def finish(db, request_id, *, release=False, allow_unknown=False, audit_context=None):
    try:
        states = ("reserved", "unknown") if release and allow_unknown else (("reserved",) if release else ("dispatched", "unknown"))
        target = "released" if release else "settled"
        changed = await db.execute(update(RequestRecord).where(RequestRecord.id == request_id,
            RequestRecord.source == "image", RequestRecord.state.in_(states)).values(state=target, settled_at=utcnow()))
        record = await db.get(RequestRecord, request_id, populate_existing=True)
        if record is None:
            raise metering.QuotaError("request_not_found", "图片请求不存在", 404)
        if not changed.rowcount:
            if record.state == target:
                await db.commit()
                return record
            raise metering.QuotaError("request_conflict", "图片请求状态冲突", 409)
        reservation = await db.get(ImageReservation, request_id)
        if reservation is None or reservation.state != "reserved":
            raise metering.QuotaError("ledger_conflict", "图片预占状态不一致", 409)
        applied = await db.execute(update(ImageQuotaBucket).where(ImageQuotaBucket.id == reservation.bucket_id,
                ImageQuotaBucket.reserved_images >= 1).values(reserved_images=ImageQuotaBucket.reserved_images - 1,
                used_images=ImageQuotaBucket.used_images + (0 if release else 1), revision=ImageQuotaBucket.revision + 1))
        if applied.rowcount != 1:
            raise metering.QuotaError("ledger_conflict", "图片额度状态不一致", 409)
        reservation.state = target
        if not release:
            price = record.price_snapshot.get("image_price_microusd", 0)
            record.cost_microusd = price
            record.normalized_usage = {"unit": "image", "count": 1, "total_tokens": 0,
                "cost_status": "calculated" if price else "unknown", "usage_status": "complete"}
            record.raw_usage = {"unit": "image", "count": 1, "billing": "provider_receipt_unavailable"}
            db.add(UsageEvent(request_id=request_id, user_id=record.user_id, model_id=record.model_id,
                             input_tokens=0, output_tokens=0, cache_read=0, cache_write=0, reasoning=0,
                             total_tokens=0, cost_microusd=price, raw_usage=record.raw_usage))
        if audit_context:
            db.add(AuditEvent(actor_user_id=audit_context["actor_user_id"], target_user_id=record.user_id,
                action="image.reconcile", details={"request_id": request_id, "operation": target, "reason": audit_context["reason"]}))
        await db.commit()
        return record
    except BaseException:
        await db.rollback()
        raise


async def summary(db, user_id):
    period = metering.billing_period()
    user = await db.get(User, user_id)
    bucket = await db.scalar(select(ImageQuotaBucket).where(ImageQuotaBucket.user_id == user_id, ImageQuotaBucket.period == period))
    used, reserved = (bucket.used_images, bucket.reserved_images) if bucket else (0, 0)
    ids = select(ImageReservation.request_id).where(ImageReservation.bucket_id == (bucket.id if bucket else ""))
    unknown = await db.scalar(select(func.count()).select_from(RequestRecord).where(RequestRecord.id.in_(ids), RequestRecord.state == "unknown"))
    cost = await db.scalar(select(func.sum(RequestRecord.cost_microusd)).where(RequestRecord.id.in_(ids), RequestRecord.state == "settled"))
    return {"period": period, "limit": user.image_limit, "used": used, "reserved": reserved,
            "remaining": max(0, user.image_limit - used - reserved), "unknown_requests": unknown,
            "total_cost_microusd": int(cost or 0)}
