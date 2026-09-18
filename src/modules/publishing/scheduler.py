"""Convert a ``select_top`` list into cadence-limited outbox inserts.

Does **not** rank, score, or invent proxies. The caller passes already
validated Task 012 items; this module takes them in that order.

State lives in ``publication_schedules`` (one row per channel) so a restart
or a second process cannot open the same cadence slot. Claim/retry (D-048)
is unchanged: this only gates *new* ``pending`` rows.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Final, NamedTuple

from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.logger import get_logger
from core.models import (
    ProxyPublication,
    PublicationSchedule,
    PublicationStatus,
    utcnow,
)
from modules.reporting.models import ReportItem

__all__ = [
    "DEFAULT_DEDUP_SECONDS",
    "DEFAULT_INTERVAL_SECONDS",
    "DEFAULT_MAX_PENDING",
    "MAX_NEW_PER_SLOT",
    "PublicationScheduler",
    "choose_schedule_candidates",
    "lock_schedule_statement",
    "schedule_new_publications",
]

_logger = get_logger("modules.publishing.scheduler")

#: One new outbox row per open cadence slot. Existing pending/sending rows
#: are still claimed and sent by the outbox worker (D-048).
MAX_NEW_PER_SLOT: Final = 1
DEFAULT_INTERVAL_SECONDS: Final = 300.0
DEFAULT_DEDUP_SECONDS: Final = 86400.0
DEFAULT_MAX_PENDING: Final = 20


class ExistingPublication(NamedTuple):
    """Outbox facts the scheduler needs. No message body, no secret."""

    proxy_id: int
    status: str
    last_attempt_at: datetime | None
    created_at: datetime | None


def lock_schedule_statement(channel_id: str) -> Select[tuple[PublicationSchedule]]:
    """``SELECT … FOR UPDATE SKIP LOCKED`` for one channel's cadence row."""
    return (
        select(PublicationSchedule)
        .where(PublicationSchedule.channel_id == channel_id)
        .with_for_update(skip_locked=True)
    )


def choose_schedule_candidates(
    items: Sequence[ReportItem],
    existing: Sequence[ExistingPublication],
    *,
    now: datetime,
    dedup_seconds: float,
    limit: int,
) -> list[ReportItem]:
    """First ``limit`` items, in caller order, that are not already outboxed.

    Unique ``(proxy_id, channel_id)`` is the historical identity: any existing
    row blocks a *new* insert so 014 retry owns failures. The dedup window
    additionally blocks a recently published proxy (conservative default:
    successfully published proxies are never rotated back in).
    """
    if limit <= 0:
        return []
    blocked: set[int] = set()
    window = timedelta(seconds=dedup_seconds)
    for row in existing:
        blocked.add(row.proxy_id)
        if row.status != PublicationStatus.PUBLISHED:
            continue
        published_at = row.last_attempt_at or row.created_at
        if published_at is None:
            continue
        if published_at + window > now:
            blocked.add(row.proxy_id)
    chosen: list[ReportItem] = []
    seen: set[int] = set()
    for item in items:
        if item.proxy_id in seen or item.proxy_id in blocked:
            continue
        seen.add(item.proxy_id)
        chosen.append(item)
        if len(chosen) >= limit:
            break
    return chosen


class PublicationScheduler:
    """DB-backed cadence + dedup in front of the outbox insert."""

    def __init__(
        self,
        *,
        channel_id: str,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        dedup_seconds: float = DEFAULT_DEDUP_SECONDS,
        max_pending: int = DEFAULT_MAX_PENDING,
    ) -> None:
        chat = channel_id.strip()
        if not chat:
            msg = "channel_id must not be empty"
            raise ValueError(msg)
        if interval_seconds <= 0:
            msg = f"interval_seconds must be positive, got {interval_seconds}"
            raise ValueError(msg)
        if dedup_seconds < 0:
            msg = f"dedup_seconds must be >= 0, got {dedup_seconds}"
            raise ValueError(msg)
        if max_pending < 1:
            msg = f"max_pending must be >= 1, got {max_pending}"
            raise ValueError(msg)
        self.channel_id = chat
        self.interval_seconds = interval_seconds
        self.dedup_seconds = dedup_seconds
        self.max_pending = max_pending

    async def enqueue(
        self,
        session: AsyncSession,
        items: Sequence[ReportItem],
        *,
        now: datetime | None = None,
    ) -> list[ReportItem]:
        return await schedule_new_publications(
            session,
            items,
            channel_id=self.channel_id,
            interval_seconds=self.interval_seconds,
            dedup_seconds=self.dedup_seconds,
            max_pending=self.max_pending,
            now=now,
        )


