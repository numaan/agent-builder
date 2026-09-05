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

from sqlalchemy.engine import make_url

ENV_VAR = "SUPPORT_DATABASE_URL"
DEFAULT_DATABASE_URL = "postgresql+asyncpg://support:support@localhost:5432/support"

TEST_ENV_VAR = "SUPPORT_TEST_DATABASE_URL"
DEFAULT_TEST_DATABASE_URL = "postgresql+asyncpg://support:support@localhost:5432/support_test"
TEST_DATABASE_SUFFIX = "_test"

_DRIVER_PREFIX = "postgresql+asyncpg://"


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
