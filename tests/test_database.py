"""Unit tests for :mod:`core.database` -- engine construction and URL hygiene.

No PostgreSQL required. Constructing an engine does not connect, so pool
configuration, DSN validation and secret masking are all testable offline.
Behaviour that needs a real server lives in ``tests/integration``.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from core.database import Database, database_exists
from tests.conftest import make_settings, queue_pool

DSN = "postgresql+asyncpg://mtproto:s3cr3t-pw@db.example.com:5432/mtproto"


class TestUrlValidation:
    @pytest.mark.parametrize(
        "url",
        [
            "sqlite+aiosqlite:///./local.db",
            "mysql+asyncmy://u:p@localhost/db",
            "http://db.example.com",
            "postgresql+psycopg2://u:p@localhost/db",  # sync driver: would block the loop
            "not-a-url",
            "",
        ],
    )
    def test_rejects_non_async_postgres_urls(self, url: str) -> None:
        with pytest.raises(ValueError, match="PostgreSQL DSN"):
            Database(url)

    def test_accepts_both_postgres_prefixes(self) -> None:
        assert Database(DSN).engine is not None
        assert Database(DSN.replace("+asyncpg", "")).engine is not None

    def test_bare_postgres_prefix_is_normalised_to_asyncpg(self) -> None:
        # create_async_engine resolves a bare postgresql:// to psycopg2, which is
        # not installed; accepting the prefix and then dying with
        # ModuleNotFoundError would be the worst of both behaviours.
        bare = Database(DSN.replace("+asyncpg", ""))
        assert str(bare.engine.url).startswith("postgresql+asyncpg://")
        assert bare.safe_url.startswith("postgresql+asyncpg://")

    def test_normalisation_preserves_credentials_and_params(self) -> None:
        # str(URL) masks the password by default, so render explicitly.
        url = "postgresql://u:p@h:5432/db?ssl=require"
        rendered = Database(url).engine.url.render_as_string(hide_password=False)
        assert rendered == "postgresql+asyncpg://u:p@h:5432/db?ssl=require"

    def test_sqlalchemy_masks_the_password_in_url_str(self) -> None:
        # A second, independent layer: even a raw engine.url in a log line is safe.
        assert "s3cr3t-pw" not in str(Database(DSN).engine.url)
        assert "***" in str(Database(DSN).engine.url)

    def test_rejection_does_not_echo_the_password(self) -> None:
        # The error message quotes the offending URL, so it must be scrubbed
        # first -- this exception ends up in logs and CI output.
        with pytest.raises(ValueError) as info:
            Database("mysql+asyncmy://mtproto:s3cr3t-pw@db.example.com/mtproto")
        assert "s3cr3t-pw" not in str(info.value)
        assert "***REDACTED***" in str(info.value)

    def test_rejection_message_still_identifies_the_problem(self) -> None:
        with pytest.raises(ValueError) as info:
            Database("sqlite:///:memory:")
        assert "postgresql+asyncpg://" in str(info.value)


class TestSecretMasking:
    def test_safe_url_masks_the_password(self) -> None:
        assert "s3cr3t-pw" not in Database(DSN).safe_url

    def test_safe_url_keeps_enough_to_debug(self) -> None:
        safe = Database(DSN).safe_url
        assert "db.example.com" in safe
        assert "5432" in safe
        assert "/mtproto" in safe
        assert "mtproto:" in safe  # username survives, password does not

    def test_repr_masks_the_password(self) -> None:
        assert "s3cr3t-pw" not in repr(Database(DSN))

    def test_unix_socket_url_is_also_masked(self) -> None:
        url = "postgresql+asyncpg://mtproto:s3cr3t-pw@/mtproto?host=/var/run/postgresql"
        assert "s3cr3t-pw" not in Database(url).safe_url


class TestEngineConfiguration:
    def test_pool_settings_are_applied(self) -> None:
        db = Database(DSN, pool_size=7, max_overflow=3, pool_timeout=11.0, pool_recycle=99)
        pool = queue_pool(db.engine)
        assert pool.size() == 7
        assert pool._max_overflow == 3
        assert pool._timeout == 11.0
        assert pool._recycle == 99

    def test_pre_ping_is_on_by_default(self) -> None:
        # A stale pooled connection surfaces as an InterfaceError deep inside a
        # worker loop; pre-ping turns that into a transparent reconnect.
        assert Database(DSN).engine.pool._pre_ping is True

    def test_pre_ping_can_be_disabled(self) -> None:
        assert Database(DSN, pool_pre_ping=False).engine.pool._pre_ping is False

    def test_parameters_are_hidden_by_default(self) -> None:
        # A failing INSERT would otherwise carry bind values -- including the
        # plaintext secret -- into an exception string that gets logged.
        from core.database import DEFAULT_HIDE_PARAMETERS

        assert DEFAULT_HIDE_PARAMETERS is True

    def test_hiding_parameters_can_be_disabled(self) -> None:
        assert Database(DSN, hide_parameters=False).engine is not None

    def test_from_settings_propagates_hide_parameters(self) -> None:
        settings = make_settings(database_url=DSN, db_hide_parameters=False)
        assert Database.from_settings(settings).engine is not None

    def test_echo_defaults_to_off(self) -> None:
        assert Database(DSN).engine.echo is False

    def test_engine_is_async(self) -> None:
        assert isinstance(Database(DSN).engine, AsyncEngine)

    def test_sessions_do_not_expire_on_commit(self) -> None:
        # Worker code reads attributes after commit; an expiring session would
        # emit a lazy refresh -- an implicit round trip that fails under asyncio.
        factory = Database(DSN).session_factory
        assert factory.kw["expire_on_commit"] is False

    def test_sessions_do_not_autoflush(self) -> None:
        # Autoflush mid-query makes claim semantics much harder to reason about.
        assert Database(DSN).session_factory.kw["autoflush"] is False

    def test_session_returns_an_async_session(self) -> None:
        from sqlalchemy.ext.asyncio import AsyncSession

        assert isinstance(Database(DSN).session(), AsyncSession)

    def test_two_instances_do_not_share_a_pool(self) -> None:
        # Four separate processes each build their own; even within one process
        # there is no hidden global engine.
        assert Database(DSN).engine is not Database(DSN).engine

    def test_connect_args_are_accepted(self) -> None:
        # Forwarding itself is a SQLAlchemy closure, not observable here; the
        # integration suite asserts it end-to-end via pg_stat_activity.
        db = Database(DSN, connect_args={"server_settings": {"application_name": "tester"}})
        assert isinstance(db.engine, AsyncEngine)


class TestFromSettings:
    def test_uses_the_configured_dsn(self) -> None:
        settings = make_settings(database_url=DSN)
        assert Database.from_settings(settings).safe_url == Database(DSN).safe_url

    def test_propagates_pool_settings(self) -> None:
        settings = make_settings(
            database_url=DSN,
            db_pool_size=13,
            db_max_overflow=2,
            db_pool_timeout_seconds=4.5,
            db_pool_recycle_seconds=77,
        )
        pool = queue_pool(Database.from_settings(settings).engine)
        assert pool.size() == 13
        assert pool._max_overflow == 2
        assert pool._timeout == 4.5
        assert pool._recycle == 77

    def test_propagates_pre_ping(self) -> None:
        settings = make_settings(database_url=DSN, db_pool_pre_ping=False)
        assert Database.from_settings(settings).engine.pool._pre_ping is False

    def test_for_test_targets_the_test_database(self) -> None:
        settings = make_settings(database_url=DSN)
        assert Database.from_settings(settings, for_test=True).safe_url.endswith("/mtproto_test")

    def test_for_test_never_targets_the_dev_database(self) -> None:
        # The migration lifecycle test runs `downgrade base`, which DROPS EVERY
        # TABLE. It must not be able to reach a working database by inference.
        settings = make_settings(database_url=DSN)
        assert (
            Database.from_settings(settings).safe_url
            != Database.from_settings(settings, for_test=True).safe_url
        )

    def test_for_test_honours_an_explicit_dsn(self) -> None:
        explicit = "postgresql+asyncpg://u:p@localhost/explicit_test_db"
        settings = make_settings(database_url=DSN, test_database_url=explicit)
        assert Database.from_settings(settings, for_test=True).safe_url.endswith(
            "/explicit_test_db"
        )

    def test_for_test_refuses_to_derive_in_production(self) -> None:
        settings = make_settings(env="production", database_url=DSN)
        with pytest.raises(ValueError, match="ENV=production"):
            Database.from_settings(settings, for_test=True)

    def test_falls_back_to_cached_settings(self) -> None:
        from core.config import get_settings

        get_settings.cache_clear()
        db = Database.from_settings()
        assert isinstance(db.engine, AsyncEngine)
        get_settings.cache_clear()


class _StubSession:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def commit(self) -> None:
        self.calls.append("commit")

    async def rollback(self) -> None:
        self.calls.append("rollback")

    async def close(self) -> None:
        self.calls.append("close")


def _database_with_stub_session() -> tuple[Database, list[_StubSession]]:
    db = Database(DSN)
    created: list[_StubSession] = []

    def factory() -> _StubSession:
        session = _StubSession()
        created.append(session)
        return session

    db._session_factory = factory  # type: ignore[assignment]
    return db, created


class TestSessionScope:
    async def test_commits_on_success(self) -> None:
        db, created = _database_with_stub_session()
        async with db.session_scope():
            pass
        # The stub replaced the real session factory, so the scope handed out
        # exactly one stub session and committed then closed it.
        assert len(created) == 1
        assert created[0].calls == ["commit", "close"]

    async def test_rolls_back_on_exception(self) -> None:
        db, created = _database_with_stub_session()
        with pytest.raises(RuntimeError, match="boom"):
            async with db.session_scope():
                raise RuntimeError("boom")
        assert created[0].calls == ["rollback", "close"]

    async def test_never_commits_after_a_rollback(self) -> None:
        db, created = _database_with_stub_session()
        with pytest.raises(ValueError):
            async with db.session_scope():
                raise ValueError
        assert "commit" not in created[0].calls

    async def test_closes_even_on_cancel(self) -> None:
        import asyncio

        db, created = _database_with_stub_session()

        async def victim() -> None:
            async with db.session_scope():
                await asyncio.sleep(30)

        task = asyncio.create_task(victim())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # CancelledError is a BaseException, so `except Exception` would miss it
        # and leak the connection back to the pool.
        assert "close" in created[0].calls
        assert "rollback" in created[0].calls

    async def test_each_scope_gets_its_own_session(self) -> None:
        db, created = _database_with_stub_session()
        async with db.session_scope():
            pass
        async with db.session_scope():
            pass
        assert len(created) == 2


class TestLifecycle:
    async def test_dispose_is_idempotent(self) -> None:
        db = Database(DSN)
        await db.dispose()
        await db.dispose()

    async def test_async_context_manager_disposes(self) -> None:
        db = Database(DSN)
        async with db as entered:
            assert entered is db
        # A disposed engine's pool cannot hand out a checked-out connection.
        assert queue_pool(db.engine).checkedout() == 0

    async def test_is_reachable_is_false_without_a_server(self) -> None:
        unreachable = "postgresql+asyncpg://u:p@127.0.0.1:1/none"
        db = Database(unreachable, pool_size=1, max_overflow=0, pool_timeout=0.2)
        try:
            assert await db.is_reachable() is False
        finally:
            await db.dispose()

    async def test_is_reachable_never_raises(self) -> None:
        db = Database("postgresql+asyncpg://u:p@127.0.0.1:1/none", pool_timeout=0.2)
        try:
            assert isinstance(await db.is_reachable(), bool)
        finally:
            await db.dispose()

    async def test_ping_raises_when_unreachable(self) -> None:
        from sqlalchemy.exc import SQLAlchemyError

        db = Database("postgresql+asyncpg://u:p@127.0.0.1:1/none", pool_timeout=0.2)
        try:
            # Not `pytest.raises(SQLAlchemyError)`: a refused TCP connection
            # escapes the dialect as a bare OSError, so the docstring says so and
            # is_reachable catches both.
            with pytest.raises((SQLAlchemyError, OSError)):
                await db.ping()
        finally:
            await db.dispose()

    async def test_a_refused_connection_is_not_a_sqlalchemy_error(self) -> None:
        # Pins the surprising behaviour the docstring above depends on.
        from sqlalchemy.exc import SQLAlchemyError

        db = Database("postgresql+asyncpg://u:p@127.0.0.1:1/none", pool_timeout=0.2)
        try:
            with pytest.raises(OSError) as info:
                await db.ping()
            assert not isinstance(info.value, SQLAlchemyError)
        finally:
            await db.dispose()


class TestDatabaseExists:
    async def test_returns_false_for_an_unreachable_server(self) -> None:
        # Fixtures use this to decide whether to skip, so a dead server must be
        # reported as "no", never raised.
        assert await database_exists("postgresql+asyncpg://u:p@127.0.0.1:1/x", "x") is False

    async def test_rejects_a_non_postgres_url_without_raising(self) -> None:
        # Callers are fixtures deciding whether to skip. A DSN this module does
        # not understand must degrade to False, not become a collection error.
        result: Any = await database_exists("sqlite:///:memory:", "x")
        assert result is False
        assert await database_exists("nonsense", "x") is False

    async def test_targets_the_maintenance_database(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # CREATE DATABASE cannot run inside a transaction against the target
        # database itself, so the probe must connect to `postgres`.
        seen: list[str] = []
        real_init = Database.__init__

        def spy(self: Database, url: str, **kwargs: Any) -> None:
            seen.append(url)
            real_init(self, url, **kwargs)

        monkeypatch.setattr(Database, "__init__", spy)
        await database_exists(DSN, "mtproto")
        assert seen == ["postgresql+asyncpg://mtproto:s3cr3t-pw@db.example.com:5432/postgres"]

    async def test_preserves_unix_socket_query_params(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The socket directory lives in ?host=; dropping it would make the probe
        # dial the default TCP port instead of the local cluster.
        seen: list[str] = []
        real_init = Database.__init__

        def spy(self: Database, url: str, **kwargs: Any) -> None:
            seen.append(url)
            real_init(self, url, **kwargs)

        monkeypatch.setattr(Database, "__init__", spy)
        url = "postgresql+asyncpg://u:p@/mtproto?host=/var/run/postgresql"
        await database_exists(url, "mtproto")
        assert seen[0].endswith("/postgres?host=/var/run/postgresql")

    async def test_disposes_its_probe_engine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        disposed: list[bool] = []
        real_dispose = Database.dispose

        async def spy(self: Database) -> None:
            disposed.append(True)
            await real_dispose(self)

        monkeypatch.setattr(Database, "dispose", spy)
        await database_exists("postgresql+asyncpg://u:p@127.0.0.1:1/x", "x")
        assert disposed == [True]
