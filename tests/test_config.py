"""Tests for :mod:`core.config` -- environment loading, validation, secret typing."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from core.config import (
    DEFAULT_DEV_DATABASE_URL,
    Environment,
    build_settings,
    get_settings,
    reload_settings,
)

from .conftest import make_settings


class TestDefaults:
    def test_defaults_are_development_friendly(self) -> None:
        """A fresh clone must run locally without exporting anything."""
        settings = make_settings()
        assert settings.env is Environment.DEVELOPMENT
        assert settings.log_level == "INFO"
        assert settings.database_url.get_secret_value() == DEFAULT_DEV_DATABASE_URL
        assert settings.db_echo is False

    def test_console_logging_by_default_in_development(self) -> None:
        assert make_settings().resolved_log_format == "console"

    def test_json_logging_by_default_outside_development(self) -> None:
        for env in (Environment.STAGING, Environment.PRODUCTION):
            settings = make_settings(
                env=env, database_url=SecretStr("postgresql+asyncpg://u:p@h/db")
            )
            assert settings.resolved_log_format == "json", env

    def test_explicit_log_format_wins_over_env_derivation(self) -> None:
        prod = make_settings(
            env=Environment.PRODUCTION,
            log_format="console",
            database_url=SecretStr("postgresql+asyncpg://u:p@h/db"),
        )
        assert prod.resolved_log_format == "console"
        assert (
            make_settings(env=Environment.DEVELOPMENT, log_format="json").resolved_log_format
            == "json"
        )

    def test_settings_are_immutable(self) -> None:
        settings = make_settings()
        with pytest.raises(ValidationError):
            settings.log_level = "DEBUG"


class TestEnvironmentLoading:
    def test_reads_uppercase_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ENV", "staging")
        monkeypatch.setenv("LOG_LEVEL", "warning")
        monkeypatch.setenv("DB_POOL_SIZE", "11")
        settings = build_settings(env_file=None)
        assert settings.env is Environment.STAGING
        assert settings.log_level == "WARNING"
        assert settings.db_pool_size == 11

    def test_reads_lowercase_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Case-insensitive matching keeps Windows/POSIX shell habits both working."""
        monkeypatch.setenv("log_level", "debug")
        assert build_settings(env_file=None).log_level == "DEBUG"

    def test_unknown_env_vars_are_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOME_FUTURE_TASK_VARIABLE", "x")
        assert build_settings(env_file=None).env is Environment.DEVELOPMENT

    def test_reads_dotenv_file(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text(
            "ENV=staging\nLOG_LEVEL=ERROR\nWORKER_POLL_INTERVAL_SECONDS=12.5\n",
            encoding="utf-8",
        )
        settings = build_settings(env_file=env_file)
        assert settings.env is Environment.STAGING
        assert settings.log_level == "ERROR"
        assert settings.worker_poll_interval_seconds == 12.5

    def test_environment_beats_dotenv_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("LOG_LEVEL=ERROR\n", encoding="utf-8")
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        assert build_settings(env_file=env_file).log_level == "DEBUG"

    def test_get_settings_reads_dotenv_by_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: passing ``_env_file=None`` silently disables ``.env``.

        ``get_settings()`` must omit the argument so a real deployment picks up
        its ``.env`` file; only ``env_file=None`` disables dotenv loading.
        """
        monkeypatch.chdir(tmp_path)
        tmp_path.joinpath(".env").write_text("LOG_LEVEL=ERROR\nENV=staging\n", encoding="utf-8")

        assert get_settings().log_level == "ERROR"
        get_settings.cache_clear()
        assert get_settings(env_file=None).log_level == "INFO"
        get_settings.cache_clear()
        assert build_settings(env_file=None).log_level == "INFO"

    def test_get_settings_is_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOG_LEVEL", "INFO")
        first = get_settings(env_file=None)
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        assert get_settings(env_file=None) is first
        assert reload_settings(env_file=None).log_level == "DEBUG"

    def test_reload_settings_clears_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOG_LEVEL", "INFO")
        first = reload_settings(env_file=None)
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        second = reload_settings(env_file=None)
        assert first is not second
        assert second.log_level == "DEBUG"


class TestValidation:
    @pytest.mark.parametrize("level", ["debug", "INFO", "  Warning  ", "error", "CRITICAL"])
    def test_log_level_normalised_and_accepted(self, level: str) -> None:
        assert make_settings(log_level=level).log_level == level.strip().upper()

    @pytest.mark.parametrize("level", ["VERBOSE", "", "infoo", "TRACE"])
    def test_log_level_rejects_unknown_values(self, level: str) -> None:
        with pytest.raises(ValidationError, match="log_level"):
            make_settings(log_level=level)

    def test_third_party_log_level_defaults_to_warning(self) -> None:
        """Application DEBUG must not drag asyncio/Telethon into DEBUG noise."""
        assert make_settings().third_party_log_level == "WARNING"
        assert make_settings(log_level="DEBUG").third_party_log_level == "WARNING"

    def test_third_party_log_level_is_validated_too(self) -> None:
        assert make_settings(third_party_log_level="debug").third_party_log_level == "DEBUG"
        with pytest.raises(ValidationError, match="third_party_log_level"):
            make_settings(third_party_log_level="NOPE")

    def test_rejects_unknown_environment(self) -> None:
        with pytest.raises(ValidationError):
            make_settings(env="qa")

    def test_rejects_negative_pool_size(self) -> None:
        with pytest.raises(ValidationError):
            make_settings(db_pool_size=-1)

    def test_rejects_non_positive_poll_interval(self) -> None:
        with pytest.raises(ValidationError):
            make_settings(worker_poll_interval_seconds=0)

    def test_tick_timeout_defaults_to_disabled(self) -> None:
        assert make_settings().worker_tick_timeout_seconds == 0.0

    def test_tick_timeout_rejects_negative(self) -> None:
        with pytest.raises(ValidationError):
            make_settings(worker_tick_timeout_seconds=-1)

    def test_rejects_inverted_backoff_bounds(self) -> None:
        with pytest.raises(ValidationError, match="worker_max_error_backoff_seconds"):
            make_settings(worker_error_backoff_seconds=30, worker_max_error_backoff_seconds=5)

    def test_blank_worker_name_becomes_unset(self) -> None:
        assert make_settings(worker_name="   ").worker_name == "unset"


class TestProductionStrictness:
    """Production must refuse to boot on development defaults."""

    def test_rejects_default_database_url(self) -> None:
        with pytest.raises(ValidationError, match="DATABASE_URL"):
            make_settings(env=Environment.PRODUCTION)

    def test_rejects_non_postgres_dsn(self) -> None:
        with pytest.raises(ValidationError, match="PostgreSQL DSN"):
            make_settings(
                env=Environment.PRODUCTION, database_url=SecretStr("sqlite+aiosqlite:///x.db")
            )

    def test_rejects_sql_echo(self) -> None:
        with pytest.raises(ValidationError, match="DB_ECHO"):
            make_settings(
                env=Environment.PRODUCTION,
                database_url=SecretStr("postgresql+asyncpg://u:p@h/db"),
                db_echo=True,
            )

    def test_accepts_valid_production_configuration(self) -> None:
        settings = make_settings(
            env=Environment.PRODUCTION,
            database_url=SecretStr("postgresql+asyncpg://u:p@h:5432/db"),
        )
        assert settings.is_production
        assert settings.resolved_log_format == "json"

    def test_development_may_keep_the_default_dsn(self) -> None:
        assert make_settings(env=Environment.DEVELOPMENT).sqlalchemy_url.startswith(
            "postgresql+asyncpg://"
        )


class TestDatabaseUrlHandling:
    def test_bare_postgres_scheme_is_normalised_to_asyncpg(self) -> None:
        settings = make_settings(database_url=SecretStr("postgresql://u:p@localhost:5432/db"))
        assert settings.sqlalchemy_url == "postgresql+asyncpg://u:p@localhost:5432/db"

    def test_asyncpg_scheme_is_preserved(self) -> None:
        dsn = "postgresql+asyncpg://u:p@localhost:5432/db"
        assert make_settings(database_url=SecretStr(dsn)).sqlalchemy_url == dsn

    def test_only_the_scheme_prefix_is_rewritten(self) -> None:
        settings = make_settings(database_url=SecretStr("postgresql://u:p@h/postgresql_db"))
        assert settings.sqlalchemy_url == "postgresql+asyncpg://u:p@h/postgresql_db"


class TestSecretHygiene:
    """The DSN carries a password; it must never be trivially printable."""

    DSN = "postgresql+asyncpg://mtproto:sup3r-s3cret-pw@localhost:5432/mtproto"

    def test_database_url_is_a_secret_str(self) -> None:
        assert isinstance(make_settings().database_url, SecretStr)

    def test_repr_hides_the_password(self) -> None:
        settings = make_settings(database_url=SecretStr(self.DSN))
        rendered = repr(settings)
        assert "sup3r-s3cret-pw" not in rendered
        assert "**********" in rendered

    def test_str_hides_the_password(self) -> None:
        assert "sup3r-s3cret-pw" not in str(make_settings(database_url=SecretStr(self.DSN)))

    def test_safe_dump_masks_credentials_but_keeps_tunables(self) -> None:
        dumped = make_settings(database_url=SecretStr(self.DSN)).safe_dump()
        assert dumped["database_url"] == "**********"
        assert dumped["db_pool_size"] == 5
        assert dumped["env"] == "development"

    def test_safe_dump_never_contains_the_plain_dsn(self) -> None:
        dumped = make_settings(database_url=SecretStr(self.DSN)).safe_dump()
        assert self.DSN not in str(dumped)


class TestResolvedTestUrl:
    """Which database the integration suite is allowed to touch.

    The migration lifecycle test runs ``downgrade base``, which DROPS EVERY
    TABLE. Deriving the wrong DSN here is destructive, so the resolution rules
    are pinned.
    """

    DEV = "postgresql+asyncpg://mtproto:pw@localhost:5432/mtproto"

    def test_derives_a_test_suffix_from_the_dev_dsn(self) -> None:
        assert make_settings(database_url=self.DEV).resolved_test_url == (
            "postgresql+asyncpg://mtproto:pw@localhost:5432/mtproto_test"
        )

    def test_never_targets_the_dev_database(self) -> None:
        settings = make_settings(database_url=self.DEV)
        assert settings.resolved_test_url != settings.sqlalchemy_url

    def test_explicit_test_dsn_wins(self) -> None:
        explicit = "postgresql+asyncpg://u:p@localhost/explicit_db"
        settings = make_settings(database_url=self.DEV, test_database_url=explicit)
        assert settings.resolved_test_url == explicit

    def test_explicit_test_dsn_wins_even_when_it_looks_like_the_dev_one(self) -> None:
        settings = make_settings(database_url=self.DEV, test_database_url=self.DEV)
        assert settings.resolved_test_url == self.DEV

    def test_explicit_test_dsn_is_normalised_to_asyncpg(self) -> None:
        settings = make_settings(
            database_url=self.DEV, test_database_url="postgresql://u:p@localhost/t"
        )
        assert settings.resolved_test_url.startswith("postgresql+asyncpg://")

    def test_derivation_normalises_a_bare_postgres_prefix(self) -> None:
        settings = make_settings(database_url="postgresql://u:p@localhost:5432/mtproto")
        assert settings.resolved_test_url == (
            "postgresql+asyncpg://u:p@localhost:5432/mtproto_test"
        )

    def test_query_parameters_survive_derivation(self) -> None:
        # asyncpg carries the Unix-socket directory in ?host=; losing it would
        # make the test suite dial a TCP port that has no server on it.
        url = "postgresql+asyncpg://mtproto:pw@/mtproto?host=/var/run/postgresql"
        derived = make_settings(database_url=url).resolved_test_url
        assert derived == "postgresql+asyncpg://mtproto:pw@/mtproto_test?host=/var/run/postgresql"

    def test_multiple_query_parameters_survive(self) -> None:
        url = "postgresql+asyncpg://u:p@h/d?ssl=require&application_name=tester"
        derived = make_settings(database_url=url).resolved_test_url
        assert derived.endswith("/d_test?ssl=require&application_name=tester")

    def test_an_already_suffixed_database_is_not_doubled(self) -> None:
        # Keeps `resolved_test_url` idempotent, so pointing DATABASE_URL at the
        # test database does not produce `mtproto_test_test`.
        url = "postgresql+asyncpg://u:p@localhost/mtproto_test"
        assert make_settings(database_url=url).resolved_test_url == url

    def test_refuses_to_derive_in_production(self) -> None:
        # A production database must never become a test target by inference.
        settings = make_settings(env="production", database_url=self.DEV)
        with pytest.raises(ValueError, match="ENV=production"):
            _ = settings.resolved_test_url

    def test_production_with_an_explicit_test_dsn_is_allowed(self) -> None:
        settings = make_settings(
            env="production", database_url=self.DEV, test_database_url=self.DEV + "_shadow"
        )
        assert settings.resolved_test_url == self.DEV + "_shadow"

    @pytest.mark.parametrize("env", ["development", "staging"])
    def test_derives_outside_production(self, env: str) -> None:
        assert make_settings(env=env, database_url=self.DEV).resolved_test_url.endswith(
            "mtproto_test"
        )

    def test_refuses_a_dsn_with_an_empty_database_path(self) -> None:
        with pytest.raises(ValueError, match="empty path"):
            _ = make_settings(database_url="postgresql+asyncpg://u:p@localhost").resolved_test_url

    def test_env_var_is_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_DATABASE_URL", "postgresql+asyncpg://u:p@localhost/from_env")
        assert build_settings(env_file=None).resolved_test_url.endswith("/from_env")

    def test_test_database_url_is_a_secret_str(self) -> None:
        settings = make_settings(database_url=self.DEV, test_database_url=self.DEV)
        assert isinstance(settings.test_database_url, SecretStr)

    def test_test_database_url_is_masked_in_the_repr(self) -> None:
        # A distinctive password, so the assertion cannot pass by accident
        # through a short substring appearing elsewhere in the repr.
        distinctive = "un1que-test-db-passw0rd"
        settings = make_settings(
            database_url=self.DEV,
            test_database_url=f"postgresql+asyncpg://u:{distinctive}@localhost/t",
        )
        rendered = repr(settings)
        assert distinctive not in rendered
        assert str(settings).find(distinctive) == -1
        assert "**********" in rendered

    def test_safe_dump_masks_the_test_dsn(self) -> None:
        dumped = make_settings(database_url=self.DEV, test_database_url=self.DEV).safe_dump()
        assert dumped["test_database_url"] == "**********"


class TestPoolPrePing:
    def test_defaults_to_true(self) -> None:
        # A stale pooled connection otherwise surfaces as an InterfaceError deep
        # inside a worker loop; pre-ping turns it into a transparent reconnect.
        assert make_settings().db_pool_pre_ping is True

    def test_can_be_disabled(self) -> None:
        assert make_settings(db_pool_pre_ping=False).db_pool_pre_ping is False

    def test_is_read_from_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DB_POOL_PRE_PING", "false")
        assert build_settings(env_file=None).db_pool_pre_ping is False

    def test_rejects_a_non_boolean(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            make_settings(db_pool_pre_ping="maybe")


class TestHideParameters:
    def test_defaults_to_true(self) -> None:
        # SQLAlchemy appends [parameters: (...)] to DBAPI errors and this schema
        # stores an MTProto secret in plaintext, so hiding them is the safe default.
        assert make_settings().db_hide_parameters is True

    def test_can_be_disabled_for_debugging(self) -> None:
        assert make_settings(db_hide_parameters=False).db_hide_parameters is False

    def test_is_read_from_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DB_HIDE_PARAMETERS", "false")
        assert build_settings(env_file=None).db_hide_parameters is False


class TestScorerBatchSize:
    def test_defaults_to_fifty(self) -> None:
        assert make_settings().scorer_batch_size == 50

    def test_is_read_from_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SCORER_BATCH_SIZE", "12")
        assert build_settings(env_file=None).scorer_batch_size == 12

    def test_rejects_non_positive(self) -> None:
        with pytest.raises(ValidationError):
            make_settings(scorer_batch_size=0)


class TestReportingSettings:
    def test_defaults(self) -> None:
        settings = make_settings()
        assert settings.report_default_limit == 20
        assert settings.report_max_limit == 100
        assert settings.report_max_success_age_hours == 6.0

    def test_is_read_from_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REPORT_DEFAULT_LIMIT", "5")
        monkeypatch.setenv("REPORT_MAX_LIMIT", "10")
        monkeypatch.setenv("REPORT_MAX_SUCCESS_AGE_HOURS", "3")
        settings = build_settings(env_file=None)
        assert settings.report_default_limit == 5
        assert settings.report_max_limit == 10
        assert settings.report_max_success_age_hours == 3.0

    def test_rejects_inverted_limits(self) -> None:
        with pytest.raises(ValidationError, match="report_default_limit"):
            make_settings(report_default_limit=50, report_max_limit=10)

    def test_rejects_age_above_lookback(self) -> None:
        with pytest.raises(ValidationError):
            make_settings(report_max_success_age_hours=25)


class TestPublisherSettings:
    def test_defaults_are_unconfigured(self) -> None:
        settings = make_settings()
        assert settings.telegram_bot_token is None
        assert settings.telegram_channel_id is None
        assert settings.publisher_timeout_seconds == 15.0
        assert settings.publisher_connect_timeout_seconds == 5.0

    def test_is_read_from_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:AATestTokenNotARealSecretValue")
        monkeypatch.setenv("TELEGRAM_CHANNEL_ID", "@proxy_channel")
        monkeypatch.setenv("PUBLISHER_TIMEOUT_SECONDS", "9")
        settings = build_settings(env_file=None)
        assert settings.telegram_bot_token is not None
        assert settings.telegram_bot_token.get_secret_value().endswith("SecretValue")
        assert settings.telegram_channel_id == "@proxy_channel"
        assert settings.publisher_timeout_seconds == 9.0
        assert "AATestTokenNotARealSecretValue" not in repr(settings)
        assert settings.safe_dump()["telegram_bot_token"] == "**********"

    def test_blank_token_and_channel_become_none(self) -> None:
        settings = make_settings(telegram_bot_token="  ", telegram_channel_id="  ")
        assert settings.telegram_bot_token is None
        assert settings.telegram_channel_id is None

    def test_rejects_non_positive_timeout(self) -> None:
        with pytest.raises(ValidationError):
            make_settings(publisher_timeout_seconds=0)

    def test_retry_defaults(self) -> None:
        settings = make_settings()
        assert settings.telegram_publication_lease_seconds == 60.0
        assert settings.telegram_max_retries == 8
        assert settings.telegram_retry_base_seconds == 2.0
        assert settings.telegram_retry_max_seconds == 300.0

    def test_retry_overrides_from_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TELEGRAM_PUBLICATION_LEASE_SECONDS", "45")
        monkeypatch.setenv("TELEGRAM_MAX_RETRIES", "3")
        monkeypatch.setenv("TELEGRAM_RETRY_BASE_SECONDS", "1")
        monkeypatch.setenv("TELEGRAM_RETRY_MAX_SECONDS", "10")
        settings = build_settings(env_file=None)
        assert settings.telegram_publication_lease_seconds == 45.0
        assert settings.telegram_max_retries == 3
        assert settings.telegram_retry_base_seconds == 1.0
        assert settings.telegram_retry_max_seconds == 10.0

    def test_rejects_inverted_retry_bounds(self) -> None:
        with pytest.raises(ValidationError, match="telegram_retry_max_seconds"):
            make_settings(telegram_retry_base_seconds=30, telegram_retry_max_seconds=5)

    def test_schedule_defaults(self) -> None:
        settings = make_settings()
        assert settings.telegram_publication_interval_seconds == 300.0
        assert settings.telegram_publication_dedup_seconds == 86400.0
        assert settings.telegram_publication_max_pending == 20

    def test_schedule_overrides_from_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TELEGRAM_PUBLICATION_INTERVAL_SECONDS", "120")
        monkeypatch.setenv("TELEGRAM_PUBLICATION_DEDUP_SECONDS", "3600")
        monkeypatch.setenv("TELEGRAM_PUBLICATION_MAX_PENDING", "5")
        settings = build_settings(env_file=None)
        assert settings.telegram_publication_interval_seconds == 120.0
        assert settings.telegram_publication_dedup_seconds == 3600.0
        assert settings.telegram_publication_max_pending == 5

    def test_rejects_non_positive_interval(self) -> None:
        with pytest.raises(ValidationError):
            make_settings(telegram_publication_interval_seconds=0)

    def test_rejects_negative_dedup(self) -> None:
        with pytest.raises(ValidationError):
            make_settings(telegram_publication_dedup_seconds=-1)

    def test_rejects_non_positive_max_pending(self) -> None:
        with pytest.raises(ValidationError):
            make_settings(telegram_publication_max_pending=0)


class TestApiBind:
    def test_defaults_to_loopback_8080(self) -> None:
        settings = make_settings()
        assert settings.api_host == "127.0.0.1"
        assert settings.api_port == 8080

    def test_is_read_from_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("API_HOST", "0.0.0.0")
        monkeypatch.setenv("API_PORT", "9000")
        settings = build_settings(env_file=None)
        assert settings.api_host == "0.0.0.0"
        assert settings.api_port == 9000

    def test_strips_host_whitespace(self) -> None:
        assert make_settings(api_host="  127.0.0.1  ").api_host == "127.0.0.1"

    def test_rejects_empty_host(self) -> None:
        with pytest.raises(ValidationError, match="api_host"):
            make_settings(api_host="   ")

    @pytest.mark.parametrize("port", [0, -1, 65536])
    def test_rejects_out_of_range_port(self, port: int) -> None:
        with pytest.raises(ValidationError):
            make_settings(api_port=port)


class TestDiscoverySettings:
    def test_defaults_are_idle_and_bounded(self) -> None:
        settings = make_settings()
        assert settings.discovery_sources == ""
        assert settings.discovery_timeout_seconds == 15.0
        assert settings.discovery_connect_timeout_seconds == 5.0
        assert settings.discovery_max_response_bytes == 2 * 1024 * 1024
        assert settings.discovery_max_redirects == 3
        assert settings.discovery_concurrency == 3

    def test_is_read_from_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DISCOVERY_SOURCES", "telegram:ProxyList")
        monkeypatch.setenv("DISCOVERY_TIMEOUT_SECONDS", "9")
        monkeypatch.setenv("DISCOVERY_CONCURRENCY", "2")
        settings = build_settings(env_file=None)
        assert settings.discovery_sources == "telegram:ProxyList"
        assert settings.discovery_timeout_seconds == 9.0
        assert settings.discovery_concurrency == 2

    def test_rejects_non_positive_timeout(self) -> None:
        with pytest.raises(ValidationError):
            make_settings(discovery_timeout_seconds=0)
