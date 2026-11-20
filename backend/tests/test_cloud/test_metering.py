"""Ledger tests use independent sessions and a real database, never model APIs."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os
import sys

import pytest
import pytest_asyncio
from sqlalchemy import delete, event, func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.cloud import metering
from app.cloud.models import (
    AuditEvent, CloudBase, Conversation, Job, Message, QuotaBucket, RequestRecord,
    Reservation, UsageEvent, User, Workspace, new_id,
)


@pytest_asyncio.fixture(params=["sqlite", "postgresql"])
async def ledger(request, tmp_path):
    schema = None
    if request.param == "postgresql":
        url = os.getenv("CLOUD_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("Set CLOUD_TEST_POSTGRES_URL for the real PostgreSQL concurrency suite")
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        schema = "metering_" + new_id()
        owner = create_async_engine(url)
        async with owner.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    else:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ledger.sqlite'}")
        @event.listens_for(engine.sync_engine, "connect")
        def pragmas(connection, _record):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=10000")
            cursor.close()
    async with engine.begin() as connection:
        await connection.run_sync(CloudBase.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    sessions.test_schema = schema
    async with sessions() as db:
        db.add(User(id="user-a", email="a@example.test", display_name="A", password_hash="unused", token_limit=1000))
        db.add(User(id="user-b", email="b@example.test", display_name="B", password_hash="unused", token_limit=1000))
        await db.commit()
    yield sessions
    await engine.dispose()
    if schema:
        async with owner.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await owner.dispose()


def usage(inputs=120, outputs=100):
    return {"input_total": inputs, "output_total": outputs,
            "total_tokens": inputs + outputs, "raw_usage": {"test_input": inputs}}


async def admitted(sessions, *, amount=300, model="model-a", job=None):
    async with sessions() as db:
        record = await metering.reserve(db, "user-a", model, job, amount)
        assert await metering.mark_dispatched(db, record.id)
        return record


async def test_concurrent_last_quota_is_atomic_across_connections(ledger):
    async with ledger() as db:
        record = await metering.reserve(db, "user-a", "model-a", None, 600)
        await metering.mark_dispatched(db, record.id)
        await metering.settle(db, record.id, usage(400, 200))

    async def contender():
        async with ledger() as db:
            try:
                return await metering.reserve(db, "user-a", "model-a", None, 300)
            except metering.QuotaError as exc:
                assert exc.code == "quota_exceeded"
                return None
    results = await asyncio.gather(contender(), contender())
    assert sum(record is not None for record in results) == 1
    async with ledger() as db:
        summary = await metering.usage_summary(db, "user-a")
        assert (summary["used_tokens"], summary["reserved_tokens"], summary["remaining_tokens"]) == (600, 300, 100)
        assert (await metering.usage_summary(db, "user-b"))["used_tokens"] == 0


async def test_quota_admission_is_safe_across_os_processes(ledger):
    child = '''
import asyncio, os, sys
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.cloud.metering import reserve, QuotaError
async def main():
    url = os.environ["METERING_CHILD_URL"]
    schema = os.environ["METERING_CHILD_SCHEMA"]
    options = {"connect_args": {"server_settings": {"search_path": schema}}} if schema else {}
    engine = create_async_engine(url, **options)
    if not schema:
        @event.listens_for(engine.sync_engine, "connect")
        def busy(connection, record):
            connection.execute("PRAGMA busy_timeout=10000")
    print("ready", flush=True)
    sys.stdin.readline()
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            try:
                await reserve(db, "user-a", "model-a", None, 700)
                print("admitted", flush=True)
            except QuotaError as exc:
                print(exc.code, flush=True)
    finally:
        await engine.dispose()
asyncio.run(main())
'''
    env = dict(os.environ)
    env["METERING_CHILD_URL"] = ledger.kw["bind"].url.render_as_string(hide_password=False)
    env["METERING_CHILD_SCHEMA"] = ledger.test_schema or ""
    processes = [await asyncio.create_subprocess_exec(
        sys.executable, "-c", child, env=env, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    ) for _ in range(2)]
    try:
        for process in processes:
            assert await asyncio.wait_for(process.stdout.readline(), 10) == b"ready\n"
        for process in processes:
            process.stdin.write(b"go\n")
            await process.stdin.drain()
        outputs = await asyncio.gather(*(asyncio.wait_for(p.communicate(), 15) for p in processes))
        assert sorted(stdout.strip() for stdout, _ in outputs) == [b"admitted", b"quota_exceeded"]
        assert all(p.returncode == 0 for p in processes)
    finally:
        for process in processes:
            if process.returncode is None:
                process.kill()
                await process.wait()
    async with ledger() as db:
        assert (await metering.usage_summary(db, "user-a"))["reserved_tokens"] == 700


async def test_parallel_duplicate_settlement_and_dispatch_only_once(ledger):
    record = await admitted(ledger)
    async with ledger() as db:
        assert not await metering.mark_dispatched(db, record.id)
    async def settle_one():
        async with ledger() as db:
            return await metering.settle(db, record.id, usage(), cost_microusd=17)
    await asyncio.gather(settle_one(), settle_one(), settle_one())
    async with ledger() as db:
        assert (await db.execute(select(func.count()).select_from(UsageEvent))).scalar_one() == 1
        summary = await metering.usage_summary(db, "user-a")
        assert (summary["used_tokens"], summary["reserved_tokens"], summary["total_cost_microusd"]) == (220, 0, 17)
        with pytest.raises(metering.QuotaError, match="different usage"):
            await metering.settle(db, record.id, usage(121), cost_microusd=17)
        assert (await metering.usage_summary(db, "user-a"))["used_tokens"] == 220


async def test_unknown_retains_reservation_until_explicit_reconciliation(ledger):
    record = await admitted(ledger, amount=1000)
    async with ledger() as db:
        await metering.mark_unknown(db, record.id, "stream interrupted")
        await metering.mark_unknown(db, record.id, "same unresolved request")
        summary = await metering.usage_summary(db, "user-a")
        assert (summary["reserved_tokens"], summary["unknown_requests"], summary["remaining_tokens"]) == (1000, 1, 0)
        with pytest.raises(metering.QuotaError):
            await metering.release(db, record.id)
        with pytest.raises(metering.QuotaError):
            await metering.reserve(db, "user-a", "model-a", None, 1)
        await metering.settle(db, record.id, usage())
        assert (await metering.usage_summary(db, "user-a"))["reserved_tokens"] == 0


async def test_release_and_dispatch_race_cannot_refund_sent_work(ledger):
    async with ledger() as db:
        record = await metering.reserve(db, "user-a", "model-a", None, 300)
    async def dispatch():
        async with ledger() as db:
            return await metering.mark_dispatched(db, record.id)
    async def release():
        async with ledger() as db:
            try:
                await metering.release(db, record.id)
                return True
            except metering.QuotaError:
                return False
    sent, refunded = await asyncio.gather(dispatch(), release())
    assert sent != refunded
    async with ledger() as db:
        summary = await metering.usage_summary(db, "user-a")
        assert summary["reserved_tokens"] == (300 if sent else 0)


async def test_admin_unknown_release_and_idempotency(ledger):
    record = await admitted(ledger)
    async with ledger() as db:
        with pytest.raises(metering.QuotaError):
            await metering.release(db, record.id, allow_unknown=True)
        await metering.mark_unknown(db, record.id)
        await metering.release(db, record.id, allow_unknown=True)
        await metering.release(db, record.id, allow_unknown=True)
        assert (await metering.usage_summary(db, "user-a"))["reserved_tokens"] == 0
        assert not await metering.mark_dispatched(db, record.id)


async def test_delayed_old_month_settles_original_bucket(ledger, monkeypatch):
    now = datetime(2026, 1, 31, 15, 59, tzinfo=timezone.utc)
    monkeypatch.setattr(metering, "utcnow", lambda: now)
    old = await admitted(ledger)
    now = datetime(2026, 1, 31, 16, 1, tzinfo=timezone.utc)  # Shanghai February
    async with ledger() as db:
        new = await metering.reserve(db, "user-a", "model-b", None, 500)
        await metering.settle(db, old.id, usage())
        summary = await metering.usage_summary(db, "user-a")
        assert (summary["period"], summary["used_tokens"], summary["reserved_tokens"]) == ("2026-02", 0, 500)
        buckets = (await db.execute(select(QuotaBucket).order_by(QuotaBucket.period))).scalars().all()
        assert [(b.period, b.used_tokens, b.reserved_tokens) for b in buckets] == [("2026-01", 220, 0), ("2026-02", 0, 500)]
        await metering.mark_dispatched(db, new.id)
        await metering.settle(db, new.id, usage())
        summary = await metering.usage_summary(db, "user-a")
        assert summary["daily"] == [{"date": "2026-02-01", "total_tokens": 220}]


async def test_actual_over_estimate_is_recorded_and_stops_next_admission(ledger):
    record = await admitted(ledger, amount=100)
    async with ledger() as db:
        await metering.settle(db, record.id, usage(1001, 100))
        summary = await metering.usage_summary(db, "user-a")
        assert (summary["used_tokens"], summary["reserved_tokens"], summary["remaining_tokens"]) == (1101, 0, 0)
        with pytest.raises(metering.QuotaError):
            await metering.reserve(db, "user-a", "model-a", None, 1)


async def test_limit_reduction_and_inactive_user_block_admission(ledger):
    async with ledger() as db:
        await metering.reserve(db, "user-a", "model-a", None, 600)
        await db.execute(update(User).where(User.id == "user-a").values(token_limit=300))
        await db.commit()
        assert (await metering.usage_summary(db, "user-a"))["remaining_tokens"] == 0
        with pytest.raises(metering.QuotaError):
            await metering.reserve(db, "user-a", "model-a", None, 1)
        await db.execute(update(User).where(User.id == "user-b").values(is_active=False))
        await db.commit()
        with pytest.raises(metering.QuotaError) as exc:
            await metering.reserve(db, "user-b", "model-a", None, 1)
        assert exc.value.status_code == 403


async def test_content_deletion_does_not_delete_ledger(ledger):
    async with ledger() as db:
        db.add(Workspace(id="workspace", user_id="user-a", name="Files"))
        await db.flush()
        db.add(Conversation(id="conversation", user_id="user-a", workspace_id="workspace", title="Chat"))
        await db.flush()
        db.add(Message(id="message", user_id="user-a", conversation_id="conversation", role="user", content="Hello"))
        db.add(Job(id="job", user_id="user-a", conversation_id="conversation", model_id="model-a", prompt="Hello", idempotency_key="once"))
        await db.commit()
    record = await admitted(ledger, job="job")
    async with ledger() as db:
        await metering.settle(db, record.id, usage())
        await db.execute(delete(Job).where(Job.id == "job"))
        await db.execute(delete(Conversation).where(Conversation.id == "conversation"))
        await db.commit()
        assert (await metering.usage_summary(db, "user-a"))["used_tokens"] == 220
        assert await db.get(RequestRecord, record.id)


async def test_partial_and_invalid_usage_keep_full_reservation(ledger):
    record = await admitted(ledger)
    for invalid in ({}, usage() | {"usage_status": "partial"}, usage() | {"total_tokens": 1},
                    usage() | {"cache_read": 500}, usage() | {"reasoning": -1}):
        async with ledger() as db:
            with pytest.raises(metering.QuotaError):
                await metering.settle(db, record.id, invalid)
            assert (await metering.usage_summary(db, "user-a"))["reserved_tokens"] == 300


@pytest.mark.parametrize("operation", ["settle", "release"])
async def test_reconciliation_audit_is_atomic_and_failure_rolls_back(ledger, operation):
    record = await admitted(ledger)
    async with ledger() as db:
        await metering.mark_unknown(db, record.id)
        await db.execute(update(User).where(User.id == "user-b").values(role="admin"))
        await db.commit()
    context = {"actor_user_id": "user-b", "reason": "Provider receipt checked", "action": "usage.reconcile"}
    async def reconcile(db):
        if operation == "settle":
            return await metering.settle(db, record.id, usage(), audit_context=context)
        return await metering.release(db, record.id, allow_unknown=True, audit_context=context)
    def audit_unavailable(*_args):
        raise RuntimeError("Audit storage unavailable")
    event.listen(AuditEvent, "before_insert", audit_unavailable)
    try:
        async with ledger() as db:
            with pytest.raises(RuntimeError, match="Audit storage"):
                await reconcile(db)
    finally:
        event.remove(AuditEvent, "before_insert", audit_unavailable)
    async with ledger() as db:
        assert (await db.get(RequestRecord, record.id)).state == "unknown"
        assert (await metering.usage_summary(db, "user-a"))["reserved_tokens"] == 300
        assert (await db.scalar(select(func.count()).select_from(UsageEvent))) == 0
        await reconcile(db)
        await reconcile(db)  # Identical replay never adds another audit row.
        events = (await db.scalars(select(AuditEvent))).all()
        assert len(events) == 1
        assert (events[0].actor_user_id, events[0].details["action"]) == ("user-b", operation)


async def test_reconciliation_cannot_forge_an_active_admin(ledger):
    record = await admitted(ledger)
    async with ledger() as db:
        await metering.mark_unknown(db, record.id)
        with pytest.raises(metering.QuotaError) as exc:
            await metering.release(db, record.id, allow_unknown=True,
                                   audit_context={"actor_user_id": "user-b", "reason": "Pretend admin"})
        assert exc.value.status_code == 403
        assert (await db.get(RequestRecord, record.id)).state == "unknown"
        assert (await metering.usage_summary(db, "user-a"))["reserved_tokens"] == 300
