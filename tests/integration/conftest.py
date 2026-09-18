"""Fixtures for the integration suite: a real PostgreSQL, really exercised.

Scope
-----
Everything in this package requires a live server. Tests **skip** with an
actionable message when one is absent rather than failing, so ``uv run pytest``
stays green on a machine that has not provisioned a database -- but they never
skip silently when a database *is* reachable.

To run them::

    uv run --with pgserver python scripts/dev_pg.py run -- uv run pytest -m integration

or point ``TEST_DATABASE_URL`` at any PostgreSQL you like.

Design notes
------------
* **The URL is captured at import time.** The root ``conftest.py`` has an autouse
  fixture that strips ``DATABASE_URL``/``TEST_DATABASE_URL`` from the environment
  so unit tests stay hermetic. Reading the settings here, at module import, means
  the integration suite is unaffected by that and the unit suite is unaffected by
  a developer's exported credentials.
* **Migration and reachability work is synchronous.** Wrapping it in
  ``asyncio.run`` avoids session-scoped event loops entirely, which is the main
  source of pytest-asyncio loop-scope pain.
* **Tables are truncated per test, not rolled back.** A savepoint rollback cannot
  exercise ``FOR UPDATE SKIP LOCKED`` across concurrent sessions, which is the
  single most important behaviour in this layer.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Generator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import build_settings
from core.database import Database, database_exists
from core.logger import scrub_secrets

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from core.models import Proxy

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Read once at import, before any fixture can clear the environment.
_BOOT_SETTINGS = build_settings()


def _database_name(url: str) -> str:
    return (urlsplit(url).path or "").lstrip("/")


def _resolve_test_url() -> str | None:
    """The integration DSN, or ``None`` if it cannot be determined safely."""
    try:
        return _BOOT_SETTINGS.resolved_test_url
    except ValueError:
        # ENV=production without an explicit TEST_DATABASE_URL: refusing is the
        # correct behaviour, and here it means "skip", not "fail".
        return None


_TEST_URL = _resolve_test_url()

#: Tables truncated between tests. Listed explicitly rather than discovered so a
#: new table cannot quietly escape isolation.
TABLES = (
    "proxy_publications",
    "proxy_scores",
    "proxy_observations",
    "proxy_discoveries",
    "proxies",
)


def _run_alembic(database_url: str, *arguments: str, allow_failure: bool = False) -> str:
    """Run the Alembic CLI against ``database_url`` and return its output.

    The DSN travels in the environment rather than in ``argv``, so it never shows
    up in ``ps`` output or in a CI job log of the command line.
    """
    env = {**os.environ, "TEST_DATABASE_URL": database_url}
    completed = subprocess.run(  # noqa: S603 - fixed executable, no shell
        [sys.executable, "-m", "alembic", "-x", "test=1", *arguments],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    output = f"{completed.stdout}\n{completed.stderr}"
    if completed.returncode != 0 and not allow_failure:
        msg = (
            f"alembic {' '.join(arguments)} failed against {scrub_secrets(database_url)}\n{output}"
        )
        raise RuntimeError(msg)
    return output


def _maintenance_url(url: str) -> str:
    """The same DSN pointed at the ``postgres`` maintenance database.

    ``CREATE``/``DROP DATABASE`` cannot run inside a transaction block against
    the target database itself, so they go through here in AUTOCOMMIT.
    """
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path="/postgres"))


@asynccontextmanager
async def _autocommit(url: str) -> AsyncIterator[object]:
    probe = Database(_maintenance_url(url), pool_size=1, max_overflow=0)
    try:
        async with probe.engine.connect() as connection:
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            yield connection
    finally:
        await probe.dispose()


async def create_database(url: str, database: str) -> None:
    """Create ``database``. Callers must have the CREATEDB privilege."""
    async with _autocommit(url) as connection:
        # Identifiers cannot be bind parameters, so the name is quote-escaped.
        # It always comes from this module or a fixture, never from user input.
        quoted = database.replace('"', '""')
        await connection.execute(text(f'CREATE DATABASE "{quoted}"'))  # type: ignore[attr-defined]


async def drop_database(url: str, database: str) -> None:
    """Drop ``database``, disconnecting anything still attached.

    ``WITH (FORCE)`` matters: a pooled connection left open by an earlier test
    would otherwise make the DROP fail and leave the database behind.
    """
    async with _autocommit(url) as connection:
        quoted = database.replace('"', '""')
        await connection.execute(  # type: ignore[attr-defined]
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :name AND pid <> pg_backend_pid()"
            ),
            {"name": database},
        )
        await connection.execute(text(f'DROP DATABASE IF EXISTS "{quoted}"'))  # type: ignore[attr-defined]


def url_for_database(url: str, database: str) -> str:
    """The same DSN pointed at a different database."""
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{database}"))


async def _ensure_database(url: str) -> tuple[bool, str]:
    """Create the test database if the server is up but the database is not.

    Returns ``(available, reason)``.
    """
    database = _database_name(url)
    if not database:
        return False, f"DSN {scrub_secrets(url)!r} names no database"

    if await database_exists(url, database):
        return True, "exists"

    try:
        await create_database(url, database)
    except (SQLAlchemyError, OSError, ValueError) as exc:
        return False, (
            f"database {database!r} does not exist and could not be created "
            f"({type(exc).__name__}). Create it, grant CREATEDB, or run "
            f"scripts/dev_pg.py up."
        )
    return True, "created"


@pytest.fixture(scope="session")
def test_database_url() -> str:
    """A reachable test DSN, or a skip with instructions."""
    if _TEST_URL is None:
        pytest.skip(
            "no test database: set TEST_DATABASE_URL (or DATABASE_URL outside "
            "production). `uv run --with pgserver python scripts/dev_pg.py run -- "
            "uv run pytest -m integration` provisions one for you."
        )
    available, reason = asyncio.run(_ensure_database(_TEST_URL))
    if not available:
        pytest.skip(f"PostgreSQL unavailable: {reason}")
    return _TEST_URL


@pytest.fixture(scope="session", autouse=True)
def migrated_schema(test_database_url: str) -> Iterator[str]:
    """Bring the test database to ``head`` once per session.

    Runs the real Alembic CLI rather than ``Base.metadata.create_all``: the point
    of these tests is that the *migration* produces a working schema. Using
    ``create_all`` would let the migration rot undetected while every test still
    passed.
    """
    _run_alembic(test_database_url, "upgrade", "head")
    yield test_database_url


@pytest.fixture
async def db(test_database_url: str) -> AsyncIterator[Database]:
    """A per-test engine, disposed afterwards.

    Not session-scoped on purpose: an ``AsyncEngine`` binds its pool to the loop
    it was created on, and the default fixture loop scope is ``function``.
    """
    database = Database(test_database_url, pool_size=5, max_overflow=5, pool_timeout=10.0)
    try:
        yield database
    finally:
        await database.dispose()


@pytest.fixture(autouse=True)
async def clean_tables(db: Database) -> AsyncIterator[None]:
    """Empty every table before each test and reset identity sequences.

    ``CASCADE`` is required because ``proxy_observations`` is ``ON DELETE
    RESTRICT`` against ``proxies`` -- which is itself an assertion worth making
    elsewhere. ``RESTART IDENTITY`` keeps generated ids predictable.
    """
    async with db.session_scope() as session:
        await session.execute(text(f"TRUNCATE TABLE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture
async def session(db: Database) -> AsyncIterator[AsyncSession]:
    """A session for tests that manage their own transactions.

    Commits on clean exit and rolls back on any error, so a test that leaves a
    rejected statement behind cannot poison the next one.
    """
    async with db.session_scope() as active:
        yield active


def make_proxy(
    *,
    server: str = "proxy.example.com",
    port: int = 443,
    secret: str = "ee" + "a1" * 15,
    protocol: str = "mtproto",
    **overrides: object,
) -> Proxy:
    """Build a :class:`~core.models.Proxy` with a valid fingerprint.

    The fingerprint is computed rather than faked, so uniqueness holds for free.
    Pass ``fingerprint=`` explicitly to bypass that computation -- necessary when
    testing a CHECK constraint on a value that
    :func:`~core.identity.compute_fingerprint` would itself reject first.
    """
    from core.identity import compute_fingerprint
    from core.models import Proxy

    fields: dict[str, object] = {
        "protocol": protocol,
        "server": server,
        "port": port,
        "secret": secret,
    }
    if "fingerprint" not in overrides:
        fields["fingerprint"] = compute_fingerprint(
            server=server, port=port, secret=secret, protocol=protocol
        )
    fields.update(overrides)
    return Proxy(**fields)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item,  # noqa: ARG001 - fixed by pytest's hook protocol
    call: pytest.CallInfo[None],  # noqa: ARG001
) -> Generator[None, Any, None]:
    """Scrub credentials out of test failure output.

    pytest prints fixture values verbatim when a test fails, and the integration
    fixtures hold DSNs. A failure report is exactly the kind of artifact that
    ends up pasted into an issue or a CI log, so the same scrubber that protects
    runtime logs is applied here too. Only rewrites when something matched.
    """
    outcome = yield
    report = outcome.get_result()
    longrepr = report.longrepr
    if longrepr is None:
        return None
    rendered = str(longrepr)
    scrubbed = scrub_secrets(rendered)
    if scrubbed != rendered:
        report.longrepr = scrubbed
    return None
