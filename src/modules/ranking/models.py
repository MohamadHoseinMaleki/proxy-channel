"""Public ranking types. No SQLAlchemy, no I/O, no secrets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

__all__ = [
    "ProxyListing",
    "RankingPage",
]


@dataclass(frozen=True, slots=True)
class ProxyListing:
    """One ranked proxy, safe to serialise or log.

    Deliberately omits secret, fingerprint, raw URLs, and internal error text.
    ``secret_type`` is the wire-format class (``legacy`` / ``dd`` / ``ee``),
    never the secret itself.
    """

    proxy_id: int
    server: str
    port: int
    secret_type: str
    score: Decimal
    reliability_1h: Decimal | None
    reliability_6h: Decimal | None
    reliability_24h: Decimal | None
    latency_p50_ms: Decimal | None
    latency_p95_ms: Decimal | None
    sample_count_24h: int
    scoring_version: str
    scored_at: datetime

    def to_public_dict(self) -> dict[str, Any]:
        """JSON-ready mapping. Decimals are strings so ranking stays exact."""
        return {
            "proxy_id": self.proxy_id,
            "server": self.server,
            "port": self.port,
            "secret_type": self.secret_type,
            "score": str(self.score),
            "reliability_1h": None if self.reliability_1h is None else str(self.reliability_1h),
            "reliability_6h": None if self.reliability_6h is None else str(self.reliability_6h),
            "reliability_24h": None if self.reliability_24h is None else str(self.reliability_24h),
            "latency_p50_ms": None if self.latency_p50_ms is None else str(self.latency_p50_ms),
            "latency_p95_ms": None if self.latency_p95_ms is None else str(self.latency_p95_ms),
            "sample_count_24h": self.sample_count_24h,
            "scoring_version": self.scoring_version,
            "scored_at": self.scored_at.isoformat(),
        }

    def __repr__(self) -> str:
        return (
            f"<ProxyListing proxy_id={self.proxy_id} {self.server}:{self.port} "
            f"type={self.secret_type} score={self.score} n24={self.sample_count_24h}>"
        )


@dataclass(frozen=True, slots=True)
class RankingPage:
    """Bounded first page of the current ranking."""

    items: tuple[ProxyListing, ...]
    as_of: datetime
    limit: int
    max_age_hours: float
    scoring_version: str

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "limit": self.limit,
            "max_age_hours": self.max_age_hours,
            "scoring_version": self.scoring_version,
            "count": len(self.items),
            "items": [item.to_public_dict() for item in self.items],
        }

    def __repr__(self) -> str:
        return (
            f"<RankingPage count={len(self.items)} limit={self.limit} "
            f"version={self.scoring_version}>"
        )
