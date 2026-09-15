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
