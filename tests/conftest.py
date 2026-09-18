"""Shared pytest fixtures.

Design goals:

* **Hermetic configuration.** No test may accidentally inherit a developer's
  ``.env`` file or a stale exported environment variable.
* **No network, no database.** Unit tests never touch a real PostgreSQL or a
  real proxy. Integration tests opt in via the ``integration`` marker and live
  network tests via the ``live`` marker (never enabled in CI).
* **Deterministic logging capture.** ``configure_logging`` is forced per test so
  assertions can be made on the exact rendered output.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import structlog

from core.config import Settings, build_settings, get_settings
from core.logger import configure_logging

#: Environment variables understood by :class:`core.config.Settings`. Cleared
#: before every test so the host environment cannot influence assertions.
CONFIG_ENV_VARS = (
    "ENV",
    "LOG_LEVEL",
    "THIRD_PARTY_LOG_LEVEL",
    "LOG_FORMAT",
    "WORKER_NAME",
    "DATABASE_URL",
    "DB_POOL_SIZE",
    "DB_MAX_OVERFLOW",
    "DB_POOL_TIMEOUT_SECONDS",
    "DB_POOL_RECYCLE_SECONDS",
    "DB_POOL_PRE_PING",
    "DB_HIDE_PARAMETERS",
    "DB_ECHO",
    "TEST_DATABASE_URL",
    "SHUTDOWN_GRACE_SECONDS",
    "HEARTBEAT_INTERVAL_SECONDS",
    "WORKER_POLL_INTERVAL_SECONDS",
    "WORKER_ERROR_BACKOFF_SECONDS",
    "WORKER_MAX_ERROR_BACKOFF_SECONDS",
    "WORKER_TICK_TIMEOUT_SECONDS",
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "TESTER_CONCURRENCY",
    "TESTER_TCP_TIMEOUT_SECONDS",
    "TESTER_MTPROTO_TIMEOUT_SECONDS",
    "TESTER_TOTAL_TIMEOUT_SECONDS",
    "TESTER_BATCH_SIZE",
    "SCORER_BATCH_SIZE",
    "API_HOST",
    "API_PORT",
    "DISCOVERY_SOURCES",
    "DISCOVERY_TIMEOUT_SECONDS",
    "DISCOVERY_CONNECT_TIMEOUT_SECONDS",
    "DISCOVERY_MAX_RESPONSE_BYTES",
    "DISCOVERY_MAX_REDIRECTS",
    "DISCOVERY_CONCURRENCY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHANNEL_ID",
    "PUBLISHER_TIMEOUT_SECONDS",
    "PUBLISHER_CONNECT_TIMEOUT_SECONDS",
    "TELEGRAM_PUBLICATION_LEASE_SECONDS",
    "TELEGRAM_MAX_RETRIES",
    "TELEGRAM_RETRY_BASE_SECONDS",
    "TELEGRAM_RETRY_MAX_SECONDS",
    "TELEGRAM_PUBLICATION_INTERVAL_SECONDS",
    "TELEGRAM_PUBLICATION_DEDUP_SECONDS",
    "TELEGRAM_PUBLICATION_MAX_PENDING",
    "REPORT_DEFAULT_LIMIT",
    "REPORT_MAX_LIMIT",
    "REPORT_MAX_SUCCESS_AGE_HOURS",
)


def make_settings(**overrides: Any) -> Settings:
    """Build a ``Settings`` that ignores any ``.env`` on disk."""
    return build_settings(env_file=None, **overrides)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Strip configuration environment variables for the duration of a test."""
    for key in CONFIG_ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture(autouse=True)
def clean_settings_cache() -> Iterator[None]:
    """Ensure ``get_settings()`` never carries state between tests."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def clean_contextvars() -> Iterator[None]:
    """Reset structlog contextvars so bound keys cannot leak between tests."""
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


@pytest.fixture
def json_logs(capsys: pytest.CaptureFixture[str]) -> pytest.CaptureFixture[str]:
    """Configure JSON logging and hand back ``capsys`` for output assertions."""
    configure_logging(make_settings(log_format="json", log_level="DEBUG"), force=True)
    return capsys


@pytest.fixture
def console_logs(capsys: pytest.CaptureFixture[str]) -> pytest.CaptureFixture[str]:
    """Configure human-readable console logging for output assertions."""
    configure_logging(make_settings(log_format="console", log_level="DEBUG"), force=True)
    return capsys


# ---------------------------------------------------------------------------
# Typed accessors for SQLAlchemy internals
#
# These exist so the test suite can assert on engine and schema details without
# scattering `type: ignore` comments. Each one narrows a real runtime type that
# SQLAlchemy's own annotations describe more loosely.
# ---------------------------------------------------------------------------


def queue_pool(engine: Any) -> Any:
    """Narrow ``engine.pool`` to ``QueuePool``.

    ``size()``, ``checkedout()`` and the overflow/timeout/recycle settings live on
    ``QueuePool``; ``engine.pool`` is annotated as the ``Pool`` base class, which
    does not declare them. Every engine this project builds uses a QueuePool.
    """
    from sqlalchemy.pool import QueuePool

    pool = engine.pool
    assert isinstance(pool, QueuePool), f"expected QueuePool, got {type(pool).__name__}"
    return pool


def table_of(model: type[Any]) -> Any:
    """The ``Table`` for a declarative model, reached through metadata.

    ``Model.__table__`` is annotated as ``FromClause``, so ``.indexes`` and
    ``.constraints`` are invisible to mypy. ``MetaData.tables`` returns a proper
    ``Table``.
    """
    from sqlalchemy import Table

    table = model.metadata.tables[model.__tablename__]
    assert isinstance(table, Table)
    return table


def string_length(column: Any) -> int:
    """The declared width of a string column.

    ``Column.type`` is annotated as ``TypeEngine``, which has no ``length``.
    """
    from sqlalchemy import String

    assert isinstance(column.type, String), f"{column.name} is {type(column.type).__name__}"
    return int(column.type.length or 0)
