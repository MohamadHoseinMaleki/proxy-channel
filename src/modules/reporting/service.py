"""Read-only reporting over persisted scores and observations.

Architectural invariants:
1. Task 005 scores are consumed as-is. This module never recalculates them.
2. Observations, proxies, and scores are never updated or deleted.
3. No network I/O, no DNS, no tester/scorer scheduling writes.
4. Secrets never appear in logs, ``repr``, or exception text.
5. Publishability is stricter than ranking (D-046): recent GetConfig success,
   no Fake-TLS, no CANCELLED-as-success.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings
from core.database import Database
from core.logger import get_logger, safe_error_message
from core.models import ErrorCategory, Proxy, ProxyObservation, ProxyScore, utcnow
from modules.discovery.parser import ProxyParseError, parse_proxy_url
from modules.reporting.models import Report, ReportItem
from modules.reporting.policy import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MAX_SUCCESS_AGE_HOURS,
    SERVING_VERSION,
    coerce_limit,
    require_aware,
)
from modules.reporting.selector import PublishCandidate, select_publishable

__all__ = ["ReportingService"]

_logger = get_logger("modules.reporting.service")


class ReportingService:
    """Selects currently publishable proxies from PostgreSQL."""

    def __init__(self, db: Database, *, settings: Settings | None = None) -> None:
        self.db = db
        self.default_limit = (
            settings.report_default_limit if settings is not None else DEFAULT_LIMIT
        )
        self.max_limit = settings.report_max_limit if settings is not None else MAX_LIMIT
        self.max_success_age_hours = (
            settings.report_max_success_age_hours if settings is not None else MAX_SUCCESS_AGE_HOURS
        )

    async def select_top(
        self,
        *,
        limit: int | None = None,
        as_of: datetime | None = None,
        max_success_age_hours: float | None = None,
        scoring_version: str = SERVING_VERSION,
    ) -> Report:
        """Return the bounded ordered set of currently publishable proxies.

        ``as_of`` is the freshness reference. Production callers may omit it;
        the service stamps ``utcnow()`` once. Tests must pass it.
        """
        resolved_limit = coerce_limit(
            limit,
            default=self.default_limit,
            maximum=self.max_limit,
        )
        moment = utcnow() if as_of is None else require_aware(as_of)
        age_hours = (
            self.max_success_age_hours if max_success_age_hours is None else max_success_age_hours
        )

        async with self.db.session_scope() as session:
            candidates = await self._load_candidates(
                session,
                scoring_version=scoring_version,
            )

        selected = select_publishable(
            candidates,
            as_of=moment,
            limit=resolved_limit,
            max_success_age_hours=age_hours,
            scoring_version=scoring_version,
        )
        items = _drop_unserializable(selected)
        _logger.info(
            "reporting_selected",
            count=len(items),
            limit=resolved_limit,
            scoring_version=scoring_version,
            proxy_ids=[item.proxy_id for item in items],
        )
        return Report(
            items=items,
            generated_at=moment,
            limit=resolved_limit,
            max_success_age_hours=age_hours,
            scoring_version=scoring_version,
        )

    async def _load_candidates(
        self,
        session: AsyncSession,
        *,
        scoring_version: str,
    ) -> list[PublishCandidate]:
        """Latest v1 score per active proxy plus latest non-cancelled observation.

        ``DISTINCT ON`` uses ``ix_proxy_scores_proxy_id_calculated_at``.
        Observation recency uses ``ix_proxy_observations_proxy_id_observed_at``.
        No extra index: the recent-success filter already bounds the set.
        """
        latest_score_ids = (
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

        latest_obs = (
            select(
                ProxyObservation.proxy_id,
                ProxyObservation.observed_at,
                ProxyObservation.success,
            )
            .where(ProxyObservation.error_category.is_distinct_from(ErrorCategory.CANCELLED))
            .distinct(ProxyObservation.proxy_id)
            .order_by(
                ProxyObservation.proxy_id.asc(),
                ProxyObservation.observed_at.desc(),
                ProxyObservation.id.desc(),
            )
        ).subquery()

        statement = (
            select(Proxy, ProxyScore, latest_obs.c.observed_at, latest_obs.c.success)
            .join(ProxyScore, Proxy.id == ProxyScore.proxy_id)
            .join(latest_obs, latest_obs.c.proxy_id == Proxy.id)
            .where(
                ProxyScore.id.in_(select(latest_score_ids.c.id)),
                Proxy.is_active.is_(True),
                ProxyScore.sample_count_24h > 0,
            )
        )
        rows = (await session.execute(statement)).all()
        return [
            PublishCandidate(
                proxy_id=proxy.id,
                is_active=proxy.is_active,
                server=proxy.server,
                port=proxy.port,
                secret=proxy.secret,
                protocol=proxy.protocol,
                fingerprint=proxy.fingerprint,
                score_id=score.id,
                score=score.score,
                calculated_at=score.calculated_at,
                scoring_version=score.scoring_version,
                reliability_24h=score.reliability_24h,
                latency_p50_ms=score.latency_p50_ms,
                latency_p95_ms=score.latency_p95_ms,
                sample_count_24h=score.sample_count_24h,
                last_meaningful_at=observed_at,
                last_meaningful_success=bool(success),
            )
            for proxy, score, observed_at, success in rows
        ]


def _drop_unserializable(items: tuple[ReportItem, ...]) -> tuple[ReportItem, ...]:
    """Skip a row that cannot round-trip through the discovery parser.

    Failures are logged by ``proxy_id`` only. The rest of the report is kept
    so one bad identity cannot poison a publishing payload.
    """
    kept: list[ReportItem] = []
    for item in items:
        try:
            parsed = parse_proxy_url(item.url)
        except (ProxyParseError, ValueError) as exc:
            _logger.error(
                "reporting_item_unserializable",
                proxy_id=item.proxy_id,
                error=safe_error_message(exc),
                exception_type=type(exc).__name__,
            )
            continue
        if parsed.fingerprint != item.fingerprint:
            _logger.error("reporting_item_fingerprint_mismatch", proxy_id=item.proxy_id)
            continue
        kept.append(item)
    return tuple(kept)
