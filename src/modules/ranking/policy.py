"""Serving policy constants and pure helpers.

Kept in code, not env, so two callers cannot silently fork eligibility
(D-040). ``SCORER_BATCH_SIZE`` remains the only scoring setting; ranking
does not add a competing health score.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final

from core.identity import ProxySecret
from core.models import SCORING_VERSION_V1
from modules.discovery.models import SecretType

__all__ = [
    "DEFAULT_LIMIT",
    "MAX_AGE_HOURS",
    "MAX_LIMIT",
    "SERVING_VERSION",
    "TYPE_UNKNOWN",
    "classify_secret_type",
    "coerce_limit",
    "freshness_cutoff",
    "require_aware",
]

#: Matches the v1 scoring lookback. A snapshot older than the window it was
#: computed over is not current enough to serve. Not a second scoring formula.
MAX_AGE_HOURS: Final = 24.0

DEFAULT_LIMIT: Final = 20
MAX_LIMIT: Final = 100

#: Rankings only serve the current scoring version.
SERVING_VERSION: Final = SCORING_VERSION_V1

TYPE_UNKNOWN: Final = "unknown"


def coerce_limit(limit: int | None) -> int:
    """Reject unbounded or inverted pages. ``None`` means the default."""
    if limit is None:
        return DEFAULT_LIMIT
    if isinstance(limit, bool) or not isinstance(limit, int):
        msg = "limit must be an int"
        raise TypeError(msg)
    if limit < 1 or limit > MAX_LIMIT:
        msg = f"limit must be in 1..{MAX_LIMIT}, got {limit}"
        raise ValueError(msg)
    return limit


def require_aware(as_of: datetime, *, name: str = "as_of") -> datetime:
    if as_of.tzinfo is None:
        msg = f"{name} must be timezone-aware; use core.models.utcnow()"
        raise ValueError(msg)
    return as_of


def freshness_cutoff(as_of: datetime, *, max_age_hours: float = MAX_AGE_HOURS) -> datetime:
    require_aware(as_of)
    return as_of - timedelta(hours=max_age_hours)


def classify_secret_type(secret: ProxySecret) -> str:
    """Wire-format class of a stored secret, without exposing plaintext.

    Classification uses decoded identity bytes. Unknown structures become
    ``unknown`` rather than raising — ranking must not fail a listing because
    discovery's parser is stricter than identity.
    """
    raw = secret.identity_bytes
    if len(raw) == 16:
        return SecretType.LEGACY.value
    if raw and raw[0] == 0xDD:
        return SecretType.SECURE_RANDOMIZED.value
    if raw and raw[0] == 0xEE:
        return SecretType.FAKE_TLS.value
    return TYPE_UNKNOWN