async def schedule_new_publications(
    session: AsyncSession,
    items: Sequence[ReportItem],
    *,
    channel_id: str,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    dedup_seconds: float = DEFAULT_DEDUP_SECONDS,
    max_pending: int = DEFAULT_MAX_PENDING,
    now: datetime | None = None,
) -> list[ReportItem]:
    """Insert at most one new pending row if the cadence slot is free.

    Returns the items actually inserted (subset of ``items``, same order).
    Does not claim or send. Empty ``items`` is a no-op and does not consume
    the slot.
    """
    moment = _aware(now)
    if not items:
        return []

    locked = await _lock_or_create_schedule(session, channel_id)
    if locked is None:
        _logger.info("publication_schedule_slot_busy", channel_id=channel_id)
        return []

    if locked.last_scheduled_at is not None:
        earliest = locked.last_scheduled_at + timedelta(seconds=interval_seconds)
        if moment < earliest:
            _logger.info(
                "publication_schedule_cadence_hold",
                channel_id=channel_id,
            )
            return []

    pending = await _pending_count(session, channel_id)
    capacity = min(MAX_NEW_PER_SLOT, max_pending - pending)
    if capacity <= 0:
        _logger.info(
            "publication_schedule_backpressure",
            channel_id=channel_id,
            pending=pending,
            max_pending=max_pending,
        )
        return []

    existing = await _existing_rows(session, channel_id)
    chosen = choose_schedule_candidates(
        items,
        existing,
        now=moment,
        dedup_seconds=dedup_seconds,
        limit=capacity,
    )
    if not chosen:
        return []

    values = [
        {
            "proxy_id": item.proxy_id,
            "channel_id": channel_id,
            "status": PublicationStatus.PENDING,
            "next_attempt_at": moment,
            "attempt_count": 0,
        }
        for item in chosen
    ]
    await session.execute(
        insert(ProxyPublication)
        .values(values)
        .on_conflict_do_nothing(constraint="uq_proxy_publications_proxy_channel")
    )
    locked.last_scheduled_at = moment
    _logger.info(
        "publication_scheduled",
        channel_id=channel_id,
        scheduled=len(chosen),
        proxy_ids=[item.proxy_id for item in chosen],
    )
    return chosen


async def _lock_or_create_schedule(
    session: AsyncSession, channel_id: str
) -> PublicationSchedule | None:
    locked = (await session.execute(lock_schedule_statement(channel_id))).scalar_one_or_none()
    if locked is not None:
        return locked
    visible = (
        await session.execute(
            select(PublicationSchedule).where(PublicationSchedule.channel_id == channel_id)
        )
    ).scalar_one_or_none()
    if visible is not None:
        # Row exists but another worker holds the lock.
        return None
    await session.execute(
        insert(PublicationSchedule)
        .values(channel_id=channel_id, last_scheduled_at=None)
        .on_conflict_do_nothing(index_elements=["channel_id"])
    )
    return (await session.execute(lock_schedule_statement(channel_id))).scalar_one_or_none()


async def _pending_count(session: AsyncSession, channel_id: str) -> int:
    statement = select(func.count()).where(
        ProxyPublication.channel_id == channel_id,
        ProxyPublication.status.in_((PublicationStatus.PENDING, PublicationStatus.SENDING)),
    )
    value = (await session.execute(statement)).scalar_one()
    return int(value or 0)


async def _existing_rows(session: AsyncSession, channel_id: str) -> list[ExistingPublication]:
    statement = select(
        ProxyPublication.proxy_id,
        ProxyPublication.status,
        ProxyPublication.last_attempt_at,
        ProxyPublication.created_at,
    ).where(ProxyPublication.channel_id == channel_id)
    rows = (await session.execute(statement)).all()
    return [
        ExistingPublication(
            proxy_id=int(row.proxy_id),
            status=str(row.status),
            last_attempt_at=row.last_attempt_at,
            created_at=row.created_at,
        )
        for row in rows
    ]


def _aware(now: datetime | None) -> datetime:
    moment = now or utcnow()
    if moment.tzinfo is None:
        msg = "now must be timezone-aware; use core.models.utcnow()"
        raise ValueError(msg)
    return moment
