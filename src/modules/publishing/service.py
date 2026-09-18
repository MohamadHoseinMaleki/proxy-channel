"""Publish :class:`~modules.reporting.models.Report` items to Telegram.

Consumes Task 012 selection. Does not rescore, re-rank, or rewrite eligibility.
Telegram I/O happens **outside** a database transaction. One failed post is
recorded and the rest of the batch continues.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.config import Settings
from core.database import Database
from core.logger import get_logger, safe_error_message
from core.models import ProxyPublication, PublicationStatus
from modules.discovery.models import SecretType
from modules.publishing.message import format_channel_message
from modules.publishing.protocol import PublishResult, TelegramPublisher
from modules.reporting.models import Report, ReportItem
from modules.reporting.service import ReportingService

__all__ = ["PublishCycleResult", "PublishingService"]

_logger = get_logger("modules.publishing.service")


@dataclass(frozen=True, slots=True)
class PublishCycleResult:
    """Counters for one publisher tick. No secrets, no message bodies."""

    selected: int
    published: int
    skipped: int
    failed: int
    channel_id: str

    def __repr__(self) -> str:
        return (
            f"<PublishCycleResult selected={self.selected} published={self.published} "
            f"skipped={self.skipped} failed={self.failed}>"
        )


class PublishingService:
    """Select publishable proxies, post them, persist the audit trail."""

    def __init__(
        self,
        db: Database,
        *,
        publisher: TelegramPublisher,
        channel_id: str,
        reporting: ReportingService | None = None,
        settings: Settings | None = None,
    ) -> None:
        chat = channel_id.strip()
        if not chat:
            msg = "telegram_channel_id must not be empty"
            raise ValueError(msg)
        self.db = db
        self.publisher = publisher
        self.channel_id = chat
        self.reporting = (
            reporting if reporting is not None else ReportingService(db, settings=settings)
        )

    async def publish_cycle(
        self,
        *,
        limit: int | None = None,
        as_of: datetime | None = None,
    ) -> PublishCycleResult:
        """Load Task 012's selection and publish each item at most once."""
        report = await self.reporting.select_top(limit=limit, as_of=as_of)
        return await self.publish_report(report)

    async def publish_report(self, report: Report) -> PublishCycleResult:
        already = await self._successful_proxy_ids(tuple(item.proxy_id for item in report.items))
        published = 0
        skipped = 0
        failed = 0
        for item in report.items:
            if item.proxy_id in already:
                skipped += 1
                _logger.info("publication_skipped_duplicate", proxy_id=item.proxy_id)
                continue
            if not _is_safe_to_post(item):
                skipped += 1
                _logger.warning("publication_skipped_unpublishable", proxy_id=item.proxy_id)
                continue
            outcome = await self._publish_one(item)
            if outcome is PublicationStatus.SUCCESS:
                published += 1
                already.add(item.proxy_id)
            else:
                failed += 1
        _logger.info(
            "publication_cycle_completed",
            selected=len(report.items),
            published=published,
            skipped=skipped,
            failed=failed,
        )
        return PublishCycleResult(
            selected=len(report.items),
            published=published,
            skipped=skipped,
            failed=failed,
            channel_id=self.channel_id,
        )

    async def _publish_one(self, item: ReportItem) -> PublicationStatus:
        text = format_channel_message(item)
        try:
            result = await self.publisher.publish(text)
        except Exception as exc:
            # CancelledError is BaseException, not Exception, and still
            # propagates so a shutdown cannot be recorded as a failed post.
            result = PublishResult(
                ok=False,
                telegram_message_id=None,
                error_safe=safe_error_message(exc) or type(exc).__name__,
            )
        if result.ok and result.telegram_message_id is not None:
            stored = await self._record(
                proxy_id=item.proxy_id,
                status=PublicationStatus.SUCCESS,
                telegram_message_id=result.telegram_message_id,
                error_message_safe=None,
            )
            if stored:
                _logger.info(
                    "publication_succeeded",
                    proxy_id=item.proxy_id,
                    telegram_message_id=result.telegram_message_id,
                )
                return PublicationStatus.SUCCESS
            _logger.info("publication_skipped_duplicate", proxy_id=item.proxy_id)
            return PublicationStatus.FAILURE

        error = result.error_safe or "telegram_publish_failed"
        await self._record(
            proxy_id=item.proxy_id,
            status=PublicationStatus.FAILURE,
            telegram_message_id=None,
            error_message_safe=error,
        )
        _logger.warning("publication_failed", proxy_id=item.proxy_id, error=error)
        return PublicationStatus.FAILURE

    async def _successful_proxy_ids(self, proxy_ids: Sequence[int]) -> set[int]:
        if not proxy_ids:
            return set()
        statement = select(ProxyPublication.proxy_id).where(
            ProxyPublication.channel_id == self.channel_id,
            ProxyPublication.status == PublicationStatus.SUCCESS,
            ProxyPublication.proxy_id.in_(tuple(proxy_ids)),
        )
        async with self.db.session_scope() as session:
            rows = (await session.execute(statement)).scalars().all()
        return set(rows)

    async def _record(
        self,
        *,
        proxy_id: int,
        status: PublicationStatus,
        telegram_message_id: int | None,
        error_message_safe: str | None,
    ) -> bool:
        row = ProxyPublication(
            proxy_id=proxy_id,
            channel_id=self.channel_id,
            status=str(status),
            telegram_message_id=telegram_message_id,
            error_message_safe=error_message_safe,
        )
        try:
            async with self.db.session_scope() as session:
                session.add(row)
            return True
        except IntegrityError:
            _logger.info("publication_unique_conflict", proxy_id=proxy_id)
            return False


def _is_safe_to_post(item: ReportItem) -> bool:
    """Defence in depth: never post Fake-TLS even if a caller forged a Report."""
    return item.secret_type in {
        SecretType.LEGACY.value,
        SecretType.SECURE_RANDOMIZED.value,
    }
