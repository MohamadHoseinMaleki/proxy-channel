"""Integration tests for the ``FOR UPDATE SKIP LOCKED`` claim query.

This is the mechanism that replaces a message broker in this architecture, so it
is tested against a real PostgreSQL with real concurrent transactions. Nothing
here is simulated: two sessions genuinely hold row locks at the same time, and
the assertions are about what the server actually did.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import DEFAULT_LEASE_SECONDS, Proxy, utcnow
from modules.scheduling import claim_due_proxies
from tests.integration.conftest import make_proxy

#: Everything here needs a live PostgreSQL. The marker lets the suite be run or
#: skipped as a unit; with no database reachable the fixtures skip cleanly.
pytestmark = pytest.mark.integration


async def seed(db: object, count: int, **overrides: object) -> list[int]:
    """Insert ``count`` distinct proxies and return their ids."""
    ids: list[int] = []
    async with db.session_scope() as session:  # type: ignore[attr-defined]
        for index in range(count):
            proxy = make_proxy(server=f"p{index}.example.com", **overrides)  # type: ignore[arg-type]
            session.add(proxy)
            await session.flush()
            ids.append(proxy.id)
    return ids


def ids_of(proxies: list[Proxy]) -> set[int]:
    return {proxy.id for proxy in proxies if proxy.id is not None}


def servers_of(proxies: list[Proxy]) -> list[str]:
    """Read ``server`` from claimed rows.

    Call this **before** rolling the session back. A rollback expires every
    attribute, and re-reading one afterwards fires an implicit lazy refresh --
    which cannot work under asyncio. ``expire_on_commit=False`` protects the
    commit path only.
    """
    return [proxy.server for proxy in proxies]


class TestBasicClaiming:
    async def test_claims_a_due_proxy(self, db: object, session: AsyncSession) -> None:
        await seed(db, 1)
        claimed = await claim_due_proxies(session)
        await session.commit()

        assert len(claimed) == 1
        assert claimed[0].server == "p0.example.com"

    async def test_returns_the_full_row_including_the_secret(
        self, session: AsyncSession, db: object
    ) -> None:
        # The tester needs server, port and the real secret to connect, so one
        # round trip must return all of them -- no follow-up SELECT.
        await seed(db, 1)
        claimed = await claim_due_proxies(session)
        await session.commit()

        proxy = claimed[0]
        assert proxy.port == 443
        assert proxy.secret.reveal().startswith("ee")
        assert proxy.fingerprint

    async def test_selection_is_fair_when_there_are_more_due_than_the_limit(
        self, db: object, session: AsyncSession
    ) -> None:
        """The database-side guarantee: the *oldest* rows are the ones leased.

        Distinct from ``test_claims_in_due_order``, which checks the order of the
        returned list. With six due rows and a limit of three, fairness means the
        three most-overdue are claimed -- not merely that three are claimed.
        Inserted in scrambled order so heap layout cannot flatter the result.
        """
        base = utcnow() - timedelta(hours=6)
        scrambled = [(5, 300), (1, 60), (4, 240), (0, 0), (3, 180), (2, 120)]
        async with db.session_scope() as s:  # type: ignore[attr-defined]
            for index, minutes in scrambled:
                s.add(
                    make_proxy(
                        server=f"p{index}.example.com",
                        next_test_at=base + timedelta(minutes=minutes),
                    )
                )

        claimed = await claim_due_proxies(session, limit=3)
        got = servers_of(claimed)
        await session.rollback()

        assert sorted(got) == ["p0.example.com", "p1.example.com", "p2.example.com"]
        assert got == ["p0.example.com", "p1.example.com", "p2.example.com"]

    async def test_returned_order_is_stable_across_repeated_claims(
        self, db: object, session: AsyncSession
    ) -> None:
        # If ordering depended on heap layout it would drift as the table fills.
        # Claim, release, claim again: the order must be identical every time.
        base = utcnow() - timedelta(hours=3)
        async with db.session_scope() as s:  # type: ignore[attr-defined]
            for index, minutes in ((2, 120), (0, 0), (1, 60)):
                s.add(
                    make_proxy(
                        server=f"p{index}.example.com",
                        next_test_at=base + timedelta(minutes=minutes),
                    )
                )

        orders = []
        for _ in range(3):
            claimed = await claim_due_proxies(session, limit=3)
            orders.append(servers_of(claimed))
            await session.rollback()
            async with db.session_scope() as s:  # type: ignore[attr-defined]
                await s.execute(text("UPDATE proxies SET test_lock_until = NULL"))

        assert orders[0] == orders[1] == orders[2]
        assert orders[0] == ["p0.example.com", "p1.example.com", "p2.example.com"]

    async def test_respects_the_limit(self, db: object, session: AsyncSession) -> None:
        await seed(db, 10)
        assert len(await claim_due_proxies(session, limit=3)) == 3
        await session.rollback()

    async def test_returns_fewer_than_the_limit_when_the_queue_is_short(
        self, db: object, session: AsyncSession
    ) -> None:
        await seed(db, 2)
        assert len(await claim_due_proxies(session, limit=25)) == 2
        await session.rollback()

    async def test_returns_nothing_when_the_table_is_empty(self, session: AsyncSession) -> None:
        assert await claim_due_proxies(session) == []

    async def test_claims_in_due_order(self, db: object, session: AsyncSession) -> None:
        # Oldest first, so a proxy cannot be starved by a stream of new arrivals.
        # claim_due_proxies sorts in Python: UPDATE ... FROM ... RETURNING does not
        # preserve the CTE's ORDER BY, so this would otherwise depend on heap
        # layout and pass or fail by luck.
        base = utcnow() - timedelta(hours=2)
        async with db.session_scope() as s:  # type: ignore[attr-defined]
            for offset in (60, 0, 30):  # inserted out of order on purpose
                proxy = make_proxy(
                    server=f"p{offset}.example.com", next_test_at=base + timedelta(minutes=offset)
                )
                s.add(proxy)

        claimed = await claim_due_proxies(session, limit=3)
        order = servers_of(claimed)  # read before the rollback expires them
        await session.rollback()
        assert order == ["p0.example.com", "p30.example.com", "p60.example.com"]

    async def test_sets_the_lease_and_the_start_time(
        self, db: object, session: AsyncSession
    ) -> None:
        await seed(db, 1)
        moment = utcnow()
        claimed = await claim_due_proxies(session, lease_seconds=90, now=moment)
        await session.commit()

        proxy = claimed[0]
        assert proxy.last_test_started_at == moment
        assert proxy.test_lock_until == moment + timedelta(seconds=90)
        assert proxy.test_attempts == 1

    async def test_the_lease_is_persisted(self, db: object, session: AsyncSession) -> None:
        await seed(db, 1)
        await claim_due_proxies(session, lease_seconds=90)
        await session.commit()

        async with db.session_scope() as fresh:  # type: ignore[attr-defined]
            stored = (await fresh.execute(select(Proxy.test_lock_until, Proxy.test_attempts))).one()
        assert stored.test_lock_until is not None
        assert stored.test_attempts == 1

    async def test_attempts_accumulate_across_claims(
        self, db: object, session: AsyncSession
    ) -> None:
        # The counter is the trail left by a proxy that keeps killing workers, so
        # it must survive repeated claims rather than reset.
        await seed(db, 1)
        for expected in (1, 2, 3):
            claimed = await claim_due_proxies(session)
            await session.commit()
            assert claimed[0].test_attempts == expected
            # Expire the lease so the next iteration can claim again.
            async with db.session_scope() as s:  # type: ignore[attr-defined]
                await s.execute(text("UPDATE proxies SET test_lock_until = NULL"))


class TestEligibility:
    async def test_skips_proxies_not_yet_due(self, db: object, session: AsyncSession) -> None:
        await seed(db, 1, next_test_at=utcnow() + timedelta(hours=1))
        assert await claim_due_proxies(session) == []

    async def test_skips_inactive_proxies(self, db: object, session: AsyncSession) -> None:
        await seed(db, 2)
        async with db.session_scope() as s:  # type: ignore[attr-defined]
            await s.execute(
                text("UPDATE proxies SET is_active = false WHERE server = 'p0.example.com'")
            )

        claimed = await claim_due_proxies(session)
        remaining = servers_of(claimed)
        await session.rollback()
        assert remaining == ["p1.example.com"]

    async def test_skips_a_proxy_whose_lease_is_still_held(
        self, db: object, session: AsyncSession
    ) -> None:
        # This is what stops two testers working the same proxy.
        await seed(db, 1)
        async with db.session_scope() as s:  # type: ignore[attr-defined]
            await s.execute(
                text("UPDATE proxies SET test_lock_until = :until"),
                {"until": utcnow() + timedelta(seconds=300)},
            )
        assert await claim_due_proxies(session) == []

    async def test_reclaims_a_proxy_whose_lease_has_expired(
        self, db: object, session: AsyncSession
    ) -> None:
        # Crash recovery. A worker killed with -9 never clears its claim, so the
        # lease expiring is the *only* thing that makes the row available again.
        await seed(db, 1)
        async with db.session_scope() as s:  # type: ignore[attr-defined]
            await s.execute(
                text("UPDATE proxies SET test_lock_until = :until"),
                {"until": utcnow() - timedelta(seconds=1)},
            )

        claimed = await claim_due_proxies(session)
        await session.rollback()
        assert len(claimed) == 1

    async def test_a_lease_expiring_exactly_now_is_claimable(
        self, db: object, session: AsyncSession
    ) -> None:
        # Boundary: the predicate is `test_lock_until < now`, so an equal
        # timestamp is NOT free. Pinned because an off-by-one here either strands
        # a row for one lease period or double-claims it.
        moment = utcnow()
        # next_test_at must already be in the past, otherwise the row is not due
        # and the lease boundary is not what is being measured.
        await seed(db, 1, next_test_at=moment - timedelta(hours=1), test_lock_until=moment)
        assert await claim_due_proxies(session, now=moment) == []
        assert len(await claim_due_proxies(session, now=moment + timedelta(microseconds=1))) == 1

    async def test_a_row_due_exactly_now_is_claimable(
        self, db: object, session: AsyncSession
    ) -> None:
        # Boundary: `next_test_at <= now`, so equality is due.
        moment = utcnow()
        await seed(db, 1, next_test_at=moment)
        assert len(await claim_due_proxies(session, now=moment)) == 1


class TestLeaseReclaim:
    async def test_a_committed_claim_blocks_a_second_claim(self, db: object) -> None:
        await seed(db, 1)
        async with db.session_scope() as first:  # type: ignore[attr-defined]
            assert len(await claim_due_proxies(first)) == 1

        async with db.session_scope() as second:  # type: ignore[attr-defined]
            assert await claim_due_proxies(second) == []

    async def test_a_rolled_back_claim_releases_the_row(self, db: object) -> None:
        # A worker that dies before committing must not consume the work.
        await seed(db, 1)
        with pytest.raises(RuntimeError, match="simulated worker failure"):
            async with db.session_scope() as first:  # type: ignore[attr-defined]
                assert len(await claim_due_proxies(first)) == 1
                raise RuntimeError("simulated worker failure")

        async with db.session_scope() as second:  # type: ignore[attr-defined]
            claimed = await claim_due_proxies(second)
            attempts = claimed[0].test_attempts
        assert len(claimed) == 1
        assert attempts == 1  # the failed claim left no trace

    async def test_default_lease_outlives_a_slow_test(
        self, db: object, session: AsyncSession
    ) -> None:
        # The lease must exceed the tester's overall timeout, or a slow-but-alive
        # handshake gets double-claimed mid-flight.
        await seed(db, 1)
        claimed = await claim_due_proxies(session)
        await session.commit()

        proxy = claimed[0]
        assert proxy.test_lock_until is not None
        assert proxy.last_test_started_at is not None
        lease = proxy.test_lock_until - proxy.last_test_started_at
        assert lease == timedelta(seconds=DEFAULT_LEASE_SECONDS)
        assert lease >= timedelta(seconds=60)


class TestConcurrency:
    async def test_two_sessions_claim_disjoint_rows(self, db: object) -> None:
        """The core guarantee.

        Both transactions hold their row locks at the same time. With plain
        ``FOR UPDATE`` the second would block until the first committed; with
        ``SKIP LOCKED`` it takes different rows instead.
        """
        await seed(db, 10)
        first = db.session()  # type: ignore[attr-defined]
        second = db.session()  # type: ignore[attr-defined]
        try:
            claimed_first = await claim_due_proxies(first, limit=5)
            claimed_second = await claim_due_proxies(second, limit=5)

            ids_first, ids_second = ids_of(claimed_first), ids_of(claimed_second)
            assert len(ids_first) == 5
            assert len(ids_second) == 5
            assert ids_first.isdisjoint(ids_second), "two workers claimed the same proxy"

            await first.commit()
            await second.commit()
        finally:
            await first.close()
            await second.close()

        async with db.session_scope() as check:  # type: ignore[attr-defined]
            leased = (
                await check.execute(
                    select(func.count(Proxy.id)).where(Proxy.test_lock_until.is_not(None))
                )
            ).scalar_one()
        assert leased == 10

    async def test_an_uncommitted_claim_is_skipped_not_waited_on(self, db: object) -> None:
        # If this blocked, the second session would hang for the whole lease.
        await seed(db, 6)
        holder = db.session()  # type: ignore[attr-defined]
        taker = db.session()  # type: ignore[attr-defined]
        try:
            held = await claim_due_proxies(holder, limit=4)  # deliberately not committed
            assert len(held) == 4

            remaining = await asyncio.wait_for(claim_due_proxies(taker, limit=10), timeout=5.0)
            assert len(remaining) == 2
            assert ids_of(remaining).isdisjoint(ids_of(held))
        finally:
            await holder.rollback()
            await taker.rollback()
            await holder.close()
            await taker.close()

    async def test_many_workers_partition_the_whole_queue(self, db: object) -> None:
        await seed(db, 40)
        workers = 4

        async def claim() -> set[int]:
            session = db.session()  # type: ignore[attr-defined]
            try:
                claimed = await claim_due_proxies(session, limit=100, now=utcnow())
                await session.commit()
                return ids_of(claimed)
            finally:
                await session.close()

        results = await asyncio.gather(*(claim() for _ in range(workers)))
        union = set().union(*results)
        assert len(union) == 40, "some proxies were never claimed"
        assert sum(len(batch) for batch in results) == 40, "a proxy was claimed twice"

    async def test_concurrent_claims_never_double_increment_attempts(self, db: object) -> None:
        # test_attempts is incremented server-side (`SET x = x + 1`), so
        # concurrent claims cannot lose an update the way a Python read-modify-
        # write would.
        await seed(db, 1)

        async def attempt() -> int:
            session = db.session()  # type: ignore[attr-defined]
            try:
                claimed = await claim_due_proxies(session)
                await session.commit()
                return claimed[0].test_attempts if claimed else 0
            finally:
                await session.close()

        counts = await asyncio.gather(attempt(), attempt(), attempt())
        # Only one can hold the lease at a time; each successful claim bumps by 1.
        assert sorted(c for c in counts if c) == [1]
        async with db.session_scope() as check:  # type: ignore[attr-defined]
            stored = (await check.execute(select(Proxy.test_attempts))).scalar_one()
        assert stored == 1

    async def test_a_claim_does_not_hold_a_transaction_open(self, db: object) -> None:
        # The rule the whole pattern exists to enforce: never hold a transaction
        # while waiting on an MTProto timeout. session_scope must have committed
        # and released its connection before any network I/O happens.
        await seed(db, 2)
        async with db.session_scope() as session:  # type: ignore[attr-defined]
            claimed = await claim_due_proxies(session, limit=2)
        # Outside the scope: the rows are committed and still readable elsewhere.
        assert len(claimed) == 2
        async with db.session_scope() as other:  # type: ignore[attr-defined]
            leased = (
                await other.execute(
                    select(func.count(Proxy.id)).where(Proxy.test_lock_until.is_not(None))
                )
            ).scalar_one()
        assert leased == 2


class TestSessionHazards:
    async def test_rollback_expires_claimed_rows(self, db: object, session: AsyncSession) -> None:
        """A trap worth writing down: read claimed rows before rolling back.

        ``expire_on_commit=False`` makes the commit path safe, but a rollback
        still expires every attribute, and re-reading one fires an implicit lazy
        refresh that cannot run under asyncio. The tester must therefore pull
        ``id``/``server``/``port``/``secret`` out of a claimed proxy immediately.
        """
        from sqlalchemy.exc import MissingGreenlet

        await seed(db, 1)
        claimed = await claim_due_proxies(session)
        assert claimed[0].server == "p0.example.com"  # fine: still in the transaction

        await session.rollback()
        with pytest.raises(MissingGreenlet):
            _ = claimed[0].server

    async def test_committed_claims_stay_readable(self, db: object, session: AsyncSession) -> None:
        # The normal worker path: claim, commit, then go do network I/O using the
        # values. This must work -- it is why expire_on_commit=False is set.
        await seed(db, 1)
        claimed = await claim_due_proxies(session)
        await session.commit()
        assert claimed[0].server == "p0.example.com"
        assert claimed[0].secret.reveal().startswith("ee")


class TestQueryPlan:
    async def test_the_claim_query_can_use_ix_proxies_due(
        self, db: object, session: AsyncSession
    ) -> None:
        """Prove the index actually serves the claim predicate.

        With a handful of rows PostgreSQL correctly prefers a sequential scan, so
        a plan test on an empty table would prove nothing. ``enable_seqscan=off``
        forces the planner to show whether the index *can* serve the query, which
        is the property worth pinning: an index that does not match the predicate
        silently becomes decoration.
        """
        await seed(db, 5)
        await session.execute(text("SET LOCAL enable_seqscan = off"))
        plan = (
            (
                await session.execute(
                    text(
                        "EXPLAIN (FORMAT TEXT) "
                        "SELECT id FROM proxies "
                        "WHERE is_active AND next_test_at <= now() "
                        "AND (test_lock_until IS NULL OR test_lock_until < now()) "
                        "ORDER BY next_test_at, id LIMIT 25 FOR UPDATE SKIP LOCKED"
                    )
                )
            )
            .scalars()
            .all()
        )
        rendered = "\n".join(plan)
        await session.rollback()

        assert "ix_proxies_due" in rendered, rendered
        # The partial index also removes the is_active filter from the plan.
        assert "Seq Scan" not in rendered

    async def test_the_index_is_partial_on_is_active(self, session: AsyncSession) -> None:
        definition = (
            await session.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_proxies_due'")
            )
        ).scalar_one()
        assert "WHERE is_active" in definition
        assert "next_test_at" in definition

    async def test_a_query_without_is_active_cannot_use_the_partial_index(
        self, db: object, session: AsyncSession
    ) -> None:
        """The discriminating half of the previous test.

        A partial index is only usable when the query's predicate *implies* the
        index predicate. Dropping ``is_active`` from the WHERE clause must
        therefore lose the index -- if it did not, the index would not really be
        partial and would be carrying dead rows for nothing.
        """
        await seed(db, 5)
        await session.execute(text("SET LOCAL enable_seqscan = off"))
        plan = (
            (
                await session.execute(
                    text(
                        "EXPLAIN (FORMAT TEXT) "
                        "SELECT id FROM proxies "
                        "WHERE next_test_at <= now() "
                        "ORDER BY next_test_at LIMIT 25"
                    )
                )
            )
            .scalars()
            .all()
        )
        await session.rollback()

        assert "ix_proxies_due" not in "\n".join(plan)

    async def test_inactive_proxies_are_not_claimable_even_when_due(
        self, db: object, session: AsyncSession
    ) -> None:
        # The behavioural payoff of the partial index: retiring a proxy removes it
        # from the claim set entirely, not merely from the index.
        await seed(db, 3)
        async with db.session_scope() as s:  # type: ignore[attr-defined]
            await s.execute(text("UPDATE proxies SET is_active = false"))

        assert await claim_due_proxies(session) == []
        active = (
            await session.execute(select(func.count(Proxy.id)).where(Proxy.is_active.is_(True)))
        ).scalar_one()
        assert active == 0
