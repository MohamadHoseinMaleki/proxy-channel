"""Scoring service: claim due proxies, load observations, persist snapshots.

Architectural invariants:
1. Raw ``ProxyObservation`` rows are never updated or deleted.
2. Each run inserts a new ``ProxyScore`` snapshot (append-only, D-022).
3. There is no network I/O. Computation is pure (:func:`score_observations`).
4. Concurrent workers partition work with ``FOR UPDATE SKIP LOCKED`` on
   ``proxies``. No second lock table, no Redis, and ``test_lock_until`` is
   not reused (that lease belongs to the tester). Duplicate snapshots of
   the same generation are prevented by the row lock plus the claim
   predicate, not by a UNIQUE constraint (D-022).
5. The row lock is held only for the short read-compute-insert transaction.
   A crash rolls back and another worker can take the row immediately.
   Scoring never writes ``next_test_at``, ``test_lock_until``, or
   observations — it is not a second tester scheduler.
6. Logs carry ``proxy_id`` only — never secrets, DSNs, or ``tg://`` URLs.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import Database
from core.logger import get_logger, safe_error_message
from core.models import SCORING_VERSION_V1, Proxy, ProxyObservation, ProxyScore, utcnow
from modules.scoring.calculator import LOOKBACK_HOURS, score_observations
from modules.scoring.models import ObservationInput, ScoreBreakdown

__all__ = ["ScoringService"]

_logger = get_logger("modules.scoring.service")

DEFAULT_BATCH_SIZE = 50


class ScoringService:
    """Orchestrates one scoring pass over proxies that have new observations."""

    def __init__(self, db: Database, *, batch_size: int = DEFAULT_BATCH_SIZE) -> None:
        if batch_size <= 0:
            msg = f"batch_size must be positive, got {batch_size}"
            raise ValueError(msg)
        self.db = db
        self.batch_size = batch_size

    async def run_batch(self) -> list[ScoreBreakdown]:
        """Claim due proxies, score them, persist snapshots.

        One short transaction: lock rows, read observations, compute, insert.
        Scoring is in-process arithmetic — not external work — so holding the
        row lock across it is safe and is what makes concurrent workers
        partition without a dedicated score lease column.
        """
        async with self.db.session_scope() as session:
            claimed = await self.claim_due_proxies(session, limit=self.batch_size)
            if not claimed:
                return []

            proxy_ids = [proxy.id for proxy in claimed]
            _logger.info("scorer_batch_claimed", count=len(proxy_ids), proxy_ids=proxy_ids)

            now = utcnow()
            cutoff = now - timedelta(hours=LOOKBACK_HOURS)
            grouped = await self._load_observations(session, proxy_ids, cutoff)

            results: list[ScoreBreakdown] = []
            for proxy_id in proxy_ids:
                try:
                    breakdown = score_observations(proxy_id, grouped.get(proxy_id, ()), now=now)
                    session.add(_to_proxy_score(breakdown))
                    results.append(breakdown)
                except Exception as exc:
                    _logger.error(
                        "scorer_proxy_failed",
                        proxy_id=proxy_id,
                        error=safe_error_message(exc),
                        exception_type=type(exc).__name__,
                    )
                    raise

            _logger.info(
                "scorer_batch_completed",
                total=len(results),
                scored=sum(1 for item in results if item.observation_count > 0),
            )
            return results

    async def claim_due_proxies(self, session: AsyncSession, *, limit: int) -> list[Proxy]:
        """Lock up to ``limit`` active proxies whose tests are newer than their latest v1 score."""
        already_scored = exists().where(
            ProxyScore.proxy_id == Proxy.id,
            ProxyScore.scoring_version == SCORING_VERSION_V1,
            ProxyScore.calculated_at >= Proxy.last_test_finished_at,
        )
        statement = (
            select(Proxy)
            .where(
                Proxy.is_active.is_(True),
                Proxy.last_test_finished_at.is_not(None),
                ~already_scored,
            )
            .order_by(Proxy.last_test_finished_at, Proxy.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        result = await session.execute(statement)
        claimed = list(result.scalars().unique().all())
        claimed.sort(
            key=lambda proxy: (
                proxy.last_test_finished_at or proxy.created_at,
                proxy.id,
            )
        )
        return claimed

    async def _load_observations(
        self,
        session: AsyncSession,
        proxy_ids: Sequence[int],
        cutoff: datetime,
    ) -> dict[int, list[ObservationInput]]:
        if not proxy_ids:
            return {}
        statement = select(ProxyObservation).where(
            ProxyObservation.proxy_id.in_(proxy_ids),
            ProxyObservation.observed_at >= cutoff,
        )
        rows = (await session.execute(statement)).scalars().all()
        grouped: dict[int, list[ObservationInput]] = {proxy_id: [] for proxy_id in proxy_ids}
        for row in rows:
            grouped[row.proxy_id].append(
                ObservationInput(
                    observed_at=row.observed_at,
                    success=row.success,
                    mtproto_connect_ms=row.mtproto_connect_ms,
                    tcp_connect_ms=row.tcp_connect_ms,
                    total_latency_ms=row.total_latency_ms,
                    error_category=row.error_category,
                )
            )
        return grouped


def _to_proxy_score(breakdown: ScoreBreakdown) -> ProxyScore:
    return ProxyScore(
        proxy_id=breakdown.proxy_id,
        calculated_at=breakdown.calculated_at,
        score=breakdown.score,
        reliability_1h=breakdown.reliability_1h,
        reliability_6h=breakdown.reliability_6h,
        reliability_24h=breakdown.reliability_24h,
        latency_p50_ms=breakdown.latency_p50_ms,
        latency_p95_ms=breakdown.latency_p95_ms,
        sample_count_1h=breakdown.sample_count_1h,
        sample_count_6h=breakdown.sample_count_6h,
        sample_count_24h=breakdown.sample_count_24h,
        scoring_version=breakdown.scoring_version,
    )
