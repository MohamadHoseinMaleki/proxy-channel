"""Publishability policy. Not a second scoring formula.

Constants live here so two callers cannot silently fork eligibility.
``REPORT_*`` settings may override the numeric knobs; ``scoring_version``
stays ``v1`` (D-038 / D-046). Ranking (D-040) is a different contract:
it may list failed-only histories without secrets. Reporting must not.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final

from core.models import SCORING_VERSION_V1
from modules.discovery.models import SecretType
from modules.scoring.calculator import LOOKBACK_HOURS, WINDOW_6H
from modules.scoring.models import ScoreFreshness

__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "MAX_SUCCESS_AGE_HOURS",
    "PUBLISHABLE_SECRET_TYPES",
    "SERVING_VERSION",
    "coerce_limit",
    "is_publishable_secret_type",
    "label_freshness",
    "require_aware",
    "score_is_finite",
    "success_cutoff",
]

#: Latest v1 snapshot only. Ranking uses the same version.
SERVING_VERSION: Final = SCORING_VERSION_V1

#: Default recent-success window. Matches scoring ``RECENT`` / 6 h half-life,
#: which is stricter than ranking's 24 h snapshot age (D-040).
MAX_SUCCESS_AGE_HOURS: Final = WINDOW_6H

DEFAULT_LIMIT: Final = 20
MAX_LIMIT: Final = 100

#: Fake-TLS (``ee``) is never publishable: Telethon cannot verify it (D-044).
PUBLISHABLE_SECRET_TYPES: Final[frozenset[str]] = frozenset(
    {
        SecretType.LEGACY.value,
        SecretType.SECURE_RANDOMIZED.value,
    }
)


def coerce_limit(
    limit: int | None,
    *,
    default: int = DEFAULT_LIMIT,
    maximum: int = MAX_LIMIT,
) -> int:
    """Reject unbounded or inverted pages. ``None`` means ``default``."""
    if limit is None:
        return default
    if isinstance(limit, bool) or not isinstance(limit, int):
        msg = "limit must be an int"
        raise TypeError(msg)
    if limit < 1 or limit > maximum:
        msg = f"limit must be in 1..{maximum}, got {limit}"
        raise ValueError(msg)
    return limit


def require_aware(as_of: datetime, *, name: str = "as_of") -> datetime:
    if as_of.tzinfo is None:
        msg = f"{name} must be timezone-aware; use core.models.utcnow()"
        raise ValueError(msg)
    return as_of


def success_cutoff(as_of: datetime, *, max_age_hours: float = MAX_SUCCESS_AGE_HOURS) -> datetime:
    require_aware(as_of)
    return as_of - timedelta(hours=max_age_hours)


def is_publishable_secret_type(secret_type: str) -> bool:
    return secret_type in PUBLISHABLE_SECRET_TYPES


def score_is_finite(score: Decimal) -> bool:
    """Reject NaN / Inf / out-of-range values the CHECK might not have seen."""
    if score.is_nan() or score.is_infinite():
        return False
    return Decimal("0") <= score <= Decimal("100")


def label_freshness(last_success_at: datetime, as_of: datetime) -> ScoreFreshness:
    """Explainability only. Eligibility uses ``max_success_age_hours``."""
    require_aware(as_of)
    require_aware(last_success_at, name="last_success_at")
    age_hours = (as_of - last_success_at).total_seconds() / 3600.0
    if age_hours < WINDOW_6H:
        return ScoreFreshness.RECENT
    if age_hours <= LOOKBACK_HOURS:
        return ScoreFreshness.AGING
    return ScoreFreshness.STALE
