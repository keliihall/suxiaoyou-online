"""Durable per-attempt accounting, independent of deletable chat content.

Mutations own their transaction and commit before returning. Admission starts
with a write, rather than a read-then-write transaction: this also serializes
SQLite writers without a deferred-transaction lock upgrade. No process-local
lock is part of the quota correctness contract.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.cloud.models import (
    AuditEvent, ModelRoute, QuotaBucket, RequestRecord, Reservation, UsageEvent, User, new_id, utcnow,
)

_BILLING_ZONE = ZoneInfo("Asia/Shanghai")
_MAX_COUNTER = 2**63 - 1


class QuotaError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 409):
        super().__init__(message)
        self.code, self.message, self.status_code = code, message, status_code


def billing_period(now: datetime | None = None) -> str:
    """The admission month, not the later settlement month."""
    instant = now or utcnow()
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(_BILLING_ZONE).strftime("%Y-%m")


def _counter(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_COUNTER:
        raise QuotaError("invalid_usage", f"{field} must be a non-negative 64-bit integer", 400)
    return value


def _validated_usage(usage: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(usage, Mapping):
        raise QuotaError("invalid_usage", "Usage must be an object", 400)
    if "input_total" not in usage or "output_total" not in usage:
        raise QuotaError("invalid_usage", "Complete input_total and output_total are required", 400)
    if usage.get("usage_status", "complete") not in {"complete", "reconciled", "estimated"}:
        raise QuotaError("incomplete_usage", "Partial or unknown usage cannot release a reservation", 409)
    fields = ("input_total", "output_total", "cache_read", "cache_write", "reasoning")
    normalized = {key: _counter(usage.get(key, 0), key) for key in fields}
    total = normalized["input_total"] + normalized["output_total"]
    _counter(total, "total_tokens")
    if "total_tokens" in usage and _counter(usage["total_tokens"], "total_tokens") != total:
        raise QuotaError("invalid_usage", "total_tokens must equal input_total + output_total", 400)
    if normalized["cache_read"] + normalized["cache_write"] > normalized["input_total"]:
        raise QuotaError("invalid_usage", "Cache tokens cannot exceed total input", 400)
    if normalized["reasoning"] > normalized["output_total"]:
        raise QuotaError("invalid_usage", "Reasoning tokens cannot exceed total output", 400)
    normalized.update(total_tokens=total, usage_status=usage.get("usage_status", "complete"))
    # Preserve diagnostic metadata, but compare only one well-defined numeric
    # charge on idempotent settlement. Never add cumulative stream snapshots.
    for key in ("normalization_version", "provider_total", "input_uncached", "output_visible"):
        if key in usage:
            normalized[key] = usage[key]
    raw = usage.get("raw_usage", dict(usage))
    if not isinstance(raw, dict):
        raise QuotaError("invalid_usage", "raw_usage must be an object", 400)
    return normalized, raw


async def _request(db: AsyncSession, request_id: str) -> RequestRecord:
    result = await db.execute(
        select(RequestRecord).where(RequestRecord.id == request_id)
        .execution_options(populate_existing=True)
    )
    record = result.scalar_one_or_none()
    if record is None:
        raise QuotaError("request_not_found", "Model request not found", 404)
    return record


async def _price_snapshot(
    db: AsyncSession, model_id: str, expected_route: ModelRoute | None = None,
) -> dict[str, Any]:
    """Capture only non-secret server configuration, before admission commits."""
    selected_fields = ("id", "provider", "model", "enabled", "strict_quota", "base_url", "api_key_env", "credential_version",
                       "input_price_microusd_per_million", "output_price_microusd_per_million")
    selected = {name: getattr(expected_route, name) for name in selected_fields} if expected_route is not None else None
    route = (await db.execute(select(ModelRoute).where(ModelRoute.id == model_id)
                              .execution_options(populate_existing=True))).scalar_one_or_none()
    if expected_route is not None:
        # A worker prepared its provider from a detached route. If an admin
        # edited that route while documents were parsed, do not pair the old
        # actual provider/model with a new price snapshot or disabled alias.
        if (route is None or not route.enabled or
                any(getattr(route, name) != selected[name] for name in selected_fields)):
            raise QuotaError("route_changed", "Model configuration changed before dispatch; submit a new request", 409)
    snapshot: dict[str, Any] = {
        "version": 1, "status": "unknown", "route_id": model_id, "currency": "USD",
        "calculation": "input_output_total_ceil_microusd_v1",
    }
    if route is not None:
        snapshot.update(
            status="route_configured", provider=route.provider, model=route.model,
            credential_version=_counter(route.credential_version, "credential_version"),
            input_price_microusd_per_million=_counter(route.input_price_microusd_per_million, "input_price"),
            output_price_microusd_per_million=_counter(route.output_price_microusd_per_million, "output_price"),
        )
    return snapshot


def _settlement_cost(
    snapshot: Mapping[str, Any], usage: Mapping[str, Any], supplied: int | None,
) -> tuple[int, str]:
    if supplied is not None:
        _counter(supplied, "cost_microusd")
    if not snapshot or snapshot.get("status") == "unknown":
        # Internal callers may provide an independently verified amount, but
        # the HTTP reconciliation API accepts only usage and never this value.
        # A numeric zero with unknown status is not a claim of free usage.
        return (supplied, "provided") if supplied is not None else (0, "unknown")
    if (snapshot.get("status") != "route_configured" or snapshot.get("version") != 1 or
            snapshot.get("calculation") != "input_output_total_ceil_microusd_v1"):
        raise QuotaError("invalid_pricing", "Unknown historical pricing calculation", 409)
    inputs = _counter(snapshot.get("input_price_microusd_per_million"), "input_price")
    outputs = _counter(snapshot.get("output_price_microusd_per_million"), "output_price")
    cost = (usage["input_total"] * inputs + usage["output_total"] * outputs + 999999) // 1000000
    _counter(cost, "cost_microusd")
    if supplied is not None and supplied != cost:
        raise QuotaError("cost_conflict", "Cost must match the request's historical server pricing", 409)
    return cost, "calculated"


async def reserve(
    db: AsyncSession, user_id: str, model_id: str, job_id: str | None, amount: int,
    *, expected_route: ModelRoute | None = None, source: str = "chat",
    lease_owner: str | None = None, lease_until: datetime | None = None,
) -> RequestRecord:
    """Reserve a single model attempt atomically against the current month.

    A real provider uses an estimate: this prevents concurrent admission from
    oversubscribing the estimate, not unbounded provider consumption. If actual
    usage exceeds it, settlement records the truth and blocks later admissions.
    """
    _counter(amount, "amount")
    if amount == 0:
        raise QuotaError("invalid_reservation", "Reservation must be positive", 400)
    try:
        # Lock the user row first (SQLite's first statement acquires the write
        # lock). This also linearizes quota changes and concurrent bucket creation.
        limit = (await db.execute(
            update(User).where(User.id == user_id, User.is_active.is_(True))
            .values(token_limit=User.token_limit).returning(User.token_limit)
        )).scalar_one_or_none()
        if limit is None:
            raise QuotaError("account_disabled", "Account is unavailable or disabled", 403)
        _counter(limit, "token_limit")
        period = billing_period()
        bucket = (await db.execute(select(QuotaBucket).where(
            QuotaBucket.user_id == user_id, QuotaBucket.period == period,
        ))).scalar_one_or_none()
        if bucket is None:
            bucket = QuotaBucket(id=new_id(), user_id=user_id, period=period,
                                 limit_tokens=limit, used_tokens=0, reserved_tokens=0)
            db.add(bucket)
            await db.flush()
        # The subtraction form avoids overflow of used + reserved + amount.
        admitted = await db.execute(
            update(QuotaBucket).where(
                QuotaBucket.id == bucket.id,
                QuotaBucket.used_tokens <= limit,
                QuotaBucket.reserved_tokens <= limit - QuotaBucket.used_tokens,
                amount <= limit - QuotaBucket.used_tokens - QuotaBucket.reserved_tokens,
            ).values(limit_tokens=limit,
                     reserved_tokens=QuotaBucket.reserved_tokens + amount,
                     revision=QuotaBucket.revision + 1)
            .execution_options(synchronize_session=False)
        )
        if admitted.rowcount != 1:
            raise QuotaError("quota_exceeded", "Monthly token quota is exhausted", 429)
        price_snapshot = await _price_snapshot(db, model_id, expected_route)
        record = RequestRecord(id=new_id(), user_id=user_id, model_id=model_id,
                               job_id=job_id, source=source, lease_owner=lease_owner, lease_until=lease_until,
                               state="reserved", reserved_tokens=amount,
                               price_snapshot=price_snapshot, created_at=utcnow())
        db.add(record)
        await db.flush()
        db.add(Reservation(request_id=record.id, bucket_id=bucket.id,
                           amount=amount, state="reserved"))
        await db.commit()
        return record
    except BaseException:
        await db.rollback()
        raise


async def mark_dispatched(db: AsyncSession, request_id: str) -> bool:
    """Only True grants permission to send. Replays must never resend."""
    try:
        result = await db.execute(update(RequestRecord).where(
            RequestRecord.id == request_id, RequestRecord.state == "reserved",
        ).values(state="dispatched", dispatched_at=utcnow())
          .execution_options(synchronize_session=False))
        if result.rowcount == 0:
            await _request(db, request_id)
        await db.commit()
        return result.rowcount == 1
    except BaseException:
        await db.rollback()
        raise


async def mark_unknown(db: AsyncSession, request_id: str, error: str = "") -> RequestRecord:
    """Retain the entire unconfirmed reservation; expiry never refunds it."""
    try:
        result = await db.execute(update(RequestRecord).where(
            RequestRecord.id == request_id,
            RequestRecord.state.in_(("dispatched", "unknown")),
        ).values(state="unknown", error=str(error)[:2000])
          .execution_options(synchronize_session=False))
        record = await _request(db, request_id)
        if result.rowcount:
            await db.execute(update(Reservation).where(
                Reservation.request_id == request_id, Reservation.state == "reserved",
            ).values(state="unknown"))
        elif record.state not in {"settled", "released"}:
            raise QuotaError("request_conflict", "Only dispatched requests can have unknown usage")
        await db.commit()
        return record
    except BaseException:
        await db.rollback()
        raise


async def _apply_reservations(
    db: AsyncSession, request_id: str, *, final_state: str, actual_tokens: int = 0,
) -> None:
    reservations = (await db.execute(select(Reservation).where(
        Reservation.request_id == request_id,
        Reservation.state.in_(("reserved", "unknown")),
    ).order_by(Reservation.bucket_id))).scalars().all()
    if not reservations:
        raise QuotaError("ledger_conflict", "Request has no outstanding reservation")
    for reservation in reservations:
        result = await db.execute(update(QuotaBucket).where(
            QuotaBucket.id == reservation.bucket_id,
            QuotaBucket.reserved_tokens >= reservation.amount,
            QuotaBucket.used_tokens <= _MAX_COUNTER - actual_tokens,
        ).values(reserved_tokens=QuotaBucket.reserved_tokens - reservation.amount,
                 used_tokens=QuotaBucket.used_tokens + actual_tokens,
                 revision=QuotaBucket.revision + 1)
          .execution_options(synchronize_session=False))
        if result.rowcount != 1:
            raise QuotaError("ledger_conflict", "Reservation and bucket counters disagree")
        reservation.state = final_state


async def _reconciliation_audit(
    db: AsyncSession, record: RequestRecord, operation: str, context: Mapping[str, Any] | None,
) -> None:
    if context is None:
        return
    if not isinstance(context, Mapping) or context.get("action", "usage.reconcile") != "usage.reconcile":
        raise QuotaError("invalid_audit", "Reconciliation audit action is invalid", 400)
    actor, reason = context.get("actor_user_id"), context.get("reason")
    if not isinstance(actor, str) or not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise QuotaError("invalid_audit", "Reconciliation requires an administrator and reason", 400)
    # Check the administrator under a row lock, before touching bucket counters.
    # Authorization and revocation therefore linearize with the actual change.
    authorized = await db.execute(update(User).where(
        User.id == actor, User.is_active.is_(True), User.role == "admin",
    ).values(token_limit=User.token_limit).returning(User.id))
    if authorized.scalar_one_or_none() is None:
        raise QuotaError("forbidden", "An active administrator is required", 403)
    db.add(AuditEvent(actor_user_id=actor, target_user_id=record.user_id,
                      action="usage.reconcile", details={
                          "request_id": record.id, "action": operation,
                          "reason": reason.strip(), "total_tokens": record.total_tokens,
                          "cost_microusd": record.cost_microusd,
                          "cost_status": record.normalized_usage.get("cost_status", "unknown"),
                      }))


async def settle(
    db: AsyncSession, request_id: str, usage: Mapping[str, Any], cost_microusd: int | None = None,
    *, audit_context: Mapping[str, Any] | None = None,
) -> RequestRecord:
    """Settle once, including over-estimate actual usage and delayed old months."""
    normalized, raw = _validated_usage(usage)
    try:
        # Write first, including on SQLite: serialize replays and read the
        # immutable snapshot under the same lock as all later ledger changes.
        await db.execute(update(RequestRecord).where(RequestRecord.id == request_id)
                         .values(state=RequestRecord.state)
                         .execution_options(synchronize_session=False))
        record = await _request(db, request_id)
        cost_microusd, cost_status = _settlement_cost(record.price_snapshot, normalized, cost_microusd)
        normalized["cost_status"] = cost_status
        result = await db.execute(update(RequestRecord).where(
            RequestRecord.id == request_id,
            RequestRecord.state.in_(("dispatched", "unknown")),
        ).values(state="settled", total_tokens=normalized["total_tokens"],
                 normalized_usage=normalized, raw_usage=raw, cost_microusd=cost_microusd,
                 settled_at=utcnow(), error="")
          .execution_options(synchronize_session=False))
        record = await _request(db, request_id)
        if result.rowcount == 0:
            if record.state == "settled" and record.normalized_usage == normalized and record.cost_microusd == cost_microusd:
                await db.commit()
                return record
            raise QuotaError("settlement_conflict", "Request is not dispatchable or was settled with different usage")
        await _reconciliation_audit(db, record, "settle", audit_context)
        await _apply_reservations(db, request_id, final_state="settled",
                                  actual_tokens=normalized["total_tokens"])
        db.add(UsageEvent(request_id=record.id, user_id=record.user_id,
                         model_id=record.model_id, input_tokens=normalized["input_total"],
                         output_tokens=normalized["output_total"],
                         cache_read=normalized["cache_read"], cache_write=normalized["cache_write"],
                         reasoning=normalized["reasoning"], total_tokens=normalized["total_tokens"],
                         cost_microusd=cost_microusd, raw_usage=raw))
        await db.commit()
        return record
    except BaseException:
        await db.rollback()
        raise


async def release(
    db: AsyncSession, request_id: str, *, allow_unknown: bool = False,
    audit_context: Mapping[str, Any] | None = None,
) -> RequestRecord:
    """Refund only unsent work, or an explicitly authorized admin reconciliation.

    The caller granting allow_unknown must authorize an administrator and record
    the reason in AuditEvent. A dispatched request can never be released here.
    """
    try:
        states = ("reserved", "unknown") if allow_unknown else ("reserved",)
        result = await db.execute(update(RequestRecord).where(
            RequestRecord.id == request_id, RequestRecord.state.in_(states),
        ).values(state="released", settled_at=utcnow())
          .execution_options(synchronize_session=False))
        record = await _request(db, request_id)
        if result.rowcount:
            await _reconciliation_audit(db, record, "release", audit_context)
            await _apply_reservations(db, request_id, final_state="released")
        elif record.state != "released":
            raise QuotaError("release_conflict", "Dispatched or unknown requests require reconciliation")
        await db.commit()
        return record
    except BaseException:
        await db.rollback()
        raise


async def usage_summary(db: AsyncSession, user_id: str) -> dict[str, Any]:
    period = billing_period()
    user = (await db.execute(select(User).where(User.id == user_id)
                            .execution_options(populate_existing=True))).scalar_one_or_none()
    if user is None:
        raise QuotaError("account_not_found", "Account not found", 404)
    bucket = (await db.execute(select(QuotaBucket).where(
        QuotaBucket.user_id == user_id, QuotaBucket.period == period,
    ).execution_options(populate_existing=True))).scalar_one_or_none()
    used, reserved = (bucket.used_tokens, bucket.reserved_tokens) if bucket else (0, 0)
    request_ids = select(Reservation.request_id).where(
        Reservation.bucket_id == (bucket.id if bucket else ""),
    )
    unknown = (await db.execute(select(func.count()).select_from(RequestRecord).where(
        RequestRecord.user_id == user_id, RequestRecord.id.in_(request_ids),
        RequestRecord.state == "unknown",
    ))).scalar_one()
    by_model = (await db.execute(select(
        UsageEvent.model_id, func.sum(UsageEvent.total_tokens), func.sum(UsageEvent.cost_microusd),
        func.sum(case((RequestRecord.normalized_usage["cost_status"].as_string().in_(("calculated", "provided")), 0), else_=1)),
    ).join(RequestRecord, RequestRecord.id == UsageEvent.request_id)
      .where(UsageEvent.user_id == user_id, UsageEvent.request_id.in_(request_ids))
      .group_by(UsageEvent.model_id).order_by(UsageEvent.model_id))).all()
    # Keep daily attribution in the same calendar zone as the monthly bucket.
    # UTC midnight would otherwise display January 31 in a February bucket.
    if db.get_bind().dialect.name == "postgresql":
        billing_day = func.date(func.timezone("Asia/Shanghai", RequestRecord.created_at))
    else:
        billing_day = func.date(RequestRecord.created_at, "+8 hours")
    daily = (await db.execute(select(
        billing_day, func.sum(UsageEvent.total_tokens),
    ).join(RequestRecord, RequestRecord.id == UsageEvent.request_id).where(
        UsageEvent.user_id == user_id, UsageEvent.request_id.in_(request_ids),
    ).group_by(billing_day).order_by(billing_day))).all()
    from app.cloud.image_metering import summary as image_summary
    image = await image_summary(db, user_id)
    return {
        "image": image,
        "period": period, "limit_tokens": user.token_limit, "used_tokens": used,
        "reserved_tokens": reserved, "remaining_tokens": max(0, user.token_limit - used - reserved),
        "unknown_requests": unknown,
        "unpriced_requests": sum(int(row[3]) for row in by_model),
        "total_cost_microusd": sum(int(row[2]) for row in by_model),
        "by_model": [{"model_id": row[0], "total_tokens": int(row[1]),
                      "cost_microusd": int(row[2]), "unpriced_requests": int(row[3])} for row in by_model],
        "daily": [{"date": str(row[0]), "total_tokens": int(row[1])} for row in daily],
    }
