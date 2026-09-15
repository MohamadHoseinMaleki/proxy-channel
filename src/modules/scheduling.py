"""Work-claiming primitive for the tester.

This is the *only* piece of scheduling logic in Task 002. It exists here rather
than in Task 006 because the schema and the claim query must be designed together
-- an index that does not serve the claim query is wasted, and a claim query that
cannot be expressed safely would force a schema change.

The pattern (docs/DECISION_LOG.md D-024)::

    BEGIN
      SELECT ids ... FOR UPDATE SKIP LOCKED   -- inside a CTE
      UPDATE those rows with a lease          -- same statement, same transaction
    COMMIT                                    -- locks released here
      ... network test happens OUTSIDE any transaction ...
    BEGIN
      write observation, reschedule, clear lease
    COMMIT

The critical rule: **never hold a transaction open while waiting on an MTProto
network timeout.** With ``FOR UPDATE`` alone, a second worker blocks until the
first commits; if the first is sitting in an 8-second proxy handshake, the second
is idle for 8 seconds. ``SKIP LOCKED`` makes it take different rows instead.

The lease (``test_lock_until``) is what makes a crashed worker self-healing: a
process killed with ``kill -9`` never clears its claim, but the lease expires and
the row becomes claimable again. Without it, one crash would strand proxies
permanently.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.logger import get_logger
from core.models import DEFAULT_LEASE_SECONDS, Proxy, utcnow

__all__ = ["claim_due_proxies"]

_logger = get_logger("modules.scheduling")


async def claim_due_proxies(
    session: AsyncSession,
    *,
    limit: int = 25,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    now: datetime | None = None,
) -> list[Proxy]:
    """Atomically lease up to ``limit`` due proxies for this worker.

    One statement, one round trip: a CTE selects and locks the candidates with
    ``FOR UPDATE SKIP LOCKED``, and the outer ``UPDATE`` applies the lease and
    returns the claimed rows. Rows another worker already holds are skipped rather
    than waited on, so N tester processes partition the work instead of
    serialising on it.

    Two guarantees, worth keeping separate:

    * **Selection is fair.** The CTE's ``ORDER BY next_test_at, id LIMIT n`` picks
      the most-overdue rows, so a proxy cannot be starved by a stream of new
      arrivals. This is enforced by the database.
    * **The returned list is sorted by due time.** This is enforced in Python,
      because ``UPDATE ... FROM ... RETURNING`` does not preserve CTE ordering --
      see the comment at the sort below.

    The caller owns the transaction boundary. Commit **before** doing any network
    I/O::

        async with db.session_scope() as session:      # commits on exit
            proxies = await claim_due_proxies(session, limit=25)
        results = await test_all(proxies)              # no transaction held

    A claim that is never completed simply expires: the row becomes eligible again
    once ``test_lock_until`` is in the past. ``test_attempts`` is incremented here
    so a proxy that repeatedly kills workers leaves a visible trail.

    ``now`` is injectable so tests can pin the clock instead of sleeping.
    """
    if limit <= 0:
        msg = f"limit must be positive, got {limit}"
        raise ValueError(msg)
    if lease_seconds <= 0:
        msg = f"lease_seconds must be positive, got {lease_seconds}"
        raise ValueError(msg)

    moment = now or utcnow()
    if moment.tzinfo is None:
        msg = "now must be timezone-aware; use core.models.utcnow()"
        raise ValueError(msg)
    lease_until = moment + timedelta(seconds=lease_seconds)

    table = Proxy.__table__
    candidates = (
        select(table.c.id)
        .where(
            table.c.is_active.is_(True),
            table.c.next_test_at <= moment,
            # An expired lease is treated as free: this is the crash-recovery path.
            or_(table.c.test_lock_until.is_(None), table.c.test_lock_until < moment),
        )
        .order_by(table.c.next_test_at, table.c.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .cte("claim_candidates")
    )

    statement = (
        update(Proxy)
        .where(Proxy.id == candidates.c.id)
        .values(
            test_lock_until=lease_until,
            last_test_started_at=moment,
            test_attempts=Proxy.test_attempts + 1,
        )
        .returning(Proxy)
        # Rows may already be in this session's identity map from an earlier
        # query; without populate_existing they would keep stale lease values.
        .execution_options(populate_existing=True)
    )

    result = await session.execute(statement)
    claimed = list(result.scalars().unique().all())

    # ``UPDATE ... FROM claim_candidates ... RETURNING`` does NOT preserve the
    # CTE's ORDER BY. PostgreSQL joins the CTE to the target table and returns
    # rows in join order, which tracks heap layout -- so the list order varies
    # with what else is in the table. Verified against PostgreSQL 16.2: with six
    # rows inserted in scrambled order and ``limit=3``, the three *oldest* were
    # correctly leased, but came back as p1, p0, p2.
    #
    # Fairness of SELECTION is therefore intact (the CTE's ORDER BY ... LIMIT
    # decides which rows get leased, so nothing can be starved). Only the order
    # of the returned list was arbitrary. Sorting here makes it deterministic:
    # a tester working the batch in order then handles the most-overdue proxies
    # first, so being killed mid-batch still leaves the most-starved rows done.
    # Cost is negligible -- at most `limit` rows, default 25.
    claimed.sort(key=lambda proxy: (proxy.next_test_at, proxy.id))

    _logger.debug(
        "proxies_claimed",
        requested=limit,
        claimed=len(claimed),
        proxy_ids=[proxy.id for proxy in claimed],
        lease_seconds=lease_seconds,
    )
    return claimed
