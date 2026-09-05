"""Shared fixtures. Database tests run against real Postgres (PLAN.md: no mocking the database).

Lifecycle:

* ``migrated_database`` (session scope): downgrade to base, then upgrade to head, using the
  Alembic migrations through the same async engine the application uses. Doing the downgrade
  first means every run also exercises the downgrade path and starts from a known schema.
* ``engine`` (function scope): a fresh ``AsyncEngine`` per test with ``NullPool`` so no
  connection outlives the test's event loop. Before yielding it truncates every mapped table.
* ``db_session`` (function scope): an ``AsyncSession`` from the application's session factory.

Set ``SUPPORT_DATABASE_URL`` to point at a different Postgres; the default matches
``docker-compose.yml``.
"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from support_core.storage.config import database_url
from support_core.storage.models import ALL_TABLES
from support_core.storage.session import make_session_factory

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKS_DIR = REPO_ROOT / "packs"
SAMPLE_PACK = PACKS_DIR / "acme_billing"


def alembic_config() -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option(
        "script_location", str(REPO_ROOT / "support_core" / "storage" / "migrations")
    )
    return cfg


def _run_alembic(connection: Connection, cfg: Config, revision: str, *, downgrade: bool) -> None:
    cfg.attributes["connection"] = connection
    if downgrade:
        command.downgrade(cfg, revision)
    else:
        command.upgrade(cfg, revision)


async def _rebuild_schema() -> None:
    engine = create_async_engine(database_url(), poolclass=NullPool)
    try:
        cfg = alembic_config()
        async with engine.begin() as conn:
            await conn.run_sync(_run_alembic, cfg, "base", downgrade=True)
        async with engine.begin() as conn:
            await conn.run_sync(_run_alembic, cfg, "head", downgrade=False)
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def migrated_database() -> None:
    """Rebuild the schema from the migrations once per test session."""
    asyncio.run(_rebuild_schema())


@pytest.fixture
async def engine(migrated_database: None) -> AsyncIterator[AsyncEngine]:
    """Per-test engine; every mapped table is truncated before the test starts."""
    engine = create_async_engine(database_url(), poolclass=NullPool)
    try:
        table_list = ", ".join(f'"{t.name}"' for t in ALL_TABLES)
        async with engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {table_list} RESTART IDENTITY CASCADE"))
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with make_session_factory(engine)() as session:
        yield session
