"""Where the vector store is. Implements the configuration half of DESIGN.md section 9.1, in the
shape section 20 fixes ("secrets via environment").

Deliberately the same shape as :mod:`support_core.storage.config`: one module knows how to find
one store, everything else asks it, and setting one environment variable reconfigures the CLI,
the application and the tests at once. A URL is not a secret, so it may also be written into an
``AppConfig`` file; an API key would not be.

The default is the docker-compose service. Unset and unreachable are different things and are
kept different: a deployment that names no Qdrant runs with the lexical and dense halves only and
says so once at startup, while a deployment that names one and cannot reach it degrades per call
and logs per call, because that is a fault rather than a choice.
"""

import os

ENV_VAR = "SUPPORT_QDRANT_URL"
DEFAULT_QDRANT_URL = "http://localhost:6333"

TEST_ENV_VAR = "SUPPORT_TEST_QDRANT_URL"
"""Lets the suite point at a different instance from the developer's own.

There is no ``_test`` suffix rule of the kind :func:`support_core.storage.config.test_database_url`
enforces, because the danger it guards against does not exist here: the tests never drop a
collection they did not create. Every collection they touch carries a per-run random prefix, so
a suite run against a developer's Qdrant leaves their own collections alone."""


def qdrant_url() -> str:
    """The Qdrant instance to use, from ``SUPPORT_QDRANT_URL`` or the compose default."""
    return os.environ.get(ENV_VAR, DEFAULT_QDRANT_URL)


def test_qdrant_url() -> str:
    """The instance the suite uses."""
    return os.environ.get(TEST_ENV_VAR) or qdrant_url()
