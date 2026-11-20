"""Independent cloud migration environment; never imports desktop metadata."""

import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.cloud.models import CloudBase

config = context.config
target_metadata = CloudBase.metadata


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata, version_table="cloud_alembic_version", compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def online():
    engine = async_engine_from_config(config.get_section(config.config_ini_section), prefix="sqlalchemy.", poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata, version_table="cloud_alembic_version", literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()
elif config.attributes.get("connection") is not None:
    do_run_migrations(config.attributes["connection"])
else:
    asyncio.run(online())
