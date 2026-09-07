"""Database connection settings (DESIGN.md section 17, 20 "secrets via environment").

There is exactly one place that knows how to find the database: this module. Alembic's
``env.py``, the application session factory and the test fixtures all call
:func:`database_url` (or :func:`test_database_url`) so that setting ``SUPPORT_DATABASE_URL``
reconfigures everything.

The test suite rebuilds the schema of the database it is pointed at (``alembic downgrade
base`` then ``upgrade head``), so it must never run against a development or production
database by accident. :func:`test_database_url` enforces that: it only returns a URL whose
database name ends in ``_test`` unless ``SUPPORT_TEST_DATABASE_URL`` is set explicitly.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy.engine import make_url

ENV_VAR = "SUPPORT_DATABASE_URL"
DEFAULT_DATABASE_URL = "postgresql+asyncpg://support:support@localhost:5432/support"

TEST_ENV_VAR = "SUPPORT_TEST_DATABASE_URL"
DEFAULT_TEST_DATABASE_URL = "postgresql+asyncpg://support:support@localhost:5432/support_test"
TEST_DATABASE_SUFFIX = "_test"

_DRIVER_PREFIX = "postgresql+asyncpg://"

POOL_SIZE_ENV = "SUPPORT_DB_POOL_SIZE"
MAX_OVERFLOW_ENV = "SUPPORT_DB_MAX_OVERFLOW"
POOL_TIMEOUT_ENV = "SUPPORT_DB_POOL_TIMEOUT"

CONNECTIONS_PER_TURN = 3
"""How many pooled connections one running turn can hold **at the same time**.

Counted from the code rather than guessed, because the whole of security review finding S2 is
that nobody had counted (`reviews/security-review-2026-09-07.md`, S2). Re-derive it by reading
these three:

1. **The advisory lock.** :func:`~support_core.engine.locks.conversation_lock` opens a
   connection of its own and holds its transaction open for the *whole* turn. It has to be its
   own: a turn is deliberately many transactions, one per node, so the lock cannot live on the
   connection doing the work (`support_core/engine/locks.py`, module docstring).
2. **The unit of work in hand.** One session at a time - the claim, a checkpoint, a tool call,
   the history read. Each opens, commits and closes; they do not overlap each other.
3. **One more, briefly, at the end of a checkpoint that spoke.**
   :meth:`~support_core.engine.executor.Executor._flush_outbound` holds its session open while
   it calls the send hook, and :meth:`~support_core.channels.hub.ChannelHub.deliver` opens a
   second session inside it to read the conversation row. That nesting is what makes the peak
   three rather than two.

If the send path stops nesting a session inside the flush transaction, this becomes 2 and the
pool can shrink. Nothing else in a turn holds a connection across an ``await`` on another one.
"""

RESERVE_CONNECTIONS = 8
"""Connections kept out of reach of the turns, so the service can still answer.

What lives here: ``/healthz``, every desk endpoint, the transcript a connecting socket reads,
the channel's conversation lookup, and the ``pending`` row
:meth:`~support_core.engine.executor.Executor.on_inbound` writes *before* it tries the lock.
All of them are single short transactions, and all of them are on the same pool as the turns,
which is why a saturated pool took ``/healthz`` down with it.
"""

DEFAULT_MAX_CONCURRENT_TURNS = 6
"""Turns one replica runs at once by default; see :func:`pool_settings` for what it costs.

Six rather than a larger number because Postgres ships with ``max_connections = 100`` and
DESIGN.md section 20 puts several stateless replicas behind a load balancer: at 26 connections a
replica, three replicas and a migration still fit. A deployment with a bigger database raises
``max_concurrent_turns`` and the pool follows it.
"""

DEFAULT_POOL_TIMEOUT_SECONDS = 5.0
"""How long a checkout waits before it is refused.

SQLAlchemy's default is 30 s, which is how a flood became a 30-second stall for every caller
including the health check. With the turn bound in front of the pool a checkout should never
queue at all, so this is the "something is wrong" timer rather than a working wait, and it is
short enough that the refusal reaches the client as an answer instead of a timeout.
"""


@dataclass(frozen=True, slots=True)
class PoolSettings:
    """The size of one process's connection pool."""

    size: int
    """Connections kept open once opened."""

    max_overflow: int
    """Extra connections opened on demand and closed when returned."""

    timeout: float
    """Seconds a caller waits for a connection before being refused."""

    @property
    def capacity(self) -> int:
        """The most connections this process will ever hold at once."""
        return self.size + self.max_overflow


def _positive_int(source: Mapping[str, str], name: str, fallback: int) -> int:
    raw = source.get(name)
    if raw is None or not raw.strip():
        return fallback
    try:
        value = int(raw)
    except ValueError as exc:
        msg = f"{name} must be a whole number, got {raw!r}"
        raise ValueError(msg) from exc
    if value < 0:
        msg = f"{name} must not be negative, got {value}"
        raise ValueError(msg)
    return value


