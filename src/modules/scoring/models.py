"""Domain types for deterministic proxy scoring.

These are plain dataclasses. They must not import SQLAlchemy, Telethon, or
perform I/O. The calculator consumes :class:`ObservationInput` values derived
from persisted ``ProxyObservation`` rows and emits a :class:`ScoreBreakdown`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from core.models import SCORING_VERSION_V1

__all__ = [
    "ObservationInput",
    "ScoreBreakdown",
    "ScoreFreshness",
    "ScoreStatus",
]


class ScoreStatus(StrEnum):
    """Why a score looks the way it does.

    Distinct from :class:`~core.models.ErrorCategory`: scoring never fabricates
    a network result. These states describe *sample availability*, not a probe.
    """

    SCORED = "SCORED"
    NO_OBSERVATIONS_IN_WINDOW = "NO_OBSERVATIONS_IN_WINDOW"


class ScoreFreshness(StrEnum):
    """Derived last-success age. Not stored on ``proxy_scores`` (D-045).

    Ranking already treats snapshots older than 24 h as ineligible (D-040).
    This label is explainability only and does not change ``score``.
    """

    RECENT = "RECENT"  # last GetConfig success younger than 6 h
    AGING = "AGING"  # last success in the 6-24 h lookback
    STALE = "STALE"  # no success in the 24 h window


@dataclass(frozen=True, slots=True)
class ObservationInput:
    """One persisted tester observation, stripped to scoring-relevant fields.

    ``success`` is the Task 004.1 source of truth: ``True`` only when
    unauthenticated ``help.getConfig`` returned a Telegram ``Config``. TCP
    connect and ``client.connect()`` alone are not success. Failures must not
    be turned into latency samples.
    """

    observed_at: datetime
    success: bool
    mtproto_connect_ms: float | None = None
    tcp_connect_ms: float | None = None
    total_latency_ms: float | None = None
    error_category: str | None = None

    def __repr__(self) -> str:
        return (
            f"<ObservationInput success={self.success} "
            f"mtp={self.mtproto_connect_ms} cat={self.error_category or '-'}>"
        )


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    """Explainable result of one scoring run for one proxy.

    ``score`` is what ranking uses. Windowed ``reliability_*`` / ``sample_count_*``
    / latency percentiles match the existing ``proxy_scores`` snapshot columns
    so this object can be persisted without a schema change.
    """

    proxy_id: int
    score: Decimal
    reliability_score: Decimal
    latency_score: Decimal
    confidence_score: Decimal
    confidence_factor: float
    reliability_1h: Decimal | None
    reliability_6h: Decimal | None
    reliability_24h: Decimal | None
    latency_p50_ms: Decimal | None
    latency_p95_ms: Decimal | None
    sample_count_1h: int
    sample_count_6h: int
    sample_count_24h: int
    observation_count: int
    successful_count: int
    weighted_success_rate: float
    #: Reference time the snapshot was computed against. Required so a
    #: ScoreBreakdown cannot silently stamp the wall clock.
    calculated_at: datetime
    failure_counts: tuple[tuple[str, int], ...] = ()
    status: ScoreStatus = ScoreStatus.SCORED
    scoring_version: str = SCORING_VERSION_V1
    #: Newest successful ``observed_at`` in the lookback. Not persisted.
    last_success_at: datetime | None = None
    #: Recency-weighted mean of successful ``mtproto_connect_ms``. Not persisted.
    mean_mtproto_ms: Decimal | None = None
    freshness: ScoreFreshness = ScoreFreshness.STALE

    def __repr__(self) -> str:
        return (
            f"<ScoreBreakdown proxy_id={self.proxy_id} score={self.score} "
            f"rel={self.reliability_score} lat={self.latency_score} "
            f"conf={self.confidence_score} n={self.observation_count} "
            f"status={self.status}>"
        )
