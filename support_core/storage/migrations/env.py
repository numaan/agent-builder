"""Alembic environment using the async engine (DESIGN.md sections 17, 18; PLAN.md conventions).

Two ways to run:

* From the command line (``alembic upgrade head``): an async engine is created from
  :func:`support_core.storage.config.database_url` and migrations run inside it.
* Programmatically with an existing connection: callers put a sync ``Connection`` into
  ``config.attributes["connection"]`` (typically from ``AsyncConnection.run_sync``) and this
  module uses it as-is. ``tests/conftest.py`` relies on this so migrations run inside the test
  engine rather than opening a second one.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from support_core.storage.config import database_url
from support_core.storage.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a database connection."""
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(database_url(), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        do_run_migrations(connection)
    else:
        asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
