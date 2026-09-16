"""Pure, deterministic scoring of persisted MTProto observations.

No network, no database, no randomness. Identical inputs yield identical
outputs regardless of observation order. See ``docs/SCORING.md`` and D-038.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final

from core.models import SCORING_VERSION_V1
from modules.scoring.models import ObservationInput, ScoreBreakdown, ScoreStatus

__all__ = [
    "CONFIDENCE_PRIOR_N",
    "LAPLACE_ALPHA",
    "LAPLACE_BETA",
    "LATENCY_BEST_MS",
    "LATENCY_WEIGHT",
    "LATENCY_WORST_MS",
    "LOOKBACK_HOURS",
    "RECENCY_HALF_LIFE_HOURS",
    "RELIABILITY_WEIGHT",
    "WINDOW_1H",
    "WINDOW_6H",
    "WINDOW_24H",
    "score_observations",
]

# ---------------------------------------------------------------------------
# Formula constants (scoring_version = v1)
# ---------------------------------------------------------------------------

#: Observations older than this are ignored. Matches the 24h snapshot window
#: already stored on ``proxy_scores``. With a 6h half-life, weight at 24h is
#: exp(-4) ≈ 0.018, so cutting here discards almost nothing that still matters.
LOOKBACK_HOURS: Final = 24.0

WINDOW_1H: Final = 1.0
WINDOW_6H: Final = 6.0
WINDOW_24H: Final = 24.0

#: Exponential-decay half-life in hours. The tester reschedules successes 1h
#: forward and failures 15m forward, so 6h covers several recent attempts
#: without letting day-old data dominate. An engineering parameter, not a
#: fitted optimum.
RECENCY_HALF_LIFE_HOURS: Final = 6.0

#: Laplace (uniform) prior added to the decay-weighted success totals.
#: ``(w_success + 1) / (w_n + 2)``. Softens 1/1 = 100% and 0/1 = 0%.
LAPLACE_ALPHA: Final = 1.0
LAPLACE_BETA: Final = 1.0

#: Pseudo-count for the confidence factor ``n / (n + N0)``. N0 = 10 is
#: "roughly ten hourly successes, or a couple of hours of 15-minute failure
#: retries, to reach 50% confidence." 1/1 therefore cannot outrank 100/100.
CONFIDENCE_PRIOR_N: Final = 10.0

#: Mix for the pre-confidence score. Reliability dominates: a fast proxy that
#: rarely completes ``help.getConfig`` is not useful.
RELIABILITY_WEIGHT: Final = 0.75
LATENCY_WEIGHT: Final = 0.25

#: Linear map for latency_score. 0 ms → 100, ``LATENCY_WORST_MS`` → 0.
#: Worst bound is the tester's MTProto timeout budget (8s). Successful
#: ``mtproto_connect_ms`` includes Telethon's ~2s structural floor (D-036);
#: the floor is common to every success so ranking stays meaningful. We do
#: not subtract 2000 ms — that would fabricate a network figure.
LATENCY_BEST_MS: Final = 0.0
LATENCY_WORST_MS: Final = 8000.0

_QUANT: Final = Decimal("0.001")
_ZERO: Final = Decimal("0.000")
_HUNDRED: Final = Decimal("100.000")
_LN2: Final = math.log(2.0)


def score_observations(
    proxy_id: int,
    observations: Sequence[ObservationInput],
    *,
    now: datetime,
) -> ScoreBreakdown:
    """Compute a v1 score snapshot from persisted observations.

    ``now`` is injected so tests (and two workers in the same tick) can pin
    the clock. It must be timezone-aware.
    """
    if now.tzinfo is None:
        msg = "now must be timezone-aware; use core.models.utcnow()"
        raise ValueError(msg)

    in_window = _in_lookback(observations, now)
    failure_counts = _failure_counts(in_window)

    sample_1h, rel_1h = _window_reliability(in_window, now, WINDOW_1H)
    sample_6h, rel_6h = _window_reliability(in_window, now, WINDOW_6H)
    sample_24h, rel_24h = _window_reliability(in_window, now, WINDOW_24H)

    if not in_window:
        return ScoreBreakdown(
            proxy_id=proxy_id,
            score=_ZERO,
            reliability_score=_ZERO,
            latency_score=_ZERO,
            confidence_score=_ZERO,
            confidence_factor=0.0,
            reliability_1h=None,
            reliability_6h=None,
            reliability_24h=None,
            latency_p50_ms=None,
            latency_p95_ms=None,
            sample_count_1h=0,
            sample_count_6h=0,
            sample_count_24h=0,
            observation_count=0,
            successful_count=0,
            weighted_success_rate=0.0,
            failure_counts=failure_counts,
            status=ScoreStatus.NO_OBSERVATIONS_IN_WINDOW,
            scoring_version=SCORING_VERSION_V1,
            calculated_at=now,
        )

    weight_sum = 0.0
    weighted_success = 0.0
    latency_weight_sum = 0.0
    weighted_latency = 0.0
    successful_count = 0
    latency_samples: list[float] = []

    half_life = RECENCY_HALF_LIFE_HOURS
    for item in in_window:
        weight = _recency_weight(item.observed_at, now, half_life)
        weight_sum += weight
        if item.success:
            weighted_success += weight
            successful_count += 1
            latency = _success_latency_ms(item)
            if latency is not None:
                latency_weight_sum += weight
                weighted_latency += weight * latency
                latency_samples.append(latency)

    n = len(in_window)
    raw_rate = (weighted_success / weight_sum) if weight_sum > 0.0 else 0.0
    reliability = (weighted_success + LAPLACE_ALPHA) / (weight_sum + LAPLACE_ALPHA + LAPLACE_BETA)
    reliability_score = 100.0 * reliability

    if latency_weight_sum > 0.0:
        mean_latency = weighted_latency / latency_weight_sum
        latency_score = _latency_score(mean_latency)
        p50, p95 = _percentiles(latency_samples)
    else:
        latency_score = 0.0
        p50, p95 = None, None

    confidence_factor = n / (n + CONFIDENCE_PRIOR_N)
    combined = RELIABILITY_WEIGHT * reliability_score + LATENCY_WEIGHT * latency_score
    final = combined * confidence_factor

    return ScoreBreakdown(
        proxy_id=proxy_id,
        score=_score_decimal(final),
        reliability_score=_score_decimal(reliability_score),
        latency_score=_score_decimal(latency_score),
        confidence_score=_score_decimal(100.0 * confidence_factor),
        confidence_factor=confidence_factor,
        reliability_1h=rel_1h,
        reliability_6h=rel_6h,
        reliability_24h=rel_24h,
        latency_p50_ms=_ms_decimal(p50),
        latency_p95_ms=_ms_decimal(p95),
        sample_count_1h=sample_1h,
        sample_count_6h=sample_6h,
        sample_count_24h=sample_24h,
        observation_count=n,
        successful_count=successful_count,
        weighted_success_rate=raw_rate,
        failure_counts=failure_counts,
        status=ScoreStatus.SCORED,
        scoring_version=SCORING_VERSION_V1,
        calculated_at=now,
    )


def _in_lookback(observations: Sequence[ObservationInput], now: datetime) -> list[ObservationInput]:
    cutoff = now - timedelta(hours=LOOKBACK_HOURS)
    selected = [item for item in observations if item.observed_at >= cutoff]
    # Sort so percentile and failure-count construction never depend on input order.
    selected.sort(key=lambda item: item.observed_at)
    return selected


def _recency_weight(observed_at: datetime, now: datetime, half_life_hours: float) -> float:
    age_hours = (now - observed_at).total_seconds() / 3600.0
    if age_hours < 0.0:
        age_hours = 0.0
    # True half-life: weight is 0.5 at `half_life_hours`, not 1/e.
    return math.exp(-_LN2 * age_hours / half_life_hours)


def _success_latency_ms(item: ObservationInput) -> float | None:
    """Latency used for scoring: successful ``mtproto_connect_ms`` only.

    Timeouts are not converted into a millisecond figure. TCP-only samples
    are not API-verified. Missing values are skipped, not invented.
    """
    if not item.success:
        return None
    value = item.mtproto_connect_ms
    if value is None or value < 0.0:
        return None
    return value


def _latency_score(latency_ms: float) -> float:
    span = LATENCY_WORST_MS - LATENCY_BEST_MS
    if span <= 0.0:
        return 0.0
    unit = (latency_ms - LATENCY_BEST_MS) / span
    if unit <= 0.0:
        return 100.0
    if unit >= 1.0:
        return 0.0
    return 100.0 * (1.0 - unit)


def _window_reliability(
    observations: Sequence[ObservationInput],
    now: datetime,
    hours: float,
) -> tuple[int, Decimal | None]:
    cutoff = now - timedelta(hours=hours)
    window = [item for item in observations if item.observed_at >= cutoff]
    count = len(window)
    if count == 0:
        return 0, None
    successes = sum(1 for item in window if item.success)
    return count, _score_decimal(100.0 * successes / count)


def _percentiles(values: list[float]) -> tuple[float, float]:
    """Linear interpolation over the sorted sample. Order-independent."""
    ordered = sorted(values)
    return _percentile(ordered, 50.0), _percentile(ordered, 95.0)


def _percentile(sorted_values: list[float], percent: float) -> float:
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]
    rank = (percent / 100.0) * (n - 1)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return sorted_values[low]
    frac = rank - low
    return sorted_values[low] * (1.0 - frac) + sorted_values[high] * frac


def _failure_counts(observations: Sequence[ObservationInput]) -> tuple[tuple[str, int], ...]:
    tallies: dict[str, int] = {}
    for item in observations:
        if item.success:
            continue
        key = item.error_category or "UNKNOWN_ERROR"
        tallies[key] = tallies.get(key, 0) + 1
    return tuple(sorted(tallies.items()))


def _score_decimal(value: float) -> Decimal:
    clamped = 0.0 if value < 0.0 else 100.0 if value > 100.0 else value
    quantized = Decimal(str(clamped)).quantize(_QUANT, rounding=ROUND_HALF_EVEN)
    if quantized < _ZERO:
        return _ZERO
    if quantized > _HUNDRED:
        return _HUNDRED
    return quantized


def _ms_decimal(value: float | None) -> Decimal | None:
    if value is None:
        return None
    non_negative = 0.0 if value < 0.0 else value
    return Decimal(str(non_negative)).quantize(_QUANT, rounding=ROUND_HALF_EVEN)
