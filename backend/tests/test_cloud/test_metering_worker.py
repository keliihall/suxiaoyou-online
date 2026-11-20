"""Worker fault boundaries: no paid model or outbound network is used."""

import asyncio
import contextlib
from datetime import timedelta

import pytest_asyncio
from sqlalchemy import func, select, update

from app.cloud import metering
from app.cloud.config import CloudSettings
from app.cloud.database import Database
from app.cloud.models import (
    CloudBase, Conversation, Job, Message, ModelRoute, QuotaBucket,
    RequestRecord, UsageEvent, User, Workspace, utcnow,
)
from app.cloud.providers import MockProvider
from app.cloud import worker as worker_module
from app.schemas.provider import StreamChunk


@pytest_asyncio.fixture
async def worker_env(tmp_path):
    settings = CloudSettings(environment="test", database_url=f"sqlite+aiosqlite:///{tmp_path / 'worker.sqlite'}",
                             data_dir=tmp_path / "files", allow_mock_provider=True, cookie_secure=False)
    database = Database(settings)
    async with database.engine.begin() as connection:
        await connection.run_sync(CloudBase.metadata.create_all)
    async with database.sessions() as db:
        db.add(User(id="owner", email="owner@example.test", display_name="Owner", password_hash="unused", token_limit=100000))
        await db.flush()
        db.add(Workspace(id="workspace", user_id="owner", name="Files"))
        await db.flush()
        db.add(Conversation(id="conversation", user_id="owner", workspace_id="workspace", title="Chat"))
        await db.flush()
        db.add(Message(id="message", user_id="owner", conversation_id="conversation", role="user", content="Hello"))
        db.add(ModelRoute(id="model", name="Mock", provider="mock", model="mock",
                          input_price_microusd_per_million=1000000,
                          output_price_microusd_per_million=2000000))
        db.add(Job(id="job", user_id="owner", conversation_id="conversation", model_id="model", prompt="Hello", idempotency_key="once", max_output_tokens=200))
        await db.commit()
    yield database, settings, worker_module.CloudWorker(database, settings, "test-worker")
    await database.close()


def receipt(*, partial=False):
    return {"input_total": 120, "output_total": 40, "total_tokens": 160,
            "cache_read": 0, "cache_write": 0, "reasoning": 0,
            "usage_status": "partial" if partial else "complete", "raw_usage": {"total": 160}}


class FakeProvider:
    def __init__(self, *, usage=None, error=False, pause_after_usage=False, duplicate=False):
        self.usage, self.error, self.pause_after_usage = usage, error, pause_after_usage
        self.duplicate = duplicate
        self.waiting = asyncio.Event()
        self.calls = 0
        self.closed = False

    async def stream_chat(self, *_args, **_kwargs):
        self.calls += 1
        yield StreamChunk(type="text-delta", data={"text": "Answer"})
        if self.usage is not None:
            yield StreamChunk(type="usage", data=self.usage)
            if self.duplicate:
                yield StreamChunk(type="usage", data=self.usage)
        if self.pause_after_usage:
            self.waiting.set()
            await asyncio.Event().wait()
        if self.error:
            yield StreamChunk(type="error", data={"message": "interrupted"})
        yield StreamChunk(type="finish", data={"reason": "stop"})

    async def aclose(self):
        self.closed = True


async def state(database):
    async with database.sessions() as db:
        job = await db.get(Job, "job")
        requests = (await db.scalars(select(RequestRecord))).all()
        events = (await db.scalars(select(UsageEvent))).all()
        summary = await metering.usage_summary(db, "owner")
        return job, requests, events, summary


async def test_worker_duplicate_usage_snapshots_settle_only_once(worker_env, monkeypatch):
    database, _, worker = worker_env
    provider = FakeProvider(usage=receipt(), duplicate=True)
    monkeypatch.setattr(worker_module, "build_provider", lambda *_args: provider)
    assert await worker.run_once()
    assert not await worker.run_once()
    job, requests, events, summary = await state(database)
    assert job.state == "completed"
    assert len(requests) == len(events) == provider.calls == 1
    assert requests[0].state == "settled"
    assert (summary["used_tokens"], summary["reserved_tokens"], summary["total_cost_microusd"]) == (160, 0, 200)
    assert provider.closed


async def test_partial_usage_preserves_unknown_and_worker_remains_usable(worker_env, monkeypatch):
    database, _, worker = worker_env
    provider = FakeProvider(usage=receipt(partial=True))
    monkeypatch.setattr(worker_module, "build_provider", lambda *_args: provider)
    assert await worker.run_once()
    job, requests, events, summary = await state(database)
    assert job.state == "needs_review"
    assert requests[0].state == "unknown"
    assert events == [] and summary["reserved_tokens"] > 0
    assert summary["unknown_requests"] == 1


