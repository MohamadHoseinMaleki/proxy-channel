"""Deterministic proxy scoring layer."""

from __future__ import annotations

from modules.scoring.calculator import (
    CONFIDENCE_PRIOR_N,
    LATENCY_WEIGHT,
    LATENCY_WORST_MS,
    LOOKBACK_HOURS,
    RECENCY_HALF_LIFE_HOURS,
    RELIABILITY_WEIGHT,
    score_observations,
)
from modules.scoring.models import ObservationInput, ScoreBreakdown, ScoreStatus
from modules.scoring.service import ScoringService

__all__ = [
    "CONFIDENCE_PRIOR_N",
    "LATENCY_WEIGHT",
    "LATENCY_WORST_MS",
    "LOOKBACK_HOURS",
    "RECENCY_HALF_LIFE_HOURS",
    "RELIABILITY_WEIGHT",
    "ObservationInput",
    "ScoreBreakdown",
    "ScoreStatus",
    "ScoringService",
    "score_observations",
]
