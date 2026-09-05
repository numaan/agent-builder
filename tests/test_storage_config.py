"""``SUPPORT_DATABASE_URL`` is the single switch for where everything connects, and
``test_database_url`` is the guard that keeps ``pytest`` away from non-test databases."""

import pytest

from support_core.storage import config

DEV_URL = "postgresql+asyncpg://support:support@localhost:5432/support"


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.delenv(config.ENV_VAR, raising=False)
    monkeypatch.delenv(config.TEST_ENV_VAR, raising=False)
    return monkeypatch


def test_default_matches_docker_compose(clean_env: pytest.MonkeyPatch) -> None:
    assert config.database_url() == DEV_URL


def test_env_var_overrides_default(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv(config.ENV_VAR, "postgresql+asyncpg://u:p@db.internal:6543/other")
    assert config.database_url() == "postgresql+asyncpg://u:p@db.internal:6543/other"


def test_non_asyncpg_url_is_rejected(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv(config.ENV_VAR, "postgresql://support:support@localhost/support")
    with pytest.raises(ValueError, match="postgresql\\+asyncpg://"):
        config.database_url()


def test_test_database_defaults_to_support_test(clean_env: pytest.MonkeyPatch) -> None:
    url = config.test_database_url()
    assert url == "postgresql+asyncpg://support:support@localhost:5432/support_test"
    assert url != config.database_url(), "tests must not default to the development database"


def test_test_database_refuses_non_test_name(clean_env: pytest.MonkeyPatch) -> None:
    """The reviewer's scenario (F2): a shell with the dev URL exported runs ``pytest``."""
    clean_env.setenv(config.ENV_VAR, DEV_URL)
    with pytest.raises(config.UnsafeTestDatabaseError, match=r"'support'.*_test.*SUPPORT_TEST"):
        config.test_database_url()


def test_test_database_accepts_test_suffix_from_main_env(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv(config.ENV_VAR, "postgresql+asyncpg://u:p@db.internal:6543/support_test")
    assert config.test_database_url() == "postgresql+asyncpg://u:p@db.internal:6543/support_test"


def test_explicit_test_url_wins_and_may_have_any_name(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv(config.ENV_VAR, DEV_URL)
    clean_env.setenv(config.TEST_ENV_VAR, "postgresql+asyncpg://u:p@ci:5432/throwaway")
    assert config.test_database_url() == "postgresql+asyncpg://u:p@ci:5432/throwaway"


def test_explicit_test_url_must_be_asyncpg(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv(config.TEST_ENV_VAR, "sqlite:///x_test.db")
    with pytest.raises(ValueError, match=config.TEST_ENV_VAR):
        config.test_database_url()
