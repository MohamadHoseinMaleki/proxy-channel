"""In-process publication counters. No Prometheus, no network, no I/O."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["PublicationMetrics"]


@dataclass(slots=True)
class PublicationMetrics:
    """Cumulative counters for one publisher process (or one test).

    Gauges (pending/sending/…) live in the database and are read by
    :class:`~modules.publishing.health.PublicationHealthService`.
    """

    publications_scheduled_total: int = 0
    publications_rejected_total: int = 0
    publication_retries_total: int = 0
    publication_failures_total: int = 0
    publication_success_total: int = 0
    telegram_rate_limits_total: int = 0

    def inc_scheduled(self, n: int = 1) -> None:
        self.publications_scheduled_total += n

    def inc_rejected(self, n: int = 1) -> None:
        self.publications_rejected_total += n

    def inc_retries(self, n: int = 1) -> None:
        self.publication_retries_total += n

    def inc_failures(self, n: int = 1) -> None:
        self.publication_failures_total += n

    def inc_success(self, n: int = 1) -> None:
        self.publication_success_total += n

    def inc_rate_limits(self, n: int = 1) -> None:
        self.telegram_rate_limits_total += n

    def as_dict(self) -> dict[str, int]:
        return {
            "publications_scheduled_total": self.publications_scheduled_total,
            "publications_rejected_total": self.publications_rejected_total,
            "publication_retries_total": self.publication_retries_total,
            "publication_failures_total": self.publication_failures_total,
            "publication_success_total": self.publication_success_total,
            "telegram_rate_limits_total": self.telegram_rate_limits_total,
        }
