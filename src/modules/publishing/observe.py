"""Observability labels for the publication pipeline.

Does not decide retry, claim, or enqueue. Counters and logs may *read*
existing outcomes; they must not change them.
"""

from __future__ import annotations

from enum import StrEnum

from modules.publishing.protocol import PublishResult

__all__ = [
    "EVENT_CLAIMED",
    "EVENT_FAILED",
    "EVENT_PUBLISHED",
    "EVENT_RECOVERED",
    "EVENT_REJECTED",
    "EVENT_RETRY",
    "EVENT_SCHEDULED",
    "EVENT_TELEGRAM_RATE_LIMITED",
    "PublicationErrorClass",
    "classify_publish_result",
]

EVENT_SCHEDULED = "publication_scheduled"
EVENT_REJECTED = "publication_rejected"
EVENT_CLAIMED = "publication_claimed"
EVENT_PUBLISHED = "publication_published"
EVENT_RETRY = "publication_retry"
EVENT_FAILED = "publication_failed"
EVENT_RECOVERED = "publication_recovered"
EVENT_TELEGRAM_RATE_LIMITED = "telegram_rate_limited"


class PublicationErrorClass(StrEnum):
    """Operational class for logs/metrics. Independent of retry policy."""

    VALIDATION = "validation"
    TELEGRAM = "telegram"
    DATABASE = "database"
    CONFIGURATION = "configuration"
    TRANSIENT = "transient"
    PERMANENT = "permanent"


def classify_publish_result(result: PublishResult) -> PublicationErrorClass:
    """Map a send outcome to an observability class. Does not change retry."""
    if result.ok:
        msg = "successful publish has no error class"
        raise ValueError(msg)
    if result.error_code == 429:
        return PublicationErrorClass.TELEGRAM
    if result.retryable:
        return PublicationErrorClass.TRANSIENT
    return PublicationErrorClass.PERMANENT
