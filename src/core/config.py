"""Application configuration.

Single source of truth for every tunable in the platform. Values are loaded from
the process environment and, for local development, from a ``.env`` file.

Security rule: anything that is a credential is typed as :class:`pydantic.SecretStr`
so that it can never leak through ``repr()``, ``str()`` or a naive
``model_dump()``.  See :mod:`core.logger` for the matching log-redaction layer.
"""

from __future__ import annotations

import os
from enum import StrEnum
from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "EnvFile",
    "Environment",
    "LogFormat",
    "Settings",
    "build_settings",
    "get_settings",
    "reload_settings",
]

#: Default local-development DSN. Never valid in production (see validator below).
DEFAULT_DEV_DATABASE_URL = "postgresql+asyncpg://mtproto:mtproto@localhost:5432/mtproto"

_VALID_LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})


def _normalise_async_url(url: str) -> str:
    """Rewrite a bare ``postgresql://`` DSN to the asyncpg driver."""
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def _derive_test_url(url: str) -> str:
    """Append ``_test`` to the database component of a DSN.

    Handled with :mod:`urllib.parse` rather than string surgery because DSNs may
    carry query parameters (asyncpg uses ``?host=<socket dir>`` for Unix sockets)
    and those must survive untouched.
    """
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    database = (parts.path or "/").lstrip("/")
    if not database:
        msg = f"Cannot derive a test database name from DSN with an empty path: {url!r}"
        raise ValueError(msg)
    if database.endswith("_test"):
        return url
    return urlunsplit(parts._replace(path=f"/{database}_test"))


class Environment(StrEnum):
    """Deployment environment. Drives logging format and config strictness."""

    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


LogFormat = Literal["json", "console"]