async def test_cancel_after_usage_settles_actual_cost_even_before_normal_finish(worker_env, monkeypatch):
    database, _, worker = worker_env
    provider = FakeProvider(usage=receipt(), pause_after_usage=True)
    monkeypatch.setattr(worker_module, "build_provider", lambda *_args: provider)
    job = await worker.claim()
    task = asyncio.create_task(worker.execute(job))
    await asyncio.wait_for(provider.waiting.wait(), 3)
    task.cancel()
    await task
    job, requests, events, summary = await state(database)
    assert job.state == "cancelled"
    assert requests[0].state == "settled"
    assert len(events) == 1 and summary["total_cost_microusd"] == 200
    assert summary["reserved_tokens"] == 0


async def test_error_after_known_usage_still_records_real_cost(worker_env, monkeypatch):
    database, _, worker = worker_env
    monkeypatch.setattr(worker_module, "build_provider", lambda *_args: FakeProvider(usage=receipt(), error=True))
    await worker.run_once()
    job, requests, events, summary = await state(database)
    assert job.state == "failed"
    assert requests[0].state == "settled"
    assert len(events) == 1 and summary["total_cost_microusd"] == 200


async def test_stream_error_without_usage_does_not_refund_dispatched_request(worker_env, monkeypatch):
    database, _, worker = worker_env
    monkeypatch.setattr(worker_module, "build_provider", lambda *_args: FakeProvider(error=True))
    await worker.run_once()
    job, requests, events, summary = await state(database)
    assert job.state == "needs_review" and requests[0].state == "unknown"
    assert events == [] and summary["reserved_tokens"] > 0


async def test_cancel_after_reserve_commit_before_return_cleans_orphan_reservation(worker_env, monkeypatch):
    database, _, worker = worker_env
    committed = asyncio.Event()
    original = metering.reserve
    async def reserve_then_pause(*args, **kwargs):
        record = await original(*args, **kwargs)
        committed.set()
        await asyncio.Event().wait()
        return record
    monkeypatch.setattr(metering, "reserve", reserve_then_pause)
    monkeypatch.setattr(worker_module, "build_provider", lambda *_args: MockProvider(delay_seconds=0))
    job = await worker.claim()
    task = asyncio.create_task(worker.execute(job))
    await asyncio.wait_for(committed.wait(), 3)
    task.cancel()
    await task
    # A second sweep must also be able to repair a crash after terminalization.
    await worker.recover_expired()
    job, requests, events, summary = await state(database)
    assert job.state == "cancelled"
    assert requests[0].state == "released"
    assert events == [] and summary["reserved_tokens"] == 0


async def test_worker_expired_dispatch_becomes_unknown_and_is_never_resent(worker_env, monkeypatch):
    database, _, worker = worker_env
    provider = FakeProvider(usage=receipt())
    monkeypatch.setattr(worker_module, "build_provider", lambda *_args: provider)
    job = await worker.claim()
    async with database.sessions() as db:
        attempt = await metering.reserve(db, "owner", "model", job.id, 500)
        await metering.mark_dispatched(db, attempt.id)
        await db.execute(update(Job).where(Job.id == job.id).values(
            attempt_id=attempt.id, lease_until=utcnow() - timedelta(seconds=1)))
        await db.commit()
    assert await worker.recover_expired() == 1
    assert not await worker.run_once()
    job, requests, events, summary = await state(database)
    assert job.state == "needs_review" and requests[0].state == "unknown"
    assert events == [] and summary["reserved_tokens"] == 500 and provider.calls == 0


async def test_failed_recovery_accounting_is_repaired_on_next_sweep(worker_env, monkeypatch):
    database, _, worker = worker_env
    job = await worker.claim()
    async with database.sessions() as db:
        attempt = await metering.reserve(db, "owner", "model", job.id, 500)
        await metering.mark_dispatched(db, attempt.id)
        await db.execute(update(Job).where(Job.id == job.id).values(
            attempt_id=attempt.id, lease_until=utcnow() - timedelta(seconds=1)))
        await db.commit()
    original, calls = metering.mark_unknown, []
    async def fails_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("Temporary accounting store failure")
        return await original(*args, **kwargs)
    monkeypatch.setattr(metering, "mark_unknown", fails_once)
    with contextlib.suppress(RuntimeError):
        await worker.recover_expired()
    await worker.recover_expired()
    job, requests, events, summary = await state(database)
    assert job.state == "needs_review" and requests[0].state == "unknown"
    assert events == [] and summary["unknown_requests"] == 1
