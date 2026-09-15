"""Alembic migration environment (async SQLAlchemy + asyncpg).

Two deliberate deviations from the stock template:

* **No DSN in ``alembic.ini``.** Credentials are resolved here from
  :class:`core.config.Settings`, so nothing sensitive is ever tracked in git and
  there is no second source of truth to drift.
* **No ``fileConfig()``.** The template's logging configuration would replace the
  root handlers installed by :mod:`core.logger`, so migration output would stop
  being structured JSON like the rest of the platform. We configure logging
  through the same code path the workers use instead.
"""

from __future__ import annotations

import asyncio
from typing import Any

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from core.config import Settings, build_settings
from core.logger import configure_logging, get_logger, scrub_secrets
from core.models import Base

_logger = get_logger("alembic.env")

#: Autogenerate should notice column-type and server-default drift, not just
#: added/dropped tables. Without these it silently produces incomplete migrations.
_COMPARE_OPTIONS: dict[str, Any] = {
    "compare_type": True,
    "compare_server_default": True,
}


def _resolve_url(settings: Settings, x_arguments: dict[str, str]) -> str:
    """Pick the target DSN.

    Precedence: explicit ``-x url=`` > ``-x test=1`` > ``DATABASE_URL``/.env.
    """
    explicit = x_arguments.get("url")
    if explicit:
        return explicit
    if x_arguments.get("test", "").lower() in {"1", "true", "yes"}:
        return settings.resolved_test_url
    return settings.sqlalchemy_url


def _run_migrations_offline(url: str) -> None:
    """Emit SQL to stdout without connecting (``alembic upgrade head --sql``)."""
    context.configure(
        url=url,
        target_metadata=Base.metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **_COMPARE_OPTIONS,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations_sync(connection: Connection) -> None:
    """The synchronous half, executed inside ``run_sync`` on a worker thread."""
    context.configure(connection=connection, target_metadata=Base.metadata, **_COMPARE_OPTIONS)
    with context.begin_transaction():
        context.run_migrations()


async def _run_migrations_online(url: str) -> None:
    """Connect with a NullPool: a migration run is short-lived and needs exactly
    one connection, never a pool that could outlive the process."""
    connectable = create_async_engine(url, poolclass=pool.NullPool)
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(_run_migrations_sync)
    finally:
        await connectable.dispose()


def main() -> None:
    settings = build_settings()
    configure_logging(settings, force=True)

    url = _resolve_url(settings, context.get_x_argument(as_dictionary=True))
    _logger.info("alembic_run_started", url=scrub_secrets(url), offline=context.is_offline_mode())

    if context.is_offline_mode():
        _run_migrations_offline(url)
    else:
        asyncio.run(_run_migrations_online(url))


main()
