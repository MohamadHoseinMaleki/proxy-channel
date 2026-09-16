"""Read-only ranking over persisted ``ProxyScore`` snapshots.

Architectural invariants:
1. Task 005 scores are consumed as-is. This module never recalculates them.
2. Observations, proxies, and scores are never updated or deleted.
3. No network I/O, no DNS, no tester/scorer scheduling writes.
4. Secrets never appear on :class:`ProxyListing` or in logs.
5. Pagination is a bounded first page (``limit``). OFFSET and caller-supplied
   ORDER BY are rejected by not existing.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import Database
from core.logger import get_logger
from core.models import Proxy, ProxyScore, utcnow
from modules.ranking.models import RankingPage
from modules.ranking.policy import (
    DEFAULT_LIMIT,
    MAX_AGE_HOURS,
    SERVING_VERSION,
    classify_secret_type,
    coerce_limit,
    freshness_cutoff,
    require_aware,
)
from modules.ranking.selector import ScoreSnapshot, rank_snapshots

__all__ = ["RankingService"]

_logger = get_logger("modules.ranking.service")


class RankingService:
    """Serves the current top proxies from PostgreSQL."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def list_top(
        self,
        *,
        limit: int | None = DEFAULT_LIMIT,
        as_of: datetime | None = None,
        max_age_hours: float = MAX_AGE_HOURS,
        scoring_version: str = SERVING_VERSION,
    ) -> RankingPage:
        """Return the bounded first page of the current ranking.

        ``as_of`` is the freshness reference. Production callers may omit it;
        the service stamps ``utcnow()`` once. Tests must pass it.
        """
        resolved_limit = coerce_limit(limit)
        moment = utcnow() if as_of is None else require_aware(as_of)
        cutoff = freshness_cutoff(moment, max_age_hours=max_age_hours)

        async with self.db.session_scope() as session:
            snapshots = await self._load_latest_snapshots(
                session,
                cutoff=cutoff,
                limit=resolved_limit,
                scoring_version=scoring_version,
            )

        items = rank_snapshots(
            snapshots,
            as_of=moment,
            limit=resolved_limit,
            max_age_hours=max_age_hours,
            scoring_version=scoring_version,
        )
        _logger.info(
            "ranking_listed",
            count=len(items),
            limit=resolved_limit,
            scoring_version=scoring_version,
            proxy_ids=[item.proxy_id for item in items],
        )
        return RankingPage(
            items=items,
            as_of=moment,
            limit=resolved_limit,
            max_age_hours=max_age_hours,
            scoring_version=scoring_version,
        )

    async def _load_latest_snapshots(
        self,
        session: AsyncSession,
        *,
        cutoff: datetime,
        limit: int,
        scoring_version: str,
    ) -> list[ScoreSnapshot]:
        """Latest eligible snapshot per active proxy, ordered and bounded.

        PostgreSQL ``DISTINCT ON (proxy_id)`` plus
        ``ORDER BY proxy_id, calculated_at DESC, id DESC`` is the documented
        "latest score" path (D-022, ``ix_proxy_scores_proxy_id_calculated_at``).
        Eligibility filters run on that latest row only. Final order:

        ``score DESC, calculated_at DESC, proxy_id ASC``.
        """
        latest_ids = (
            select(ProxyScore.id)
            .join(Proxy, Proxy.id == ProxyScore.proxy_id)
            .where(
                Proxy.is_active.is_(True),
                ProxyScore.scoring_version == scoring_version,
            )
            .distinct(ProxyScore.proxy_id)
            .order_by(
                ProxyScore.proxy_id.asc(),
                ProxyScore.calculated_at.desc(),
                ProxyScore.id.desc(),
            )
        ).subquery()

        statement = (
            select(Proxy, ProxyScore)
            .join(ProxyScore, Proxy.id == ProxyScore.proxy_id)
            .where(
                ProxyScore.id.in_(select(latest_ids.c.id)),
                Proxy.is_active.is_(True),
                ProxyScore.sample_count_24h > 0,
                ProxyScore.calculated_at >= cutoff,
            )
            .order_by(
                ProxyScore.score.desc(),
                ProxyScore.calculated_at.desc(),
                Proxy.id.asc(),
            )
            .limit(limit)
        )
        rows = (await session.execute(statement)).all()
        return [
            ScoreSnapshot(
                proxy_id=proxy.id,
                is_active=proxy.is_active,
                server=proxy.server,
                port=proxy.port,
                secret_type=classify_secret_type(proxy.secret),
                score_id=score.id,
                score=score.score,
                calculated_at=score.calculated_at,
                scoring_version=score.scoring_version,
                reliability_1h=score.reliability_1h,
                reliability_6h=score.reliability_6h,
                reliability_24h=score.reliability_24h,
                latency_p50_ms=score.latency_p50_ms,
                latency_p95_ms=score.latency_p95_ms,
                sample_count_24h=score.sample_count_24h,
            )
            for proxy, score in rows
        ]
