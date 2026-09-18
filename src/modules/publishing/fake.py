"""In-process Telegram publisher for tests. No network."""

from __future__ import annotations

from collections.abc import Sequence

from modules.publishing.protocol import PublishResult

__all__ = ["FakeTelegramPublisher"]


class FakeTelegramPublisher:
    """Records published texts and returns canned :class:`PublishResult`s."""

    def __init__(
        self,
        *,
        fail_on_index: Sequence[int] = (),
        raise_on_index: Sequence[int] = (),
        fail_with: str = "telegram_unavailable",
        results: Sequence[PublishResult | BaseException] | None = None,
    ) -> None:
        self.messages: list[str] = []
        self.fail_on_index = set(fail_on_index)
        self.raise_on_index = set(raise_on_index)
        self.fail_with = fail_with
        self._queued = list(results) if results is not None else None
        self._next_id = 1

    async def publish(self, message: str) -> PublishResult:
        index = len(self.messages)
        self.messages.append(message)
        if self._queued is not None:
            if index >= len(self._queued):
                return self._success()
            item = self._queued[index]
            if isinstance(item, BaseException):
                raise item
            return item
        if index in self.raise_on_index:
            msg = self.fail_with
            raise RuntimeError(msg)
        if index in self.fail_on_index:
            return PublishResult(
                ok=False,
                telegram_message_id=None,
                error_safe=self.fail_with,
                retryable=True,
            )
        return self._success()

    def _success(self) -> PublishResult:
        message_id = self._next_id
        self._next_id += 1
        return PublishResult(ok=True, telegram_message_id=message_id, error_safe=None)
