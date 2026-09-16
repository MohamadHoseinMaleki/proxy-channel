"""Public Pydantic DTOs for the ranking API. No ORM models."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from modules.ranking.models import ProxyListing, RankingPage
from modules.ranking.policy import DEFAULT_LIMIT, SERVING_VERSION

__all__ = [
    "ErrorBody",
    "HealthResponse",
    "ProxyListingResponse",
    "RankingResponse",
    "ReadyResponse",
]


class HealthResponse(BaseModel):
    """Liveness: the process is up. No dependency checks."""

    model_config = ConfigDict(frozen=True)

    status: str = "ok"


class ReadyResponse(BaseModel):
    """Readiness: ranking dependencies are reachable."""

    model_config = ConfigDict(frozen=True)

    status: str


class ErrorBody(BaseModel):
    """Generic client-visible error. Never includes exception text."""

    model_config = ConfigDict(frozen=True)

    detail: str


class ProxyListingResponse(BaseModel):
    """One ranked proxy. Field names match Task 006 ``ProxyListing``."""

    model_config = ConfigDict(frozen=True)

    proxy_id: int
    server: str
    port: int
    secret_type: str
    score: str
    reliability_1h: str | None
    reliability_6h: str | None
    reliability_24h: str | None
    latency_p50_ms: str | None
    latency_p95_ms: str | None
    sample_count_24h: int
    scoring_version: str
    scored_at: datetime

    @classmethod
    def from_listing(cls, listing: ProxyListing) -> ProxyListingResponse:
        payload = listing.to_public_dict()
        return cls.model_validate(payload)


class RankingResponse(BaseModel):
    """Bounded first page. Omits ``as_of`` so clients cannot replay freshness."""

    model_config = ConfigDict(frozen=True)

    items: list[ProxyListingResponse] = Field(default_factory=list)
    count: int
    limit: int = DEFAULT_LIMIT
    scoring_version: str = SERVING_VERSION

    @classmethod
    def from_page(cls, page: RankingPage) -> RankingResponse:
        return cls(
            items=[ProxyListingResponse.from_listing(item) for item in page.items],
            count=len(page.items),
            limit=page.limit,
            scoring_version=page.scoring_version,
        )
