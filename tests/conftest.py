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
    "DB_ECHO",
    "SHUTDOWN_GRACE_SECONDS",
    "HEARTBEAT_INTERVAL_SECONDS",
    "WORKER_POLL_INTERVAL_SECONDS",
    "WORKER_ERROR_BACKOFF_SECONDS",
    "WORKER_MAX_ERROR_BACKOFF_SECONDS",
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