def _positive_float(source: Mapping[str, str], name: str, fallback: float) -> float:
    raw = source.get(name)
    if raw is None or not raw.strip():
        return fallback
    try:
        value = float(raw)
    except ValueError as exc:
        msg = f"{name} must be a number of seconds, got {raw!r}"
        raise ValueError(msg) from exc
    if value <= 0:
        msg = f"{name} must be greater than zero, got {value}"
        raise ValueError(msg)
    return value


def pool_settings(
    max_concurrent_turns: int = DEFAULT_MAX_CONCURRENT_TURNS,
    *,
    env: Mapping[str, str] | None = None,
) -> PoolSettings:
    """The pool one process needs to run ``max_concurrent_turns`` turns and stay answerable.

    The derivation, so that the next person can redo it rather than trust it:

    * a turn costs at most :data:`CONNECTIONS_PER_TURN` connections at its peak, and exactly one
      of those - the advisory lock - is held for the turn's whole length;
    * so ``size`` keeps one connection per turn slot, which is the steady state, plus
      :data:`RESERVE_CONNECTIONS` for the work that is not a turn;
    * and ``max_overflow`` covers the other two connections a turn wants at its peak, which are
      taken for milliseconds and returned. Overflow rather than ``size`` because a connection
      that is only wanted at a peak should not be held open between peaks.

    Capacity is therefore ``max_concurrent_turns * CONNECTIONS_PER_TURN + RESERVE_CONNECTIONS``:
    26 at the default of six turns. The reserve is what makes ``/healthz`` answerable under load,
    and it only works because the turns are bounded - which is
    :class:`~support_core.engine.executor.TurnSlots`, not this function. Sizing the pool alone
    would move the queue rather than remove it.

    ``SUPPORT_DB_POOL_SIZE``, ``SUPPORT_DB_MAX_OVERFLOW`` and ``SUPPORT_DB_POOL_TIMEOUT``
    override the three numbers for a deployment whose database or turn shape is different.
    """
    source = dict(env if env is not None else os.environ)
    turns = max(1, max_concurrent_turns)
    return PoolSettings(
        size=_positive_int(source, POOL_SIZE_ENV, turns + RESERVE_CONNECTIONS),
        max_overflow=_positive_int(source, MAX_OVERFLOW_ENV, turns * (CONNECTIONS_PER_TURN - 1)),
        timeout=_positive_float(source, POOL_TIMEOUT_ENV, DEFAULT_POOL_TIMEOUT_SECONDS),
    )


class UnsafeTestDatabaseError(ValueError):
    """The test suite refused to run because it would rebuild a non-test database."""


def _require_asyncpg(url: str, source: str) -> str:
    if not url.startswith(_DRIVER_PREFIX):
        msg = f"{source} must start with {_DRIVER_PREFIX}, got {url!r}"
        raise ValueError(msg)
    return url


def database_url() -> str:
    """Return the SQLAlchemy async URL for Postgres.

    Reads ``SUPPORT_DATABASE_URL``; falls back to the docker-compose defaults. Only the
    ``postgresql+asyncpg`` driver is supported because the engine (section 7) relies on
    asyncpg-specific behaviour such as advisory locks inside async transactions.
    """
    return _require_asyncpg(os.environ.get(ENV_VAR, DEFAULT_DATABASE_URL), ENV_VAR)


def test_database_url() -> str:
    """Return the URL the test suite may destroy and rebuild.

    Resolution order:

    1. ``SUPPORT_TEST_DATABASE_URL`` if set: used as-is (explicit opt-in, any database name).
    2. ``SUPPORT_DATABASE_URL`` if set: used only when its database name ends in ``_test``;
       otherwise :class:`UnsafeTestDatabaseError` is raised so ``pytest`` cannot drop the
       tables of whatever the developer's shell happens to point at.
    3. Otherwise the docker-compose ``support_test`` database, which ``scripts/db-up.sh``
       creates next to the development database ``support``.
    """
    explicit = os.environ.get(TEST_ENV_VAR)
    if explicit is not None:
        return _require_asyncpg(explicit, TEST_ENV_VAR)
    configured = os.environ.get(ENV_VAR)
    if configured is None:
        return DEFAULT_TEST_DATABASE_URL
    url = _require_asyncpg(configured, ENV_VAR)
    name = make_url(url).database or ""
    if not name.endswith(TEST_DATABASE_SUFFIX):
        msg = (
            f"refusing to run tests against database {name!r} from {ENV_VAR}: the suite drops "
            f"and recreates every table. Point {ENV_VAR} at a database whose name ends in "
            f"{TEST_DATABASE_SUFFIX!r}, or set {TEST_ENV_VAR} explicitly."
        )
        raise UnsafeTestDatabaseError(msg)
    return url
