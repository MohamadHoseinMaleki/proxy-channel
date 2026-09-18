"""Read-only publication health and a throttled publisher heartbeat.

The snapshot never INSERT/UPDATE/DELETE. Heartbeat writes are a separate
function used by the publisher worker, not by health.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Select, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import ProxyPublication, PublicationStatus, PublisherHeartbeat, utcnow

__all__ = [
    "DEFAULT_HEARTBEAT_SECONDS",
    "DEFAULT_HEARTBEAT_STALE_SECONDS",
    "DEFAULT_STALE_PENDING_SECONDS",
    "DEFAULT_WORKER_NAME",
    "PublicationHealth",
    "PublicationHealthService",
    "heartbeat_is_stale",
    "snapshot_status_statement",
    "stale_sending_count_statement",
    "touch_publisher_heartbeat",
]

DEFAULT_WORKER_NAME = "publishing-worker"
DEFAULT_HEARTBEAT_SECONDS = 60.0
DEFAULT_HEARTBEAT_STALE_SECONDS = 180.0
DEFAULT_STALE_PENDING_SECONDS = 3600.0


@dataclass(frozen=True, slots=True)
class PublicationHealth:
    """Point-in-time gauges. Ages are seconds; None when there is no row."""

    channel_id: str
    pending_count: int
    sending_count: int
    published_count: int
    failed_count: int
    stale_sending_count: int
    stale_pending_count: int
    oldest_pending_age_seconds: float | None
    oldest_sending_age_seconds: float | None
    last_successful_publication_at: datetime | None
    last_failed_publication_at: datetime | None
    heartbeat_last_seen_at: datetime | None
    heartbeat_stale: bool
    as_of: datetime
    worker_name: str

    def __repr__(self) -> str:
        return (
            f"<PublicationHealth channel_id={self.channel_id!r} "
            f"pending={self.pending_count} sending={self.sending_count} "
            f"published={self.published_count} failed={self.failed_count} "
            f"heartbeat_stale={self.heartbeat_stale}>"
        )


def snapshot_status_statement(
    channel_id: str,
) -> Select[tuple[str, int, datetime, datetime | None]]:
    """Aggregate counts and extrema per status. SELECT only."""
    return (
        select(
            ProxyPublication.status,
            func.count().label("n"),
            func.min(ProxyPublication.created_at).label("oldest_created"),
            func.max(ProxyPublication.last_attempt_at).label("latest_attempt"),
        )
        .where(ProxyPublication.channel_id == channel_id)
        .group_by(ProxyPublication.status)
    )


def stale_sending_count_statement(channel_id: str, now: datetime) -> Select[tuple[int]]:
    """``sending`` rows whose lease has expired. SELECT only. Does not recover."""
    return select(func.count()).where(
        ProxyPublication.channel_id == channel_id,
        ProxyPublication.status == PublicationStatus.SENDING,
        or_(ProxyPublication.lease_until.is_(None), ProxyPublication.lease_until < now),
    )


def heartbeat_is_stale(
    last_seen_at: datetime | None,
    *,
    now: datetime,
    stale_seconds: float,
) -> bool:
    """Missing or too-old heartbeat is stale. A row existing is not health."""
    if last_seen_at is None:
        return True
    return now - last_seen_at >= timedelta(seconds=stale_seconds)


class PublicationHealthService:
    """Read-only snapshot of outbox + publisher heartbeat."""

    def __init__(
        self,
        *,
        channel_id: str,
        worker_name: str = DEFAULT_WORKER_NAME,
        stale_pending_seconds: float = DEFAULT_STALE_PENDING_SECONDS,
        heartbeat_stale_seconds: float = DEFAULT_HEARTBEAT_STALE_SECONDS,
    ) -> None:
        chat = channel_id.strip()
        if not chat:
            msg = "channel_id must not be empty"
            raise ValueError(msg)
        self.channel_id = chat
        self.worker_name = worker_name
        self.stale_pending_seconds = stale_pending_seconds
        self.heartbeat_stale_seconds = heartbeat_stale_seconds

    async def snapshot(
        self,
        session: AsyncSession,
        *,
        now: datetime | None = None,
    ) -> PublicationHealth:
        moment = _aware(now)
        counts: dict[str, int] = {
            PublicationStatus.PENDING: 0,
            PublicationStatus.SENDING: 0,
            PublicationStatus.PUBLISHED: 0,
            PublicationStatus.FAILED: 0,
        }
        oldest_created: dict[str, datetime | None] = dict.fromkeys(counts)
        latest_attempt: dict[str, datetime | None] = dict.fromkeys(counts)
        rows = (await session.execute(snapshot_status_statement(self.channel_id))).all()
        for row in rows:
            status = str(row.status)
            if status in counts:
                counts[status] = int(row.n)
                oldest_created[status] = row.oldest_created
                latest_attempt[status] = row.latest_attempt

        stale_stmt = stale_sending_count_statement(self.channel_id, moment)
        stale_sending = int((await session.execute(stale_stmt)).scalar_one() or 0)
        cutoff = moment - timedelta(seconds=self.stale_pending_seconds)
        stale_pending = int(
            (
                await session.execute(
                    select(func.count()).where(
                        ProxyPublication.channel_id == self.channel_id,
                        ProxyPublication.status == PublicationStatus.PENDING,
                        ProxyPublication.created_at <= cutoff,
                    )
                )
            ).scalar_one()
            or 0
        )

        heartbeat_at = (
            await session.execute(
                select(PublisherHeartbeat.last_seen_at).where(
                    PublisherHeartbeat.worker_name == self.worker_name
                )
            )
        ).scalar_one_or_none()

        return PublicationHealth(
            channel_id=self.channel_id,
            pending_count=counts[PublicationStatus.PENDING],
            sending_count=counts[PublicationStatus.SENDING],
            published_count=counts[PublicationStatus.PUBLISHED],
            failed_count=counts[PublicationStatus.FAILED],
            stale_sending_count=stale_sending,
            stale_pending_count=stale_pending,
            oldest_pending_age_seconds=_age(moment, oldest_created[PublicationStatus.PENDING]),
            oldest_sending_age_seconds=_age(moment, oldest_created[PublicationStatus.SENDING]),
            last_successful_publication_at=latest_attempt[PublicationStatus.PUBLISHED],
            last_failed_publication_at=latest_attempt[PublicationStatus.FAILED],
            heartbeat_last_seen_at=heartbeat_at,
            heartbeat_stale=heartbeat_is_stale(
                heartbeat_at, now=moment, stale_seconds=self.heartbeat_stale_seconds
            ),
            as_of=moment,
            worker_name=self.worker_name,
        )


async def touch_publisher_heartbeat(
    session: AsyncSession,
    *,
    worker_name: str = DEFAULT_WORKER_NAME,
    run_id: str | None = None,
    interval_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
    now: datetime | None = None,
) -> bool:
    """Write ``last_seen_at`` at most once per ``interval_seconds``.

    Returns whether a row was inserted or updated. ``interval_seconds <= 0``
    disables writes.
    """
    if interval_seconds <= 0:
        return False
    moment = _aware(now)
    cutoff = moment - timedelta(seconds=interval_seconds)
    statement = (
        insert(PublisherHeartbeat)
        .values(worker_name=worker_name, last_seen_at=moment, run_id=run_id)
        .on_conflict_do_update(
            index_elements=["worker_name"],
            set_={"last_seen_at": moment, "run_id": run_id},
            where=PublisherHeartbeat.last_seen_at <= cutoff,
        )
    )
    result = await session.execute(statement)
    return int(getattr(result, "rowcount", 0) or 0) > 0


def _age(now: datetime, then: datetime | None) -> float | None:
    if then is None:
        return None
    return max(0.0, (now - then).total_seconds())


def _aware(now: datetime | None) -> datetime:
    moment = now or utcnow()
    if moment.tzinfo is None:
        msg = "now must be timezone-aware; use core.models.utcnow()"
        raise ValueError(msg)
    return moment
