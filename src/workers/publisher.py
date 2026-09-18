"""``publishing-worker`` -- Telegram channel publisher.

Responsibility: take :meth:`ReportingService.select_top` and post each
currently publishable proxy to a Telegram channel, recording every attempt.
Does not discover, probe, or rescore.

Idle when ``TELEGRAM_BOT_TOKEN`` or ``TELEGRAM_CHANNEL_ID`` is unset.

Run with::

    uv run mtproto-publisher
    # or
    uv run python -m workers.publisher
"""

from __future__ import annotations

import time

from core.database import Database
from core.lifecycle import WorkerLifecycle, worker_main
from modules.publishing.bot_api import BotApiTelegramPublisher, TelegramPublishError
from modules.publishing.service import PublishingService
from modules.reporting.service import ReportingService

__all__ = ["WORKER_NAME", "main", "tick"]

WORKER_NAME = "publishing-worker"


def _configured(life: WorkerLifecycle) -> bool:
    token = life.settings.telegram_bot_token
    channel = life.settings.telegram_channel_id
    return token is not None and bool(channel)


async def tick(life: WorkerLifecycle, *, db: Database | None = None) -> None:
    """One publishing iteration: select, post, persist."""
    started = time.monotonic()
    if not _configured(life):
        life.logger.info(
            "publisher_tick",
            implemented=True,
            skipped="unconfigured",
            published=0,
            failed=0,
            duration_ms=round((time.monotonic() - started) * 1000, 3),
        )
        return

    dispose_db = False
    publisher: BotApiTelegramPublisher | None = None
    if db is None:
        db = Database.from_settings(life.settings)
        dispose_db = True

    try:
        if not await db.is_reachable():
            life.logger.warning(
                "publisher_tick_db_unreachable",
                worker=WORKER_NAME,
                duration_ms=round((time.monotonic() - started) * 1000, 3),
            )
            return

        token = life.settings.telegram_bot_token
        destination = life.settings.telegram_channel_id
        if token is None or destination is None:
            return
        try:
            publisher = BotApiTelegramPublisher(
                token=token,
                channel_id=destination,
                timeout_seconds=life.settings.publisher_timeout_seconds,
                connect_timeout_seconds=life.settings.publisher_connect_timeout_seconds,
            )
        except TelegramPublishError as exc:
            life.logger.warning(
                "publisher_tick",
                implemented=True,
                skipped="invalid_credentials",
                error=type(exc).__name__,
                duration_ms=round((time.monotonic() - started) * 1000, 3),
            )
            return

        service = PublishingService(
            db,
            publisher=publisher,
            channel_id=destination,
            reporting=ReportingService(db, settings=life.settings),
            settings=life.settings,
        )
        result = await service.publish_cycle()
        life.logger.info(
            "publisher_tick",
            implemented=True,
            selected=result.selected,
            published=result.published,
            skipped=result.skipped,
            failed=result.failed,
            duration_ms=round((time.monotonic() - started) * 1000, 3),
        )
    finally:
        if publisher is not None:
            await publisher.aclose()
        if dispose_db:
            await db.dispose()


def main() -> None:
    """Console-script entrypoint for the publishing worker process."""
    worker_main(WORKER_NAME, tick)


if __name__ == "__main__":
    main()
