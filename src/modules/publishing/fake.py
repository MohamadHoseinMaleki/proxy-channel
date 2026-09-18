"""In-process Telegram publisher for tests. No network."""

from __future__ import annotations

from collections.abc import Sequence

from modules.publishing.protocol import PublishResult

__all__ = ["FakeTelegramPublisher"]


class FakeTelegramPublisher:
    """Records published texts and returns synthetic Telegram message ids."""

    def __init__(
        self,
        *,
        fail_on_index: Sequence[int] = (),
        raise_on_index: Sequence[int] = (),
        fail_with: str = "telegram_unavailable",
    ) -> None:
        self.messages: list[str] = []
        self.fail_on_index = set(fail_on_index)
        self.raise_on_index = set(raise_on_index)
        self.fail_with = fail_with
        self._next_id = 1

    async def publish(self, message: str) -> PublishResult:
        index = len(self.messages)
        self.messages.append(message)
        if index in self.raise_on_index:
            msg = self.fail_with
            raise RuntimeError(msg)
        if index in self.fail_on_index:
            return PublishResult(ok=False, telegram_message_id=None, error_safe=self.fail_with)
        message_id = self._next_id
        self._next_id += 1
        return PublishResult(ok=True, telegram_message_id=message_id, error_safe=None)
