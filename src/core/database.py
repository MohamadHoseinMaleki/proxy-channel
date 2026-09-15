"""Async PostgreSQL infrastructure: engine, sessions, teardown.

Scope is deliberately narrow -- connection management and nothing else. No
repository layer, no unit-of-work abstraction, no query helpers. Queries live with
the code that owns them (Task 003 onwards) so the SQL stays visible.

Process model
-------------
The platform runs four independent OS processes (discovery, tester, scorer, and
later publisher). Each builds **its own** :class:`Database` at startup and disposes
it at shutdown. There is no module-level engine singleton: a global would be
per-process anyway, but keeping it explicit removes any temptation to share state
across processes and avoids binding a connection pool to an event loop that a
later ``asyncio.run()`` has already replaced.

Transaction discipline
----------------------
:func:`Database.session_scope` commits on success and rolls back on any exception.
It is meant for **short** transactions. The tester must never hold one open across
an MTProto network timeout: claim rows in one transaction, commit, do the network
I/O, then write results in a second transaction (see docs/DECISION_LOG.md D-024).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from core.config import Settings, get_settings
from core.logger import get_logger, scrub_secrets

__all__ = [
    "DEFAULT_HIDE_PARAMETERS",
    "Database",
    "database_exists",
]

_SUPPORTED_PREFIXES = ("postgresql+asyncpg://", "postgresql://")

#: ``create_async_engine`` needs an explicit async driver. A bare
#: ``postgresql://`` DSN resolves to psycopg2, which is not installed, and fails
#: with a ``ModuleNotFoundError`` that says nothing useful about the real problem.
_ASYNC_PREFIX = "postgresql+asyncpg://"
_BARE_PREFIX = "postgresql://"


def _as_async_dsn(url: str) -> str:
    """Rewrite a bare ``postgresql://`` DSN to the asyncpg driver.

    Mirrors :attr:`core.config.Settings.sqlalchemy_url` so the class is correct
    regardless of entry point: a DSN pasted from a README or an orchestrator
    secret rarely names the driver, and accepting it silently then failing on a
    missing psycopg2 would be the worst of both behaviours.
    """
    if url.startswith(_BARE_PREFIX):
        return _ASYNC_PREFIX + url[len(_BARE_PREFIX) :]
    return url


_logger = get_logger("core.database")


#: Default for ``hide_parameters``; see :attr:`Database.DEFAULT_HIDE_PARAMETERS`.
DEFAULT_HIDE_PARAMETERS = True


class Database:
    """One process-local async engine plus its session factory.

    Build once per process, reuse for every query, dispose at shutdown::

        db = Database.from_settings()
        try:
            async with db.session_scope() as session:
                ...
        finally:
            await db.dispose()

    or simply ``async with Database.from_settings() as db:``.
    """

    #: Bind parameters are hidden from exception messages by default.
    #:
    #: SQLAlchemy appends ``[parameters: (...)]`` to every DBAPI error, and this
    #: schema stores an MTProto secret in plaintext, so a failing INSERT would
    #: otherwise put that secret into an exception string that gets logged and
    #: persisted to ``proxy_observations.error_message_safe``. It is only half the
    #: defence -- PostgreSQL's own ``DETAIL: Failing row contains (...)`` is not
    #: affected by this option and is handled by the hex scrubber in
    #: :mod:`core.logger` -- so both must stay in place.
    #:
    #: Flip it off locally when debugging a query; never in production.
    def __init__(
        self,
        url: str,
        *,
        pool_size: int = 5,
        max_overflow: int = 5,
        pool_timeout: float = 30.0,
        pool_recycle: int = 1800,
        pool_pre_ping: bool = True,
        hide_parameters: bool = True,
        echo: bool = False,
        connect_args: dict[str, Any] | None = None,
    ) -> None:
        if not url.startswith(_SUPPORTED_PREFIXES):
            # Raised without echoing the value: an unsupported URL may still
            # carry credentials.
            msg = (
                "Database URL must be a PostgreSQL DSN "
                f"({' or '.join(_SUPPORTED_PREFIXES)}); got {scrub_secrets(url)!r}"
            )
            raise ValueError(msg)

        self._url = _as_async_dsn(url)
        self._engine: AsyncEngine = create_async_engine(
            self._url,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_timeout=pool_timeout,
            pool_recycle=pool_recycle,
            pool_pre_ping=pool_pre_ping,
            hide_parameters=hide_parameters,
            echo=echo,
            connect_args=connect_args or {},
        )
        # expire_on_commit=False: worker code reads attributes after commit, and
        # an expiring session would emit a lazy refresh -- an implicit round trip
        # that fails outright under asyncio.
        self._session_factory = async_sessionmaker(
            bind=self._engine,
            expire_on_commit=False,
            autoflush=False,
            class_=AsyncSession,
        )

    # -- construction -------------------------------------------------------

    @classmethod
    def from_settings(cls, settings: Settings | None = None, *, for_test: bool = False) -> Database:
        """Build a :class:`Database` from :class:`~core.config.Settings`.

        ``for_test=True`` targets :attr:`Settings.resolved_test_url`, which never
        points at the development database by default and refuses to be derived at
        all under ``ENV=production``.
        """
        cfg = settings or get_settings()
        url = cfg.resolved_test_url if for_test else cfg.sqlalchemy_url
        return cls(
            url,
            pool_size=cfg.db_pool_size,
            max_overflow=cfg.db_max_overflow,
            pool_timeout=cfg.db_pool_timeout_seconds,
            pool_recycle=cfg.db_pool_recycle_seconds,
            pool_pre_ping=cfg.db_pool_pre_ping,
            hide_parameters=cfg.db_hide_parameters,
            echo=cfg.db_echo,
        )

    # -- accessors ----------------------------------------------------------

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory

    @property
    def safe_url(self) -> str:
        """The DSN with its password masked -- the only loggable form."""
        return scrub_secrets(self._url)

    def session(self) -> AsyncSession:
        """A bare session. The caller owns commit/rollback/close."""
        return self._session_factory()

    @asynccontextmanager
    async def session_scope(self) -> AsyncIterator[AsyncSession]:
        """A short transaction: commit on success, roll back on any error.

        Not for use across network I/O. See the module docstring.
        """
        session = self._session_factory()
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
        finally:
            await session.close()

    # -- lifecycle ----------------------------------------------------------

    async def ping(self) -> None:
        """Round-trip ``SELECT 1``.

        Raises ``SQLAlchemyError`` **or** ``OSError`` on failure. The latter is
        not hypothetical: a refused TCP connection surfaces as a bare
        ``ConnectionRefusedError`` rather than being wrapped by the dialect, so
        callers must not assume ``except SQLAlchemyError`` is sufficient.
        :meth:`is_reachable` catches both and is the safe entry point.
        """
        async with self._engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    async def is_reachable(self) -> bool:
        """Whether the database answered a trivial query. Never raises."""
        try:
            await self.ping()
        except (SQLAlchemyError, OSError, ValueError) as exc:
            _logger.debug(
                "database_unreachable", url=self.safe_url, exception_type=type(exc).__name__
            )
            return False
        return True

    async def dispose(self) -> None:
        """Close every pooled connection. Safe to call more than once."""
        await self._engine.dispose()
        _logger.debug("database_disposed", url=self.safe_url)

    async def __aenter__(self) -> Database:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.dispose()

    def __repr__(self) -> str:
        return f"<Database url={self.safe_url!r} pool={self._engine.pool!r}>"


async def database_exists(url: str, database: str) -> bool:
    """Whether ``database`` exists on the server named by ``url``.

    Connects to the ``postgres`` maintenance database and consults
    ``pg_database``. Used by test fixtures to skip cleanly, and by the
    development provisioning script to avoid re-creating a database.

    Never raises. A missing maintenance database, an unreachable server and an
    unrecognised DSN are all reported as ``False``, because callers use the
    answer to decide whether to skip and a fixture that throws during
    collection is worse than one that skips.
    """
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    maintenance = urlunsplit(parts._replace(path="/postgres"))
    try:
        probe = Database(maintenance, pool_size=1, max_overflow=0)
    except ValueError:
        # A DSN this module does not understand is still "we cannot confirm the
        # database exists". Callers are test fixtures deciding whether to skip,
        # so raising here would turn a clean skip into a collection error.
        return False
    try:
        async with probe.engine.connect() as conn:
            result = await conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": database},
            )
            return result.scalar_one_or_none() is not None
    except (SQLAlchemyError, OSError, ValueError):
        return False
    finally:
        await probe.dispose()
