"""Pure latest-score selection and ranking. No database, no wall clock."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from modules.ranking.models import ProxyListing
from modules.ranking.policy import SERVING_VERSION, freshness_cutoff, require_aware

__all__ = [
    "ScoreSnapshot",
    "rank_snapshots",
]


@dataclass(frozen=True, slots=True)
class ScoreSnapshot:
    """One persisted score row joined to proxy identity, minus the secret."""

    proxy_id: int
    is_active: bool
    server: str
    port: int
    secret_type: str
    score_id: int
    score: Decimal
    calculated_at: datetime
    scoring_version: str
    reliability_1h: Decimal | None
    reliability_6h: Decimal | None
    reliability_24h: Decimal | None
    latency_p50_ms: Decimal | None
    latency_p95_ms: Decimal | None
    sample_count_24h: int


def rank_snapshots(
    snapshots: Sequence[ScoreSnapshot],
    *,
    as_of: datetime,
    limit: int,
    max_age_hours: float,
    scoring_version: str = SERVING_VERSION,
) -> tuple[ProxyListing, ...]:
    """Pick the latest eligible snapshot per proxy and order them.

    Latest means greatest ``(calculated_at, score_id)`` among rows of
    ``scoring_version``. Older generations of the same proxy are discarded,
    not averaged. Eligibility is then applied to that latest row only: if it
    is stale or empty-window, the proxy is absent, not replaced by an older
    snapshot.
    """
    require_aware(as_of)
    cutoff = freshness_cutoff(as_of, max_age_hours=max_age_hours)
    latest = _latest_per_proxy(snapshots, scoring_version=scoring_version)
    eligible = [row for row in latest if _is_serviceable(row, cutoff=cutoff)]
    eligible.sort(key=lambda row: row.proxy_id)
    eligible.sort(key=lambda row: row.calculated_at, reverse=True)
    eligible.sort(key=lambda row: row.score, reverse=True)
    return tuple(_to_listing(row) for row in eligible[:limit])


def _latest_per_proxy(
    snapshots: Sequence[ScoreSnapshot],
    *,
    scoring_version: str,
) -> list[ScoreSnapshot]:
    best: dict[int, ScoreSnapshot] = {}
    for row in snapshots:
        if row.scoring_version != scoring_version:
            continue
        current = best.get(row.proxy_id)
        if current is None or (row.calculated_at, row.score_id) > (
            current.calculated_at,
            current.score_id,
        ):
            best[row.proxy_id] = row
    return list(best.values())


def _is_serviceable(row: ScoreSnapshot, *, cutoff: datetime) -> bool:
    if not row.is_active:
        return False
    if row.sample_count_24h <= 0:
        return False
    return row.calculated_at >= cutoff


def _to_listing(row: ScoreSnapshot) -> ProxyListing:
    return ProxyListing(
        proxy_id=row.proxy_id,
        server=row.server,
        port=row.port,
        secret_type=row.secret_type,
        score=row.score,
        reliability_1h=row.reliability_1h,
        reliability_6h=row.reliability_6h,
        reliability_24h=row.reliability_24h,
        latency_p50_ms=row.latency_p50_ms,
        latency_p95_ms=row.latency_p95_ms,
        sample_count_24h=row.sample_count_24h,
        scoring_version=row.scoring_version,
        scored_at=row.calculated_at,
    )
