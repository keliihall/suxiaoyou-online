"""Cloud database lifecycle and explicit versioned migrations."""

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from fastapi import Request

from app.cloud.config import CloudSettings

SCHEMA_REVISION = "cloud_0008"
_BACKEND_DIR = Path(__file__).resolve().parents[2]


def migration_config(database_url: str) -> Config:
    config = Config(str(_BACKEND_DIR / "cloud_alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND_DIR / "cloud_alembic"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


async def migrate(database_url: str) -> None:
    """Run from a dedicated deployment job/CLI; repeated execution is safe."""
    await asyncio.to_thread(command.upgrade, migration_config(database_url), "head")


class Database:
    def __init__(self, settings: CloudSettings):
        self.settings = settings
        if settings.database_url.startswith("sqlite"):
            from sqlalchemy.engine import make_url
            name = make_url(settings.database_url).database
            if name and name != ":memory:":
                Path(name).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_async_engine(settings.database_url, pool_pre_ping=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        if settings.database_url.startswith("sqlite"):
            @event.listens_for(self.engine.sync_engine, "connect")
            def _sqlite_pragmas(connection, _record):
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=10000")
                cursor.close()

    async def initialize(self) -> None:
        if self.settings.auto_migrate:
            # Run against this very connection so SQLite :memory: works too.
            async with self.engine.begin() as connection:
                def upgrade(sync_connection):
                    config = migration_config(self.settings.database_url)
                    config.attributes["connection"] = sync_connection
                    command.upgrade(config, "head")
                await connection.run_sync(upgrade)
        async with self.engine.connect() as connection:
            tables = await connection.run_sync(lambda c: set(inspect(c).get_table_names()))
            if "cloud_alembic_version" not in tables:
                raise RuntimeError("Cloud schema is not migrated; run python -m app.cloud.manage migrate")
            revisions = (await connection.execute(text("SELECT version_num FROM cloud_alembic_version"))).scalars().all()
            if revisions != [SCHEMA_REVISION]:
                raise RuntimeError(f"Cloud schema revision mismatch: expected {SCHEMA_REVISION}; run the migrate CLI")
            from app.cloud.models import CloudBase
            if not set(CloudBase.metadata.tables).issubset(tables):
                raise RuntimeError("Cloud schema is incomplete; restore or migrate before starting")

    async def close(self) -> None:
        await self.engine.dispose()


async def get_db(request: Request):
    async with request.app.state.database.sessions() as db:
        try:
            yield db
        finally:
            if db.in_transaction():
                await db.rollback()
