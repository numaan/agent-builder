"""Async engine and session factory (DESIGN.md section 17; PLAN.md conventions).

Nothing outside ``support_core.storage`` should construct engines directly; the engine and
the repositories (phase 2) receive a session factory built here.

The pool is **sized explicitly** (security review finding S2). It used to be SQLAlchemy's
default - five connections, ten overflow, a thirty-second checkout timeout - which nothing in
this repository had chosen and nothing had counted against what a turn costs, so ten concurrent
customers exhausted it and every later request, ``/healthz`` included, blocked for thirty
seconds and then raised. :func:`~support_core.storage.config.pool_settings` does the counting
and says where the numbers come from.
"""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from support_core.storage.config import PoolSettings, database_url, pool_settings


def make_engine(
    url: str | None = None, *, echo: bool = False, pool: PoolSettings | None = None
) -> AsyncEngine:
    """Create an asyncpg-backed engine. ``url`` defaults to :func:`database_url`.

    ``pool`` defaults to :func:`~support_core.storage.config.pool_settings` for the default turn
    bound; a service that runs a different number of concurrent turns passes the settings that
    match it, so that the pool and the bound cannot drift apart.
    """
    settings = pool or pool_settings()
    return create_async_engine(
        url or database_url(),
        echo=echo,
        pool_pre_ping=True,
        pool_size=settings.size,
        max_overflow=settings.max_overflow,
        pool_timeout=settings.timeout,
    )


def pool_capacity(engine: AsyncEngine) -> int:
    """The most connections ``engine`` will ever hold at once, or ``0`` if it does not pool.

    Used by ``/healthz`` to answer "is every connection out?" without asking for one. ``0`` for
    ``NullPool`` and ``StaticPool``, which the test fixtures use and which cannot saturate.

    ``_max_overflow`` is private to SQLAlchemy's ``QueuePool``; it is read defensively here and a
    pool that does not have it simply reports "no capacity known", which turns the check off
    rather than breaking it.
    """
    pool = engine.pool
    size = getattr(pool, "size", None)
    if not callable(size):
        return 0
    overflow = getattr(pool, "_max_overflow", 0)
    try:
        return int(size()) + max(0, int(overflow))
    except (TypeError, ValueError):  # pragma: no cover - a pool with an unusual size()
        return 0


def connections_in_use(engine: AsyncEngine) -> int:
    """How many of this engine's connections are checked out right now, or ``0`` if unknown."""
    checkedout = getattr(engine.pool, "checkedout", None)
    if not callable(checkedout):
        return 0
    try:
        return max(0, int(checkedout()))
    except (TypeError, ValueError):  # pragma: no cover - as above
        return 0


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory with ``expire_on_commit=False`` so ORM objects stay usable after commit.

    That matters for the engine's checkpoint transactions (section 7.1), which commit and
    then keep using the loaded run.
    """
    return async_sessionmaker(engine, expire_on_commit=False)
