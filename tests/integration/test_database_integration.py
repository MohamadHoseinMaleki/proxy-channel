"""Integration tests for :mod:`core.database` against a real PostgreSQL.

Connection pooling, transaction boundaries and ``connect_args`` forwarding are
server-side behaviours; the unit suite can only assert how the engine was
configured. The multi-engine tests at the bottom stand in for the architecture's
central claim -- that three independent OS processes coordinate through
PostgreSQL and nothing else -- by giving each "process" its own engine and pool.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

from core.database import Database, database_exists
from core.models import Proxy
from modules.scheduling import claim_due_proxies
from tests.conftest import queue_pool
from tests.integration.conftest import _database_name, make_proxy

#: Everything here needs a live PostgreSQL. The marker lets the suite be run or
#: skipped as a unit; with no database reachable the fixtures skip cleanly.
pytestmark = pytest.mark.integration


class TestConnectivity:
    async def test_ping_succeeds(self, db: Database) -> None:
        await db.ping()

    async def test_is_reachable(self, db: Database) -> None:
        assert await db.is_reachable() is True

    async def test_the_server_is_postgresql(self, db: Database) -> None:
        async with db.engine.connect() as connection:
            version = (await connection.execute(text("SHOW server_version_num"))).scalar_one()
        assert int(version) >= 120000, "the schema targets modern PostgreSQL"

    async def test_database_exists_finds_the_test_database(self, db: Database) -> None:
        url = str(db.engine.url.render_as_string(hide_password=False))
        assert await database_exists(url, _database_name(url)) is True

    async def test_database_exists_rejects_a_missing_database(self, db: Database) -> None:
        url = str(db.engine.url.render_as_string(hide_password=False))
        assert await database_exists(url, "definitely_not_a_real_database") is False

    async def test_connects_as_a_non_superuser(self, db: Database) -> None:
        # Deliberate: running migrations and tests as a superuser would hide
        # permission bugs that only surface in production.
        async with db.engine.connect() as connection:
            is_super = (
                await connection.execute(
                    text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
                )
            ).scalar_one()
        assert is_super is False


class TestConnectArgs:
    async def test_connect_args_reach_the_server(self, test_database_url: str) -> None:
        # Forwarding is invisible from the unit side (SQLAlchemy closes over it),
        # so prove it end-to-end: the server must report the application_name we
        # asked for. This matters because it is how a DBA attributes load to a
        # specific worker process.
        database = Database(
            test_database_url,
            pool_size=1,
            max_overflow=0,
            connect_args={"server_settings": {"application_name": "integration-tester"}},
        )
        try:
            async with database.engine.connect() as connection:
                name = (
                    await connection.execute(text("SELECT current_setting('application_name')"))
                ).scalar_one()
        finally:
            await database.dispose()
        assert name == "integration-tester"

    async def test_each_worker_can_be_identified_separately(self, test_database_url: str) -> None:
        async def identify(worker: str) -> str:
            database = Database(
                test_database_url,
                pool_size=1,
                max_overflow=0,
                connect_args={"server_settings": {"application_name": worker}},
            )
            try:
                async with database.engine.connect() as connection:
                    return str(
                        (
                            await connection.execute(
                                text("SELECT current_setting('application_name')")
                            )
                        ).scalar_one()
                    )
            finally:
                await database.dispose()

        results = await asyncio.gather(
            identify("discovery-worker"), identify("tester-worker"), identify("scoring-worker")
        )
        assert sorted(results) == ["discovery-worker", "scoring-worker", "tester-worker"]


class TestTransactions:
    async def test_session_scope_commits(self, db: Database) -> None:
        async with db.session_scope() as session:
            session.add(make_proxy())
        async with db.session_scope() as other:
            assert (await other.execute(select(func.count(Proxy.id)))).scalar_one() == 1

    async def test_session_scope_rolls_back_on_error(self, db: Database) -> None:
        with pytest.raises(RuntimeError, match="simulated"):
            async with db.session_scope() as session:
                session.add(make_proxy())
                raise RuntimeError("simulated")
        async with db.session_scope() as other:
            assert (await other.execute(select(func.count(Proxy.id)))).scalar_one() == 0

    async def test_a_rolled_back_session_releases_row_locks(self, db: Database) -> None:
        # If a failed transaction held locks, the next claim in another process
        # would block -- exactly the failure SKIP LOCKED exists to prevent.
        async with db.session_scope() as seed:
            seed.add(make_proxy())

        with pytest.raises(RuntimeError):
            async with db.session_scope() as holder:
                await claim_due_proxies(holder)
                raise RuntimeError("worker died mid-claim")

        async with db.session_scope() as taker:
            claimed = await asyncio.wait_for(claim_due_proxies(taker), timeout=5.0)
        assert len(claimed) == 1

    async def test_attributes_survive_a_commit(self, db: Database) -> None:
        # expire_on_commit=False. Without it, reading `proxy.server` after commit
        # fires a lazy refresh, which cannot work under asyncio.
        async with db.session_scope() as session:
            session.add(make_proxy())
            await session.flush()
            proxy = (await session.execute(select(Proxy))).scalar_one()

        assert proxy.server == "proxy.example.com"
        assert proxy.secret.reveal().startswith("ee")

    async def test_concurrent_writes_from_one_pool(self, db: Database) -> None:
        async def write(index: int) -> None:
            async with db.session_scope() as session:
                session.add(make_proxy(server=f"c{index}.example.com"))

        await asyncio.gather(*(write(index) for index in range(20)))
        async with db.session_scope() as session:
            assert (await session.execute(select(func.count(Proxy.id)))).scalar_one() == 20


class TestPooling:
    async def test_dispose_closes_pooled_connections(self, test_database_url: str) -> None:
        database = Database(test_database_url, pool_size=2, max_overflow=0)
        async with database.session_scope() as session:
            await session.execute(text("SELECT 1"))
        assert queue_pool(database.engine).checkedout() == 0
        await database.dispose()

    async def test_a_disposed_engine_can_still_serve_new_requests(
        self, test_database_url: str
    ) -> None:
        # Workers may dispose during a graceful shutdown and still have work in
        # flight; SQLAlchemy recreates the pool rather than failing hard.
        database = Database(test_database_url, pool_size=1, max_overflow=0)
        await database.dispose()
        await database.ping()
        await database.dispose()

    async def test_pool_exhaustion_raises_rather_than_hanging(self, test_database_url: str) -> None:
        # A worker that leaks sessions must fail loudly within pool_timeout, not
        # wedge the process forever.
        from sqlalchemy.exc import TimeoutError as PoolTimeout

        database = Database(test_database_url, pool_size=1, max_overflow=0, pool_timeout=0.3)
        try:
            held = database.session()
            await held.execute(text("SELECT 1"))
            try:
                other = database.session()
                with pytest.raises(PoolTimeout):
                    await asyncio.wait_for(other.execute(text("SELECT 1")), timeout=5.0)
                await other.close()
            finally:
                await held.close()
        finally:
            await database.dispose()

    async def test_overflow_connections_are_returned(self, test_database_url: str) -> None:
        database = Database(test_database_url, pool_size=1, max_overflow=2, pool_timeout=2.0)
        try:
            sessions = [database.session() for _ in range(3)]
            await asyncio.gather(*(session.execute(text("SELECT 1")) for session in sessions))
            assert queue_pool(database.engine).checkedout() == 3
            for session in sessions:
                await session.close()
            assert queue_pool(database.engine).checkedout() == 0
        finally:
            await database.dispose()

    async def test_pre_ping_recovers_from_a_severed_connection(
        self, test_database_url: str
    ) -> None:
        # Simulates what pre-ping exists for: a pooled connection the server has
        # dropped (idle timeout, failover, restart). Without pre-ping the next
        # query dies with InterfaceError; with it the pool reconnects silently.
        database = Database(test_database_url, pool_size=1, max_overflow=0, pool_pre_ping=True)
        # A *separate* engine does the killing: terminating a backend from its own
        # connection would abort the very statement issuing the command.
        killer = Database(test_database_url, pool_size=1, max_overflow=0)
        try:
            async with database.engine.connect() as connection:
                victim_pid = (
                    await connection.execute(text("SELECT pg_backend_pid()"))
                ).scalar_one()
            # `victim_pid` is now sitting idle in database's pool.
            async with killer.engine.connect() as connection:
                await connection.execute(
                    text("SELECT pg_terminate_backend(:pid)"), {"pid": victim_pid}
                )
            await database.ping()
            async with database.engine.connect() as connection:
                assert (await connection.execute(text("SELECT 1"))).scalar_one() == 1
        finally:
            await killer.dispose()
            await database.dispose()

    async def test_without_pre_ping_a_severed_connection_surfaces_as_an_error(
        self, test_database_url: str
    ) -> None:
        # The other half of the argument: this is the failure pre-ping prevents.
        # Pinned so that turning pre-ping off is a visible regression, not a
        # silent one that only appears after a database restart at 3am.
        database = Database(test_database_url, pool_size=1, max_overflow=0, pool_pre_ping=False)
        killer = Database(test_database_url, pool_size=1, max_overflow=0)
        try:
            async with database.engine.connect() as connection:
                victim_pid = (
                    await connection.execute(text("SELECT pg_backend_pid()"))
                ).scalar_one()
            async with killer.engine.connect() as connection:
                await connection.execute(
                    text("SELECT pg_terminate_backend(:pid)"), {"pid": victim_pid}
                )
            with pytest.raises(SQLAlchemyError):
                await database.ping()
        finally:
            await killer.dispose()
            await database.dispose()


class TestMultiProcessCoordination:
    async def test_three_independent_engines_partition_the_queue(
        self, test_database_url: str
    ) -> None:
        """The architecture's central claim, tested at the database layer.

        Three separate engines with three separate pools stand in for the three
        OS processes. Nothing coordinates them except PostgreSQL row locks, so if
        this passes the design holds without a broker.
        """
        seed_engine = Database(test_database_url, pool_size=2, max_overflow=0)
        try:
            async with seed_engine.session_scope() as session:
                for index in range(12):
                    session.add(make_proxy(server=f"w{index}.example.com"))
        finally:
            await seed_engine.dispose()

        workers = [
            Database(
                test_database_url,
                pool_size=2,
                max_overflow=0,
                connect_args={"server_settings": {"application_name": f"tester-{index}"}},
            )
            for index in range(3)
        ]
        try:

            async def run(worker: Database) -> set[int]:
                claimed_ids: set[int] = set()
                for _ in range(10):
                    async with worker.session_scope() as session:
                        claimed = await claim_due_proxies(session, limit=2)
                        claimed_ids.update(proxy.id for proxy in claimed if proxy.id)
                    if not claimed:
                        break
                return claimed_ids

            results = await asyncio.gather(*(run(worker) for worker in workers))
        finally:
            for worker in workers:
                await worker.dispose()

        union = set().union(*results)
        assert len(union) == 12, f"expected all 12 claimed exactly once, got {sorted(union)}"
        assert sum(len(batch) for batch in results) == 12, "a proxy was claimed by two workers"

    async def test_no_worker_ever_sees_another_lock_wait(self, test_database_url: str) -> None:
        # If SKIP LOCKED were absent, contention would show up as blocking. Run
        # four workers flat out and require the whole thing to finish quickly.
        seed_engine = Database(test_database_url, pool_size=2, max_overflow=0)
        try:
            async with seed_engine.session_scope() as session:
                for index in range(40):
                    session.add(make_proxy(server=f"z{index}.example.com"))
        finally:
            await seed_engine.dispose()

        async def worker() -> int:
            database = Database(test_database_url, pool_size=2, max_overflow=0)
            total = 0
            try:
                while True:
                    async with database.session_scope() as session:
                        claimed = await claim_due_proxies(session, limit=5)
                        total += len(claimed)
                    if not claimed:
                        return total
            finally:
                await database.dispose()

        totals = await asyncio.wait_for(asyncio.gather(*(worker() for _ in range(4))), timeout=30.0)
        # Every row claimed exactly once, and no deadlock or lock-wait timeout:
        # with plain FOR UPDATE these four workers would serialise on each other.
        assert sum(totals) == 40, f"rows lost or double-claimed: {totals}"
        # SKIP LOCKED partitions the work. Requiring *every* worker to get a row
        # would be flaky (one can legitimately find the queue empty), but one
        # worker taking everything would mean the others were blocked.
        assert max(totals) < 40, f"one worker did all the work: {totals}"
        assert sum(1 for total in totals if total > 0) >= 2, f"no parallelism: {totals}"


class TestParameterHiding:
    """A failing INSERT must not put the plaintext secret into an exception.

    Two independent leak paths, both closed:

    * SQLAlchemy appends ``[parameters: (...)]`` to DBAPI errors -- suppressed by
      ``hide_parameters=True`` on the engine.
    * PostgreSQL appends ``DETAIL: Failing row contains (...)`` and echoes every
      column -- NOT affected by that option, and handled by the hex scrubber in
      :mod:`core.logger`.
    """

    SECRET = "ee" + "a1" * 15

    async def _violation(self, db: Database) -> str:
        from sqlalchemy.exc import SQLAlchemyError as Error

        with pytest.raises(Error) as info:
            async with db.session_scope() as session:
                await session.execute(
                    text(
                        "INSERT INTO proxies (server, port, secret, fingerprint) "
                        "VALUES ('', 0, :secret, :fp)"
                    ),
                    {"secret": self.SECRET, "fp": "b" * 64},
                )
        return str(info.value)

    async def test_parameters_are_not_appended_by_sqlalchemy(self, db: Database) -> None:
        assert "[parameters:" not in await self._violation(db)

    async def test_the_secret_is_not_in_the_safe_error_message(self, db: Database) -> None:
        from core.logger import safe_error_message

        raw = await self._violation(db)
        assert safe_error_message(raw) is not None
        assert self.SECRET not in (safe_error_message(raw) or "")

    async def test_postgres_row_detail_is_scrubbed(self, db: Database) -> None:
        # Proves the second path is real and closed: the server echoes the row,
        # so the scrubber -- not the engine option -- is what protects it.
        from core.logger import safe_error_message

        raw = await self._violation(db)
        assert "Failing row contains" in raw  # PostgreSQL really did echo the row
        assert self.SECRET in raw  # ... including the secret
        assert self.SECRET not in (safe_error_message(raw) or "")

    async def test_hiding_parameters_can_be_turned_off_for_debugging(
        self, test_database_url: str
    ) -> None:
        database = Database(test_database_url, pool_size=1, max_overflow=0, hide_parameters=False)
        try:
            with pytest.raises(SQLAlchemyError) as info:
                async with database.session_scope() as session:
                    await session.execute(
                        text(
                            "INSERT INTO proxies (server, port, secret, fingerprint) "
                            "VALUES ('', 0, :secret, :fp)"
                        ),
                        {"secret": self.SECRET, "fp": "c" * 64},
                    )
            assert "[parameters:" in str(info.value)
        finally:
            await database.dispose()

    async def test_the_setting_reaches_the_engine(self, test_database_url: str) -> None:
        # Asserted behaviourally rather than by poking engine internals: build
        # from Settings with the flag off and confirm parameters become visible.
        from tests.conftest import make_settings

        database = Database.from_settings(
            make_settings(database_url=test_database_url, db_hide_parameters=False)
        )
        try:
            with pytest.raises(SQLAlchemyError) as info:
                async with database.session_scope() as session:
                    await session.execute(
                        text(
                            "INSERT INTO proxies (server, port, secret, fingerprint) "
                            "VALUES ('', 0, :s, :f)"
                        ),
                        {"s": self.SECRET, "f": "d" * 64},
                    )
            assert "[parameters:" in str(info.value)
        finally:
            await database.dispose()

    async def test_parameters_are_hidden_by_default(self, test_database_url: str) -> None:
        from tests.conftest import make_settings

        database = Database.from_settings(make_settings(database_url=test_database_url))
        try:
            with pytest.raises(SQLAlchemyError) as info:
                async with database.session_scope() as session:
                    await session.execute(
                        text(
                            "INSERT INTO proxies (server, port, secret, fingerprint) "
                            "VALUES ('', 0, :s, :f)"
                        ),
                        {"s": self.SECRET, "f": "e" * 64},
                    )
            assert "parameters hidden" in str(info.value)
        finally:
            await database.dispose()


class TestQueryHygiene:
    async def test_a_real_query_does_not_log_the_secret(
        self, db: Database, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import logging

        from core.logger import configure_logging
        from tests.conftest import make_settings

        configure_logging(make_settings(log_format="json", log_level="DEBUG"), force=True)
        secret = "ee" + "a1" * 15

        handler = logging.StreamHandler()
        logging.getLogger().addHandler(handler)
        try:
            async with db.session_scope() as session:
                session.add(make_proxy(secret=secret))
            async with db.session_scope() as session:
                await session.execute(select(Proxy))
        finally:
            logging.getLogger().removeHandler(handler)

        emitted = capsys.readouterr()
        combined = emitted.out + emitted.err
        assert secret not in combined

    async def test_echo_is_off_so_sql_is_not_written_to_stdout(self, db: Database) -> None:
        assert db.engine.echo is False
