"""Async engine and session factory (DESIGN.md section 17; PLAN.md conventions).

Nothing outside ``support_core.storage`` should construct engines directly; the engine and
the repositories (phase 2) receive a session factory built here.
"""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from support_core.storage.config import database_url


def make_engine(url: str | None = None, *, echo: bool = False) -> AsyncEngine:
    """Create an asyncpg-backed engine. ``url`` defaults to :func:`database_url`."""
    return create_async_engine(url or database_url(), echo=echo, pool_pre_ping=True)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory with ``expire_on_commit=False`` so ORM objects stay usable after commit.

    That matters for the engine's checkpoint transactions (section 7.1), which commit and
    then keep using the loaded run.
    """
    return async_sessionmaker(engine, expire_on_commit=False)
