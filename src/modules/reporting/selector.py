"""Pure publishable selection. No database, no wall clock, no network."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from core.identity import PROTOCOL_MTPROTO, ProxySecret
from modules.ranking.policy import classify_secret_type
from modules.reporting.models import ReportItem
from modules.reporting.policy import (
    SERVING_VERSION,
    is_publishable_secret_type,
    label_freshness,
    require_aware,
    score_is_finite,
    success_cutoff,
)
from modules.reporting.urls import canonical_tg_proxy_url

__all__ = [
    "PublishCandidate",
    "select_publishable",
]


@dataclass(frozen=True, slots=True)
class PublishCandidate:
    """Latest score joined to identity and the latest meaningful observation."""

    proxy_id: int
    is_active: bool
    server: str
    port: int
    secret: ProxySecret
    protocol: str
    fingerprint: str
    score_id: int
    score: Decimal
    calculated_at: datetime
    scoring_version: str
    reliability_24h: Decimal | None
    latency_p50_ms: Decimal | None
    latency_p95_ms: Decimal | None
    sample_count_24h: int
    #: ``observed_at`` of the latest non-``CANCELLED`` observation.
    last_meaningful_at: datetime
    #: Whether that observation is a GetConfig success.
    last_meaningful_success: bool

    def __repr__(self) -> str:
        return (
            f"<PublishCandidate proxy_id={self.proxy_id} score={self.score} "
            f"success={self.last_meaningful_success} secret={self.secret.masked}>"
        )


def select_publishable(
    candidates: Sequence[PublishCandidate],
    *,
    as_of: datetime,
    limit: int,
    max_success_age_hours: float,
    scoring_version: str = SERVING_VERSION,
) -> tuple[ReportItem, ...]:
    """Latest eligible v1 snapshot per proxy, then deterministic top-N.

    Latest means greatest ``(calculated_at, score_id)`` among ``scoring_version``.
    An unpublishable latest row is omitted; an older snapshot is not a fallback.
    """
    require_aware(as_of)
    cutoff = success_cutoff(as_of, max_age_hours=max_success_age_hours)
    latest = _latest_per_proxy(candidates, scoring_version=scoring_version)
    eligible: list[PublishCandidate] = [
        row for row in latest if _is_publishable(row, cutoff=cutoff, as_of=as_of)
    ]
    eligible.sort(key=_sort_key)
    return tuple(_to_item(row, as_of=as_of) for row in eligible[:limit])


def _latest_per_proxy(
    candidates: Sequence[PublishCandidate],
    *,
    scoring_version: str,
) -> list[PublishCandidate]:
    best: dict[int, PublishCandidate] = {}
    for row in candidates:
        if row.scoring_version != scoring_version:
            continue
        current = best.get(row.proxy_id)
        if current is None or (row.calculated_at, row.score_id) > (
            current.calculated_at,
            current.score_id,
        ):
            best[row.proxy_id] = row
    return list(best.values())


def _is_publishable(
    row: PublishCandidate,
    *,
    cutoff: datetime,
    as_of: datetime,
) -> bool:
    del as_of
    if not row.is_active:
        return False
    if row.sample_count_24h <= 0:
        return False
    if not score_is_finite(row.score):
        return False
    if not row.last_meaningful_success:
        return False
    if row.last_meaningful_at < cutoff:
        return False
    secret_type = classify_secret_type(row.secret)
    return is_publishable_secret_type(secret_type)


def _sort_key(row: PublishCandidate) -> tuple[Decimal, Decimal, Decimal, str]:
    # score DESC, last success DESC, p50 ASC (missing last), fingerprint ASC
    latency = row.latency_p50_ms if row.latency_p50_ms is not None else Decimal("Infinity")
    return (-row.score, -_as_ordinal(row.last_meaningful_at), latency, row.fingerprint)


def _as_ordinal(moment: datetime) -> Decimal:
    """Timezone-aware datetime as a monotonic Decimal for sorting."""
    return Decimal(str(moment.timestamp()))


def _to_item(row: PublishCandidate, *, as_of: datetime) -> ReportItem:
    secret_type = classify_secret_type(row.secret)
    url = canonical_tg_proxy_url(server=row.server, port=row.port, secret=row.secret)
    return ReportItem(
        proxy_id=row.proxy_id,
        server=row.server,
        port=row.port,
        secret=row.secret,
        protocol=row.protocol or PROTOCOL_MTPROTO,
        secret_type=secret_type,
        fingerprint=row.fingerprint,
        score=row.score,
        scoring_version=row.scoring_version,
        reliability_24h=row.reliability_24h,
        sample_count_24h=row.sample_count_24h,
        latency_p50_ms=row.latency_p50_ms,
        latency_p95_ms=row.latency_p95_ms,
        last_success_at=row.last_meaningful_at,
        freshness=label_freshness(row.last_meaningful_at, as_of),
        url=url,
    )
