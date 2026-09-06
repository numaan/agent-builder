"""Shared fixtures. Database tests run against real Postgres (PLAN.md: no mocking the database).

Lifecycle:

* ``migrated_database`` (session scope): downgrade to base, then upgrade to head, using the
  Alembic migrations through the same async engine the application uses. Doing the downgrade
  first means every run also exercises the downgrade path and starts from a known schema.
* ``engine`` (function scope): a fresh ``AsyncEngine`` per test with ``NullPool`` so no
  connection outlives the test's event loop. Before yielding it truncates every mapped table.
* ``db_session`` (function scope): an ``AsyncSession`` from the application's session factory.

The suite runs against the dedicated ``support_test`` database, never the development one:
:func:`support_core.storage.config.test_database_url` refuses any database whose name does
not end in ``_test`` unless ``SUPPORT_TEST_DATABASE_URL`` is set explicitly. The check runs
in ``pytest_configure`` so a mis-pointed shell fails before a single test is collected.
"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from support_core.storage.config import UnsafeTestDatabaseError, test_database_url
from support_core.storage.models import ALL_TABLES
from support_core.storage.session import make_session_factory

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKS_DIR = REPO_ROOT / "packs"
SAMPLE_PACK = PACKS_DIR / "acme_billing"


def pytest_configure(config: pytest.Config) -> None:
    """Refuse to start if the suite would rebuild a non-test database (review finding F2)."""
    try:
        test_database_url()
    except UnsafeTestDatabaseError as exc:
        raise pytest.UsageError(str(exc)) from exc


def pytest_report_header(config: pytest.Config) -> str:
    return f"database: {make_url(test_database_url()).render_as_string(hide_password=True)}"


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
    engine = create_async_engine(test_database_url(), poolclass=NullPool)
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


TRUNCATE_ATTEMPTS = 5
TRUNCATE_LOCK_TIMEOUT = "5s"
CONTENDED = frozenset({"40P01", "55P03"})
"""``deadlock_detected`` and ``lock_not_available``: somebody else is holding these tables.

Reported by phase 6's reviewer, who lost twenty minutes to it. ``TRUNCATE`` takes an
``AccessExclusiveLock`` on every mapped table, and anything else touching ``support_test`` at the
same moment - a second ``pytest``, an app pointed at it - holds a ``RowShareLock`` on some of
them, so the two orders meet and Postgres kills one side. The whole file then fails behind it
with ``no conversation <uuid>`` and foreign-key violations that reproduce for nobody.

Retrying is the documented answer to a deadlock and it is the fixture's own problem, not the
product's. What a retry cannot fix is the *other* half - another process truncating rows out from
under a running test - which is phase-0 finding N9 and needs a session-long lock the fixture
design deliberately avoids. So the last attempt says what is probably happening, which is the
part the next person actually needs."""


async def _truncate(engine: AsyncEngine) -> None:
    """Empty every mapped table, waiting rather than hanging and retrying rather than failing."""
    table_list = ", ".join(f'"{t.name}"' for t in ALL_TABLES)
    for attempt in range(TRUNCATE_ATTEMPTS):
        try:
            async with engine.begin() as conn:
                await conn.execute(text(f"SET LOCAL lock_timeout = '{TRUNCATE_LOCK_TIMEOUT}'"))
                await conn.execute(text(f"TRUNCATE {table_list} RESTART IDENTITY CASCADE"))
        except DBAPIError as exc:
            code = getattr(getattr(exc, "orig", None), "sqlstate", None) or ""
            if code not in CONTENDED or attempt == TRUNCATE_ATTEMPTS - 1:
                raise
            await asyncio.sleep(0.1 * (attempt + 1))
        else:
            return


@pytest.fixture
async def engine(migrated_database: None) -> AsyncIterator[AsyncEngine]:
    """Per-test engine; every mapped table is truncated before the test starts."""
    engine = create_async_engine(test_database_url(), poolclass=NullPool)
    try:
        await _truncate(engine)
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with make_session_factory(engine)() as session:
        yield session
