"""Domain and result models for MTProto proxy connectivity testing.

Guarantees:
* Distinguishes TCP latency, MTProto latency, and total end-to-end latency.
* Holds safe, scrubbed error categories and messages.
* Exposes transport types and connection outcome stages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from core.models import ErrorCategory, utcnow

__all__ = [
    "TesterResult",
    "TransportType",
]


class TransportType(StrEnum):
    """The transport protocol used to communicate with an MTProto proxy."""

    ABRIDGED = "abridged"
    INTERMEDIATE = "intermediate"
    RANDOMIZED_INTERMEDIATE = "randomized_intermediate"
    FAKE_TLS = "fake_tls"


@dataclass(frozen=True, slots=True)
class TesterResult:
    """The immutable outcome of an MTProto connectivity probe.

    All latency figures are in milliseconds (monotonic clock).
    """

    proxy_id: int
    success: bool
    tcp_connect_ms: float | None = None
    mtproto_connect_ms: float | None = None
    total_latency_ms: float | None = None
    error_category: ErrorCategory | None = None
    error_message_safe: str | None = None
    transport_type: TransportType = TransportType.RANDOMIZED_INTERMEDIATE
    target_ip: str | None = None
    tested_at: datetime = field(default_factory=utcnow)

    def __repr__(self) -> str:
        status = "SUCCESS" if self.success else f"FAILED({self.error_category})"
        latencies = (
            f"tcp={self.tcp_connect_ms:.1f}ms "
            f"mtp={self.mtproto_connect_ms:.1f}ms "
            f"total={self.total_latency_ms:.1f}ms"
            if self.success and self.total_latency_ms is not None
            else f"err={self.error_category}"
        )
        return f"<TesterResult proxy_id={self.proxy_id} {status} {latencies}>"
