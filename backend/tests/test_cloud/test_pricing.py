"""Historical server pricing and reconciliation; never calls model networks."""

import asyncio

import pytest
from sqlalchemy import delete, event, func, select, update

from app.cloud import metering
from app.cloud.models import AuditEvent, ModelRoute, RequestRecord, UsageEvent, User
from tests.test_cloud.test_metering import ledger, admitted, usage


async def priced_attempt(ledger, *, input_price=1000000, output_price=2000000):
    async with ledger() as db:
        db.add(ModelRoute(id="priced", name="Original", provider="mock", model="original-model",
                          api_key_env="SERVER_SECRET_NAME", base_url="https://example.test/secret",
                          input_price_microusd_per_million=input_price,
                          output_price_microusd_per_million=output_price))
        await db.commit()
    return await admitted(ledger, model="priced")


async def test_unknown_reconciliation_keeps_original_price_and_model_after_route_edit(ledger):
    record = await priced_attempt(ledger)
    assert record.price_snapshot == {
        "version": 1, "status": "route_configured", "route_id": "priced", "currency": "USD",
        "calculation": "input_output_total_ceil_microusd_v1", "provider": "mock", "model": "original-model", "credential_version": 0,
        "input_price_microusd_per_million": 1000000, "output_price_microusd_per_million": 2000000,
    }
    async with ledger() as db:
        await metering.mark_unknown(db, record.id, "Receipt delayed")
        await db.execute(update(ModelRoute).where(ModelRoute.id == "priced").values(
            model="replacement-model", input_price_microusd_per_million=999000000,
            output_price_microusd_per_million=999000000))
        await db.execute(update(User).where(User.id == "user-b").values(role="admin"))
        await db.commit()
        context = {"actor_user_id": "user-b", "reason": "Provider receipt checked"}
        settled = await metering.settle(db, record.id, usage(), audit_context=context)
        assert settled.cost_microusd == 320
        assert settled.normalized_usage["cost_status"] == "calculated"
        assert settled.price_snapshot == record.price_snapshot
        # Route deletion cannot destroy or change the historical charge either.
        await db.execute(delete(ModelRoute).where(ModelRoute.id == "priced"))
        await db.commit()
        await metering.settle(db, record.id, usage(), audit_context=context)
        audit = (await db.scalars(select(AuditEvent))).one()
        assert audit.details["cost_microusd"] == 320
        assert audit.details["cost_status"] == "calculated"
        assert (await metering.usage_summary(db, "user-a"))["unpriced_requests"] == 0


async def test_client_usage_and_explicit_cost_cannot_override_configured_server_price(ledger):
    record = await priced_attempt(ledger)
    async with ledger() as db:
        with pytest.raises(metering.QuotaError) as exc:
            await metering.settle(db, record.id, usage(), cost_microusd=0)
        assert exc.value.code == "cost_conflict"
        assert (await db.get(RequestRecord, record.id)).state == "dispatched"
        assert (await metering.usage_summary(db, "user-a"))["reserved_tokens"] == 300
        settled = await metering.settle(db, record.id, usage() | {
            "cost_microusd": 0, "input_price_microusd_per_million": 0, "cost_status": "free",
        })
        assert settled.cost_microusd == 320
        assert settled.normalized_usage["cost_status"] == "calculated"


async def test_unpriced_usage_is_distinct_from_configured_free_route(ledger):
    unknown = await admitted(ledger)
    free = await priced_attempt(ledger, input_price=0, output_price=0)
    async with ledger() as db:
        unknown = await metering.settle(db, unknown.id, usage())
        free = await metering.settle(db, free.id, usage())
        assert unknown.cost_microusd == free.cost_microusd == 0
        assert unknown.price_snapshot["status"] == "unknown"
        assert unknown.normalized_usage["cost_status"] == "unknown"
        assert free.normalized_usage["cost_status"] == "calculated"
        summary = await metering.usage_summary(db, "user-a")
        assert summary["unpriced_requests"] == 1
        assert {row["model_id"]: row["unpriced_requests"] for row in summary["by_model"]} == {
            "model-a": 1, "priced": 0,
        }


async def test_priced_parallel_settlement_is_idempotent_with_and_without_matching_cost(ledger):
    record = await priced_attempt(ledger, input_price=1, output_price=1)
    async def settle(explicit):
        async with ledger() as db:
            return await metering.settle(db, record.id, usage(), cost_microusd=1 if explicit else None)
    records = await asyncio.gather(settle(False), settle(True), settle(False))
    assert [r.cost_microusd for r in records] == [1, 1, 1]  # round up once per complete request
    async with ledger() as db:
        assert await db.scalar(select(func.count()).select_from(UsageEvent)) == 1
        summary = await metering.usage_summary(db, "user-a")
        assert (summary["used_tokens"], summary["total_cost_microusd"]) == (220, 1)


async def test_priced_reconciliation_and_cost_roll_back_when_audit_fails(ledger):
    record = await priced_attempt(ledger)
    async with ledger() as db:
        await metering.mark_unknown(db, record.id)
        await db.execute(update(User).where(User.id == "user-b").values(role="admin"))
        await db.commit()
    def reject_audit(*_args):
        raise RuntimeError("Audit storage unavailable")
    event.listen(AuditEvent, "before_insert", reject_audit)
    try:
        async with ledger() as db:
            with pytest.raises(RuntimeError, match="Audit storage"):
                await metering.settle(db, record.id, usage(), audit_context={
                    "actor_user_id": "user-b", "reason": "Provider receipt checked",
                })
    finally:
        event.remove(AuditEvent, "before_insert", reject_audit)
    async with ledger() as db:
        unchanged = await db.get(RequestRecord, record.id)
        assert unchanged.state == "unknown" and unchanged.cost_microusd == 0
        assert unchanged.normalized_usage == {}
        assert await db.scalar(select(func.count()).select_from(UsageEvent)) == 0
        summary = await metering.usage_summary(db, "user-a")
        assert (summary["used_tokens"], summary["reserved_tokens"], summary["total_cost_microusd"]) == (0, 300, 0)


@pytest.mark.parametrize("change", ["model", "enabled", "api_key_env", "price", "credential_version"])
async def test_prepared_route_change_cannot_pair_old_provider_with_new_price(ledger, change):
    # Use a fresh route and detach the exact configuration selected by a worker.
    async with ledger() as db:
        db.add(ModelRoute(id="changed", name="Original", provider="mock", model="original"))
        await db.commit()
        selected = await db.get(ModelRoute, "changed")
    async with ledger() as db:
        changes = {"model": {"model": "replacement"}, "enabled": {"enabled": False},
                   "api_key_env": {"api_key_env": "OTHER_KEY"}, "price": {"input_price_microusd_per_million": 123},
                   "credential_version": {"credential_version": 1}}
        await db.execute(update(ModelRoute).where(ModelRoute.id == "changed").values(**changes[change]))
        await db.commit()
        with pytest.raises(metering.QuotaError) as exc:
            await metering.reserve(db, "user-a", "changed", None, 300, expected_route=selected)
        assert exc.value.code == "route_changed"
        assert await db.scalar(select(func.count()).select_from(RequestRecord)) == 0
        assert (await metering.usage_summary(db, "user-a"))["reserved_tokens"] == 0
