"""Publish :class:`~modules.reporting.models.Report` items via an outbox.

Consumes Task 012 selection. Does not rescore, re-rank, or rewrite eligibility.
Validated items pass through :class:`PublicationScheduler` before enqueue.
Telegram I/O happens **outside** a database transaction after rows are claimed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from core.config import Settings
from core.database import Database
from core.logger import get_logger, safe_error_message
from core.models import ProxyPublication, PublicationStatus, utcnow
from modules.publishing.backoff import (
    DEFAULT_LEASE_SECONDS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRY_BASE_SECONDS,
    DEFAULT_RETRY_MAX_SECONDS,
    retry_delay_seconds,
)
from modules.publishing.claim import claim_due_publications, recover_stale_publications
from modules.publishing.formatter import format_channel_message
from modules.publishing.metrics import PublicationMetrics
from modules.publishing.observe import (
    EVENT_FAILED,
    EVENT_PUBLISHED,
    EVENT_RECOVERED,
    EVENT_REJECTED,
    EVENT_RETRY,
    EVENT_TELEGRAM_RATE_LIMITED,
    PublicationErrorClass,
    classify_publish_result,
)
from modules.publishing.protocol import PublishResult, TelegramPublisher
from modules.publishing.scheduler import (
    DEFAULT_DEDUP_SECONDS,
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_MAX_PENDING,
    PublicationScheduler,
)
from modules.publishing.validation import validate_publication
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
    recovered: int
    retried: int
    channel_id: str

    def __repr__(self) -> str:
        return (
            f"<PublishCycleResult selected={self.selected} published={self.published} "
            f"skipped={self.skipped} failed={self.failed} recovered={self.recovered} "
            f"retried={self.retried}>"
        )


class PublishingService:
    """Enqueue ``select_top`` into the outbox, claim, post, persist."""

    def __init__(
        self,
        db: Database,
        *,
        publisher: TelegramPublisher,
        channel_id: str,
        reporting: ReportingService | None = None,
        settings: Settings | None = None,
        metrics: PublicationMetrics | None = None,
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
        self.lease_seconds = (
            settings.telegram_publication_lease_seconds if settings else DEFAULT_LEASE_SECONDS
        )
        self.max_retries = settings.telegram_max_retries if settings else DEFAULT_MAX_RETRIES
        self.retry_base_seconds = (
            settings.telegram_retry_base_seconds if settings else DEFAULT_RETRY_BASE_SECONDS
        )
        self.retry_max_seconds = (
            settings.telegram_retry_max_seconds if settings else DEFAULT_RETRY_MAX_SECONDS
        )
        self.scheduler = PublicationScheduler(
            channel_id=chat,
            interval_seconds=(
                settings.telegram_publication_interval_seconds
                if settings
                else DEFAULT_INTERVAL_SECONDS
            ),
            dedup_seconds=(
                settings.telegram_publication_dedup_seconds if settings else DEFAULT_DEDUP_SECONDS
            ),
            max_pending=(
                settings.telegram_publication_max_pending if settings else DEFAULT_MAX_PENDING
            ),
        )
        self.metrics = metrics if metrics is not None else PublicationMetrics()

    async def publish_cycle(
        self,
        *,
        limit: int | None = None,
        as_of: datetime | None = None,
        now: datetime | None = None,
    ) -> PublishCycleResult:
        """Load Task 012's selection and drive the outbox one tick."""
        report = await self.reporting.select_top(limit=limit, as_of=as_of)
        return await self.publish_report(report, now=now)

    async def publish_report(
        self,
        report: Report,
        *,
        now: datetime | None = None,
    ) -> PublishCycleResult:
        moment = now or utcnow()
        if moment.tzinfo is None:
            msg = "now must be timezone-aware; use core.models.utcnow()"
            raise ValueError(msg)

        items_by_id = {item.proxy_id: item for item in report.items}
        eligible: list[ReportItem] = []
        skipped = 0
        for item in report.items:
            check = validate_publication(item)
            if check.ok:
                eligible.append(item)
                continue
            skipped += 1
            self.metrics.inc_rejected()
            _logger.warning(
                EVENT_REJECTED,
                proxy_id=item.proxy_id,
                channel_id=self.channel_id,
                reason=check.reason,
                classification=PublicationErrorClass.VALIDATION,
            )

        async with self.db.session_scope() as session:
            scheduled = await self.scheduler.enqueue(session, eligible, now=moment)
        if scheduled:
            self.metrics.inc_scheduled(len(scheduled))
        recovered = await self._recover(now=moment)
        for publication_id in recovered:
            _logger.info(
                EVENT_RECOVERED,
                publication_id=publication_id,
                channel_id=self.channel_id,
            )

        claimed: list[ProxyPublication] = []
        if eligible:
            async with self.db.session_scope() as session:
                claimed = await claim_due_publications(
                    session,
                    channel_id=self.channel_id,
                    proxy_ids=[item.proxy_id for item in eligible],
                    limit=max(len(eligible), 1),
                    lease_seconds=self.lease_seconds,
                    now=moment,
                )

        published = 0
        failed = 0
        retried = 0
        for row in claimed:
            claimed_item = items_by_id.get(row.proxy_id)
            if claimed_item is None or not validate_publication(claimed_item).ok:
                await self._release_unsendable(row, now=moment)
                skipped += 1
                continue
            outcome = await self._send_claimed(row, claimed_item, now=moment)
            if outcome is PublicationStatus.PUBLISHED:
                published += 1
            elif outcome is PublicationStatus.FAILED:
                failed += 1
            else:
                retried += 1

        skipped += max(len(eligible) - len(claimed), 0)

        _logger.info(
            "publication_cycle_completed",
            selected=len(report.items),
            published=published,
            skipped=skipped,
            failed=failed,
            recovered=len(recovered),
            retried=retried,
        )
        return PublishCycleResult(
            selected=len(report.items),
            published=published,
            skipped=skipped,
            failed=failed,
            recovered=len(recovered),
            retried=retried,
            channel_id=self.channel_id,
        )

    async def _enqueue(self, items: list[ReportItem], *, now: datetime) -> None:
        if not items:
            return
        values = [
            {
                "proxy_id": item.proxy_id,
                "channel_id": self.channel_id,
                "status": PublicationStatus.PENDING,
                "next_attempt_at": now,
                "attempt_count": 0,
            }
            for item in items
        ]
        statement = (
            insert(ProxyPublication)
            .values(values)
            .on_conflict_do_nothing(constraint="uq_proxy_publications_proxy_channel")
        )
        async with self.db.session_scope() as session:
            await session.execute(statement)

    async def _recover(self, *, now: datetime) -> list[int]:
        async with self.db.session_scope() as session:
            return await recover_stale_publications(session, channel_id=self.channel_id, now=now)

    async def _send_claimed(
        self,
        row: ProxyPublication,
        item: ReportItem,
        *,
        now: datetime,
    ) -> PublicationStatus:
        text = format_channel_message(item)
        try:
            result = await self.publisher.publish(text)
        except Exception as exc:
            # CancelledError is BaseException and still propagates.
            result = PublishResult(
                ok=False,
                telegram_message_id=None,
                error_safe=safe_error_message(exc) or type(exc).__name__,
                retryable=True,
            )
        if result.ok and result.telegram_message_id is not None:
            stored = await self._mark_published(row, result.telegram_message_id)
            if stored:
                self.metrics.inc_success()
                _logger.info(
                    EVENT_PUBLISHED,
                    proxy_id=item.proxy_id,
                    publication_id=row.id,
                    channel_id=self.channel_id,
                    attempt=row.attempt_count + 1,
                    telegram_message_id=result.telegram_message_id,
                )
                return PublicationStatus.PUBLISHED
            self.metrics.inc_failures()
            _logger.warning(
                EVENT_FAILED,
                proxy_id=item.proxy_id,
                publication_id=row.id,
                channel_id=self.channel_id,
                attempt=row.attempt_count + 1,
                reason="unique_conflict",
                classification=PublicationErrorClass.DATABASE,
            )
            return PublicationStatus.FAILED

        error = result.error_safe or "telegram_publish_failed"
        classification = classify_publish_result(result)
        if result.error_code == 429:
            self.metrics.inc_rate_limits()
            _logger.warning(
                EVENT_TELEGRAM_RATE_LIMITED,
                proxy_id=item.proxy_id,
                publication_id=row.id,
                channel_id=self.channel_id,
                attempt=row.attempt_count + 1,
                retry_after=result.retry_after,
                classification=classification,
            )
        attempts_after = row.attempt_count + 1
        retryable = result.retryable and attempts_after < self.max_retries
        if retryable:
            delay = retry_delay_seconds(
                attempt=attempts_after,
                base_seconds=self.retry_base_seconds,
                max_seconds=self.retry_max_seconds,
                retry_after=result.retry_after,
            )
            await self._schedule_retry(row, error=error, delay_seconds=delay, now=now)
            self.metrics.inc_retries()
            _logger.info(
                EVENT_RETRY,
                proxy_id=item.proxy_id,
                publication_id=row.id,
                channel_id=self.channel_id,
                attempt=attempts_after,
                delay_seconds=delay,
                reason=error,
                classification=classification,
            )
            return PublicationStatus.PENDING

        await self._mark_failed(row, error=error)
        self.metrics.inc_failures()
        _logger.warning(
            EVENT_FAILED,
            proxy_id=item.proxy_id,
            publication_id=row.id,
            channel_id=self.channel_id,
            attempt=attempts_after,
            reason=error,
            classification=classification,
        )
        return PublicationStatus.FAILED

    async def _mark_published(self, row: ProxyPublication, message_id: int) -> bool:
        statement = (
            update(ProxyPublication)
            .where(
                ProxyPublication.id == row.id,
                ProxyPublication.status == PublicationStatus.SENDING,
            )
            .values(
                status=PublicationStatus.PUBLISHED,
                telegram_message_id=message_id,
                error_message_safe=None,
                lease_until=None,
                attempt_count=ProxyPublication.attempt_count + 1,
            )
        )
        try:
            async with self.db.session_scope() as session:
                result = await session.execute(statement)
                return int(getattr(result, "rowcount", 0) or 0) == 1
        except IntegrityError:
            _logger.info(
                "publication_unique_conflict",
                proxy_id=row.proxy_id,
                publication_id=row.id,
                channel_id=self.channel_id,
                classification=PublicationErrorClass.DATABASE,
            )
            return False

    async def _schedule_retry(
        self,
        row: ProxyPublication,
        *,
        error: str,
        delay_seconds: float,
        now: datetime,
    ) -> None:
        nxt = now + timedelta(seconds=delay_seconds)
        statement = (
            update(ProxyPublication)
            .where(
                ProxyPublication.id == row.id,
                ProxyPublication.status == PublicationStatus.SENDING,
            )
            .values(
                status=PublicationStatus.PENDING,
                lease_until=None,
                error_message_safe=error,
                next_attempt_at=nxt,
                attempt_count=ProxyPublication.attempt_count + 1,
            )
        )
        async with self.db.session_scope() as session:
            await session.execute(statement)

    async def _mark_failed(self, row: ProxyPublication, *, error: str) -> None:
        statement = (
            update(ProxyPublication)
            .where(
                ProxyPublication.id == row.id,
                ProxyPublication.status == PublicationStatus.SENDING,
            )
            .values(
                status=PublicationStatus.FAILED,
                lease_until=None,
                telegram_message_id=None,
                error_message_safe=error,
                attempt_count=ProxyPublication.attempt_count + 1,
            )
        )
        async with self.db.session_scope() as session:
            await session.execute(statement)

    async def _release_unsendable(self, row: ProxyPublication, *, now: datetime) -> None:
        statement = (
            update(ProxyPublication)
            .where(
                ProxyPublication.id == row.id,
                ProxyPublication.status == PublicationStatus.SENDING,
            )
            .values(
                status=PublicationStatus.PENDING,
                lease_until=None,
                next_attempt_at=now + timedelta(seconds=self.retry_base_seconds),
            )
        )
        async with self.db.session_scope() as session:
            await session.execute(statement)