class Settings(BaseSettings):
    """Platform settings.

    Environment variables are matched case-insensitively, so ``LOG_LEVEL`` and
    ``log_level`` are equivalent.  Unknown variables are ignored on purpose: the
    same ``.env`` file is shared by the worker processes and the ranking API,
    and no single process consumes all of it.
    """

    model_config = SettingsConfigDict(
        env_file=(".env",),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    # --- Runtime -----------------------------------------------------------
    env: Environment = Environment.DEVELOPMENT
    log_level: str = Field(default="INFO")
    #: Level for noisy third-party loggers (asyncio, SQLAlchemy, Telethon,
    #: asyncpg). Kept separate so application ``DEBUG`` does not drown the
    #: structured event stream in library chatter.
    third_party_log_level: str = Field(default="WARNING")
    #: ``None`` means "derive from ``env``": console for development, JSON otherwise.
    log_format: LogFormat | None = Field(default=None)
    worker_name: str = Field(default="unset")

    # --- Database ----------------------------------------------------------
    database_url: SecretStr = Field(default=SecretStr(DEFAULT_DEV_DATABASE_URL))
    db_pool_size: int = Field(default=5, ge=0, le=100)
    db_max_overflow: int = Field(default=5, ge=0, le=100)
    db_pool_timeout_seconds: float = Field(default=30.0, gt=0)
    #: Recycle connections before PostgreSQL/managed providers drop them silently.
    db_pool_recycle_seconds: int = Field(default=1800, ge=-1)
    #: Liveness check on pool checkout. Long-lived workers against managed
    #: PostgreSQL routinely find connections the provider already closed.
    db_pool_pre_ping: bool = Field(default=True)
    #: Keep bind parameters out of SQLAlchemy exception messages. This schema
    #: stores an MTProto secret in plaintext, so a failing INSERT would otherwise
    #: put it in an exception string that gets logged and persisted. Debug only.
    db_hide_parameters: bool = Field(default=True)
    db_echo: bool = Field(default=False)

    #: Separate DSN for the ``integration`` test suite. The migration lifecycle
    #: test runs ``downgrade base``, which DROPS EVERY TABLE, so it must never
    #: point at a working database by accident. When unset it is derived from
    #: ``database_url`` by suffixing the database name with ``_test``.
    test_database_url: SecretStr | None = Field(default=None)

    # --- Worker lifecycle --------------------------------------------------
    shutdown_grace_seconds: float = Field(default=10.0, ge=0)
    #: ``0`` disables heartbeat logging entirely (used by fast unit tests).
    heartbeat_interval_seconds: float = Field(default=60.0, ge=0)
    worker_poll_interval_seconds: float = Field(default=5.0, gt=0)
    worker_error_backoff_seconds: float = Field(default=2.0, ge=0)
    worker_max_error_backoff_seconds: float = Field(default=60.0, ge=0)

    # --- Telegram & Tester (Task 004) --------------------------------------
    telegram_api_id: int | None = Field(default=None)
    telegram_api_hash: SecretStr | None = Field(default=None)
    tester_concurrency: int = Field(default=10, ge=1, le=100)
    tester_tcp_timeout_seconds: float = Field(default=3.0, gt=0)
    tester_mtproto_timeout_seconds: float = Field(default=8.0, gt=0)
    tester_total_timeout_seconds: float = Field(default=15.0, gt=0)
    tester_batch_size: int = Field(default=25, ge=1, le=200)

    # --- Scorer (Task 005) -------------------------------------------------
    #: How many proxies to score per tick. Formula constants live in
    #: ``modules.scoring.calculator`` so a settings change cannot silently
    #: fork ``scoring_version=v1``.
    scorer_batch_size: int = Field(default=50, ge=1, le=500)

    # --- HTTP ranking API (Task 007) ---------------------------------------
    #: Loopback by default. Binding ``0.0.0.0`` is an operator choice, not the MVP.
    api_host: str = Field(default="127.0.0.1")
    api_port: int = Field(default=8080, ge=1, le=65535)

    # --- Validation --------------------------------------------------------

    @field_validator("log_level", "third_party_log_level", mode="before")
    @classmethod
    def _normalise_log_level(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().upper()
        return value

    @field_validator("log_level", "third_party_log_level")
    @classmethod
    def _check_log_level(cls, value: str, info: Any) -> str:
        if value not in _VALID_LOG_LEVELS:
            field = info.field_name
            msg = f"{field} must be one of {sorted(_VALID_LOG_LEVELS)}, got {value!r}"
            raise ValueError(msg)
        return value

    @field_validator("worker_name", mode="before")
    @classmethod
    def _normalise_worker_name(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            return value or "unset"
        return value

    @field_validator("api_host", mode="before")
    @classmethod
    def _normalise_api_host(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                msg = "api_host must not be empty"
                raise ValueError(msg)
            return stripped
        return value

    @field_validator("worker_max_error_backoff_seconds")
    @classmethod
    def _check_backoff_ordering(cls, value: float, info: Any) -> float:
        minimum = info.data.get("worker_error_backoff_seconds", 0.0)
        if value < minimum:
            msg = "worker_max_error_backoff_seconds must be >= worker_error_backoff_seconds"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _check_production_ready(self) -> Settings:
        """Fail fast on misconfiguration that is only dangerous in production."""
        if self.env is not Environment.PRODUCTION:
            return self

        url = self.database_url.get_secret_value()
        if url == DEFAULT_DEV_DATABASE_URL or not url:
            msg = (
                "DATABASE_URL must be set to a real PostgreSQL DSN when ENV=production; "
                "the bundled development default is not permitted."
            )
            raise ValueError(msg)
        if not url.startswith(("postgresql+asyncpg://", "postgresql://")):
            msg = "DATABASE_URL must be a PostgreSQL DSN (postgresql+asyncpg://...)."
            raise ValueError(msg)
        if self.db_echo:
            msg = "DB_ECHO must be disabled in production: SQL echoing can leak data to logs."
            raise ValueError(msg)
        return self

    # --- Derived helpers ---------------------------------------------------

    @property
    def is_production(self) -> bool:
        return self.env is Environment.PRODUCTION

    @property
    def resolved_log_format(self) -> LogFormat:
        """Console output while developing, structured JSON everywhere else."""
        if self.log_format is not None:
            return self.log_format
        return "console" if self.env is Environment.DEVELOPMENT else "json"

    @property
    def sqlalchemy_url(self) -> str:
        """The DSN as a plain string, for handing to SQLAlchemy only.

        Deliberately not exposed via ``__repr__`` or ``safe_dump``.
        """
        # Normalise to the async driver; asyncpg is the only supported engine.
        return _normalise_async_url(self.database_url.get_secret_value())

    @property
    def resolved_test_url(self) -> str:
        """The DSN integration tests should use, as a plain string.

        Resolution order:

        1. ``TEST_DATABASE_URL`` when set -- explicit always wins.
        2. Otherwise ``DATABASE_URL`` with ``_test`` appended to the database
           name, so a contributor's development data is never the target.

        Refuses to resolve at all when ``ENV=production`` and no explicit test
        DSN was given: a production database must never be a test target by
        inference.
        """
        if self.test_database_url is not None:
            return _normalise_async_url(self.test_database_url.get_secret_value())

        url = self.sqlalchemy_url
        if self.is_production:
            msg = (
                "TEST_DATABASE_URL must be set explicitly when ENV=production; "
                "refusing to derive a test DSN from a production database."
            )
            raise ValueError(msg)
        return _derive_test_url(url)

    def safe_dump(self) -> dict[str, Any]:
        """A loggable view of the settings with every credential masked."""
        raw: dict[str, Any] = self.model_dump()
        for key, value in raw.items():
            if isinstance(value, SecretStr):
                raw[key] = "**********"
            elif isinstance(value, Environment):
                raw[key] = str(value)
        return raw


class _Unset:
    """Sentinel distinguishing "argument omitted" from an explicit ``None``."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<unset>"


_UNSET = _Unset()

#: Accepted forms of the ``env_file`` argument.
#:
#: * omitted -- use ``model_config["env_file"]``, i.e. read ``.env`` if present;
#: * ``None`` -- explicitly disable dotenv loading (hermetic tests, tooling);
#: * a path -- load that specific file.
EnvFile = str | os.PathLike[str] | None | _Unset

#: Hashable subset of :data:`EnvFile`. ``lru_cache`` keys must be hashable, so
#: the cached accessor only accepts ``str`` paths; ``os.fspath`` normalises the
#: rest in :func:`reload_settings`.
_HashableEnvFile = str | None | _Unset


def build_settings(*, env_file: str | os.PathLike[str] | None = None, **overrides: Any) -> Settings:
    """Construct :class:`Settings` with dotenv loading disabled by default.

    ``env_file=None`` turns ``.env`` discovery *off* -- verified behaviour of
    pydantic-settings, not an assumption. That is exactly what tests want, but it
    is **not** what the worker entrypoints want, so :func:`get_settings` omits the
    argument instead of passing ``None``.

    This wrapper exists because pydantic-settings exposes the control as the
    underscore-prefixed ``_env_file`` init keyword, which type checkers do not
    model; the single suppression lives here rather than scattered around.
    """
    return Settings(_env_file=env_file, **overrides)  # type: ignore[call-arg]


@lru_cache(maxsize=1)
def get_settings(*, env_file: _HashableEnvFile = _UNSET) -> Settings:
    """Return the process-wide cached settings, reading ``.env`` when present.

    The cache keeps the three worker processes from re-parsing ``.env`` on every
    call. Tests should use :func:`reload_settings` or :func:`build_settings`
    rather than mutating this cache.
    """
    if isinstance(env_file, _Unset):
        return Settings()
    return build_settings(env_file=env_file)


def reload_settings(*, env_file: EnvFile = _UNSET) -> Settings:
    """Drop the cache and re-read configuration from the environment."""
    get_settings.cache_clear()
    if isinstance(env_file, _Unset):
        return get_settings()
    key: str | None = None if env_file is None else os.fspath(env_file)
    return get_settings(env_file=key)
