"""Database connection settings (DESIGN.md section 17, 20 "secrets via environment").

There is exactly one place that knows how to find the database: this module. Alembic's
``env.py``, the application session factory and the test fixtures all call
:func:`database_url` so that setting ``SUPPORT_DATABASE_URL`` reconfigures everything.
"""

import os

ENV_VAR = "SUPPORT_DATABASE_URL"
DEFAULT_DATABASE_URL = "postgresql+asyncpg://support:support@localhost:5432/support"


def database_url() -> str:
    """Return the SQLAlchemy async URL for Postgres.

    Reads ``SUPPORT_DATABASE_URL``; falls back to the docker-compose defaults. Only the
    ``postgresql+asyncpg`` driver is supported because the engine (section 7) relies on
    asyncpg-specific behaviour such as advisory locks inside async transactions.
    """
    url = os.environ.get(ENV_VAR, DEFAULT_DATABASE_URL)
    if not url.startswith("postgresql+asyncpg://"):
        msg = f"{ENV_VAR} must start with postgresql+asyncpg://, got {url!r}"
        raise ValueError(msg)
    return url
