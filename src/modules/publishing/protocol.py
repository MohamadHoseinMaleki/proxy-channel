"""Telegram publisher contract. No HTTP, no SQLAlchemy.

``publish(message)`` is the only method the service calls. Channel id and
bot token live on the implementation, not in the message, so business
logic never constructs Bot API URLs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

__all__ = [
    "PublishResult",
    "TelegramPublisher",
]


@dataclass(frozen=True, slots=True)
class PublishResult:
    """Outcome of one ``publish`` call. Never includes the message body."""

    ok: bool
    telegram_message_id: int | None
    error_safe: str | None = None
    error_code: int | None = None
    retry_after: float | None = None
    retryable: bool = False

    def __repr__(self) -> str:
        return (
            f"<PublishResult ok={self.ok} message_id={self.telegram_message_id} "
            f"code={self.error_code} retryable={self.retryable} "
            f"retry_after={self.retry_after} error={self.error_safe!r}>"
        )


class TelegramPublisher(Protocol):
    """Transport for posting one already-rendered channel message."""

    async def publish(self, message: str) -> PublishResult:
        """Post ``message`` to the configured channel.

        Implementations must not raise for a Telegram API failure: return
        ``ok=False`` instead so one proxy cannot abort the batch.
        ``CancelledError`` still propagates.
        """
        ...
