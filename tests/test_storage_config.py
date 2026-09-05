"""``SUPPORT_DATABASE_URL`` is the single switch for where everything connects."""

import pytest

from support_core.storage import config


def test_default_matches_docker_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(config.ENV_VAR, raising=False)
    assert config.database_url() == "postgresql+asyncpg://support:support@localhost:5432/support"


def test_env_var_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(config.ENV_VAR, "postgresql+asyncpg://u:p@db.internal:6543/other")
    assert config.database_url() == "postgresql+asyncpg://u:p@db.internal:6543/other"


def test_non_asyncpg_url_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(config.ENV_VAR, "postgresql://support:support@localhost/support")
    with pytest.raises(ValueError, match="postgresql\\+asyncpg://"):
        config.database_url()
