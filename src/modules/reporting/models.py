"""Internal reporting types. SQLAlchemy stays out of this module.

``ReportItem.secret`` is a :class:`~core.identity.ProxySecret`. Plaintext
is only for the JSON/TXT publishing payload via :meth:`ReportItem.to_json_dict`
and :meth:`Report.to_txt`. ``repr`` / ``str`` never include it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from core.identity import ProxySecret
from modules.scoring.models import ScoreFreshness

__all__ = [
    "JSON_ITEM_KEYS",
    "JSON_REPORT_KEYS",
    "Report",
    "ReportItem",
]

JSON_REPORT_KEYS: Final[tuple[str, ...]] = (
    "generated_at",
    "scoring_version",
    "max_success_age_hours",
    "limit",
    "count",
    "proxies",
)

JSON_ITEM_KEYS: Final[tuple[str, ...]] = (
    "server",
    "port",
    "secret",
    "protocol",
    "secret_type",
    "score",
    "scoring_version",
    "reliability_24h",
    "sample_count_24h",
    "latency_p50_ms",
    "latency_p95_ms",
    "last_success_at",
    "freshness",
)


def _dec(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


@dataclass(frozen=True, slots=True)
class ReportItem:
    """One publishable proxy. Not an ORM row."""

    proxy_id: int
    server: str
    port: int
    secret: ProxySecret
    protocol: str
    secret_type: str
    fingerprint: str
    score: Decimal
    scoring_version: str
    reliability_24h: Decimal | None
    sample_count_24h: int
    latency_p50_ms: Decimal | None
    latency_p95_ms: Decimal | None
    last_success_at: datetime
    freshness: ScoreFreshness
    url: str

    def to_json_dict(self) -> dict[str, Any]:
        """Internal publishing payload. Contains the plaintext secret."""
        return {
            "server": self.server,
            "port": self.port,
            "secret": self.secret.reveal(),
            "protocol": self.protocol,
            "secret_type": self.secret_type,
            "score": str(self.score),
            "scoring_version": self.scoring_version,
            "reliability_24h": _dec(self.reliability_24h),
            "sample_count_24h": self.sample_count_24h,
            "latency_p50_ms": _dec(self.latency_p50_ms),
            "latency_p95_ms": _dec(self.latency_p95_ms),
            "last_success_at": self.last_success_at.isoformat(),
            "freshness": str(self.freshness),
        }

    def __repr__(self) -> str:
        return (
            f"<ReportItem proxy_id={self.proxy_id} {self.server}:{self.port} "
            f"type={self.secret_type} score={self.score} "
            f"secret={self.secret.masked} freshness={self.freshness}>"
        )


@dataclass(frozen=True, slots=True)
class Report:
    """Bounded, ordered selection for a future publisher."""

    items: tuple[ReportItem, ...]
    generated_at: datetime
    limit: int
    max_success_age_hours: float
    scoring_version: str

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "scoring_version": self.scoring_version,
            "max_success_age_hours": self.max_success_age_hours,
            "limit": self.limit,
            "count": len(self.items),
            "proxies": [item.to_json_dict() for item in self.items],
        }

    def to_json(self) -> str:
        """UTF-8 JSON object. Field order is insertion order."""
        return json.dumps(self.to_json_dict(), ensure_ascii=False, separators=(",", ":"))

    def to_txt(self) -> str:
        """One canonical ``tg://proxy?...`` URL per line. Empty report → ``\"\"``."""
        if not self.items:
            return ""
        return "\n".join(item.url for item in self.items) + "\n"

    def __repr__(self) -> str:
        return f"<Report count={len(self.items)} limit={self.limit} version={self.scoring_version}>"
