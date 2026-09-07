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


# -- the connection pool (security review finding S2) ----------------------------------------


def test_the_pool_is_sized_from_what_a_turn_costs() -> None:
    """The derivation, asserted rather than trusted.

    Capacity has to cover every turn at its peak *and* leave the reserve untouched, because the
    reserve is what ``/healthz``, the desk and the inbound write live on. A pool that only just
    fits the turns is the finding again with different numbers.
    """
    settings = config.pool_settings(6, env={})
    assert settings.capacity == 6 * config.CONNECTIONS_PER_TURN + config.RESERVE_CONNECTIONS
    assert settings.size == 6 + config.RESERVE_CONNECTIONS
    assert settings.max_overflow == 6 * (config.CONNECTIONS_PER_TURN - 1)
    assert settings.timeout == config.DEFAULT_POOL_TIMEOUT_SECONDS


def test_a_bigger_turn_bound_gets_a_bigger_pool() -> None:
    """Raising the bound without raising the pool would be the finding all over again."""
    small = config.pool_settings(2, env={})
    large = config.pool_settings(20, env={})
    assert large.capacity > small.capacity
    for turns in (1, 2, 6, 20, 64):
        settings = config.pool_settings(turns, env={})
        assert settings.capacity >= turns * config.CONNECTIONS_PER_TURN


def test_the_checkout_timeout_is_not_sqlalchemys_thirty_seconds() -> None:
    """Thirty seconds is how a flood became a thirty-second stall for every caller."""
    assert config.pool_settings(env={}).timeout <= 10.0


def test_the_pool_can_be_sized_from_the_environment() -> None:
    settings = config.pool_settings(
        6,
        env={
            config.POOL_SIZE_ENV: "3",
            config.MAX_OVERFLOW_ENV: "4",
            config.POOL_TIMEOUT_ENV: "1.5",
        },
    )
    assert (settings.size, settings.max_overflow, settings.timeout) == (3, 4, 1.5)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("POOL_SIZE_ENV", "many"),
        ("POOL_SIZE_ENV", "-1"),
        ("POOL_TIMEOUT_ENV", "soon"),
        ("POOL_TIMEOUT_ENV", "0"),
    ],
)
def test_a_pool_setting_that_is_not_a_number_is_refused(name: str, value: str) -> None:
    """At startup, where an operator reads it, rather than at the first checkout."""
    with pytest.raises(ValueError, match=getattr(config, name)):
        config.pool_settings(env={getattr(config, name): value})
