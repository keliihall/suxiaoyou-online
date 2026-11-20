"""Actual file-SQLite HTTP mutations keep task and scheduling state atomic."""
from datetime import datetime
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.automations import router
from app.dependencies import get_db
from app.models.base import Base
from app.scheduler.engine import TaskScheduler


@pytest.mark.asyncio
async def test_http_create_template_and_toggle_schedule_in_the_writer_transaction(tmp_path):
    # File-backed connections expose the real uncommitted-row visibility issue;
    # a single StaticPool in-memory connection can accidentally hide it.
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'scheduler.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.state.task_scheduler = TaskScheduler(factory, SimpleNamespace())

    async def transaction():
        async with factory() as db:
            async with db.begin():
                yield db

    app.dependency_overrides[get_db] = transaction
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/automations", json={"name": "Transaction test", "prompt": "No provider is invoked",
                "schedule_config": {"type": "interval", "minutes": 1}})
            assert response.status_code == 200
            created = response.json()
            assert created["next_run_at"] is not None
            path = "/api/automations/" + created["id"]
            assert (await client.get(path)).json()["next_run_at"] == created["next_run_at"]
            disabled = await client.patch(path, json={"enabled": False})
            assert disabled.status_code == 200 and disabled.json()["next_run_at"] is None
            assert (await client.get(path)).json()["enabled"] is False
            enabled = await client.patch(path, json={"enabled": True, "schedule_config": {"type": "interval", "minutes": 2}})
            assert enabled.status_code == 200
            assert datetime.fromisoformat(enabled.json()["next_run_at"]) > datetime.fromisoformat(created["next_run_at"])
            assert (await client.get(path)).json()["next_run_at"] == enabled.json()["next_run_at"]
            templates = (await client.get("/api/automations/templates")).json()
            template = next(item for item in templates if item["schedule_config"])
            selected = await client.post("/api/automations/from-template", params={"template_id": template["id"]})
            assert selected.status_code == 200 and selected.json()["next_run_at"] is not None
            assert (await client.get("/api/automations/" + selected.json()["id"])).json()["next_run_at"] == selected.json()["next_run_at"]
    finally:
        await engine.dispose()
