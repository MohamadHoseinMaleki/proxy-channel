"""Read-only publication health and a throttled publisher heartbeat.

The snapshot never INSERT/UPDATE/DELETE. Heartbeat writes and counter
upserts are separate functions; health only SELECTs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import Select, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import ProxyPublication, PublicationStatus, PublisherHeartbeat, utcnow
from modules.publishing.metrics import load_counters

__all__ = [
    "DEFAULT_HEARTBEAT_SECONDS",
    "DEFAULT_HEARTBEAT_STALE_SECONDS",
    "DEFAULT_STALE_PENDING_SECONDS",
    "DEFAULT_WORKER_NAME",
    "DEFAULT_WORKER_TYPE",
    "HeartbeatState",
    "PublicationHealth",
    "PublicationHealthService",
    "WorkerHeartbeatStatus",
    "heartbeat_is_stale",
    "heartbeat_state",
    "list_heartbeats_statement",
    "snapshot_status_statement",
    "stale_sending_count_statement",
    "touch_publisher_heartbeat",
]

DEFAULT_WORKER_NAME = "publishing-worker"
DEFAULT_WORKER_TYPE = DEFAULT_WORKER_NAME
DEFAULT_HEARTBEAT_SECONDS = 60.0
DEFAULT_HEARTBEAT_STALE_SECONDS = 180.0
DEFAULT_STALE_PENDING_SECONDS = 3600.0

HeartbeatState = Literal["none", "stale", "healthy"]


@dataclass(frozen=True, slots=True)
class WorkerHeartbeatStatus:
    """One publisher process. ``status`` is healthy or stale, never none."""

    worker_id: str
    worker_type: str
    last_seen_at: datetime
    status: Literal["healthy", "stale"]

    def __repr__(self) -> str:
        return (
            f"<WorkerHeartbeatStatus worker_id={self.worker_id!r} "
            f"status={self.status} last_seen_at={self.last_seen_at}>"
        )


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
    heartbeat_state: HeartbeatState
    heartbeats: tuple[WorkerHeartbeatStatus, ...]
    healthy_publisher_count: int
    stale_publisher_count: int
    counters: dict[str, int]
    as_of: datetime
    worker_name: str

    def __repr__(self) -> str:
        return (
            f"<PublicationHealth channel_id={self.channel_id!r} "
            f"pending={self.pending_count} sending={self.sending_count} "
            f"published={self.published_count} failed={self.failed_count} "
            f"heartbeat_state={self.heartbeat_state}>"
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


def list_heartbeats_statement(worker_type: str) -> Select[tuple[PublisherHeartbeat]]:
    """All heartbeats of one worker type. SELECT only."""
    return select(PublisherHeartbeat).where(PublisherHeartbeat.worker_type == worker_type)


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


def heartbeat_state(
    views: tuple[WorkerHeartbeatStatus, ...],
) -> HeartbeatState:
    """System-level state. One existing row is not automatically healthy."""
    if not views:
        return "none"
    if any(item.status == "healthy" for item in views):
        return "healthy"
    return "stale"


class PublicationHealthService:
    """Read-only snapshot of outbox + publisher heartbeats + counters."""

    def __init__(
        self,
        *,
        channel_id: str,
        worker_name: str = DEFAULT_WORKER_TYPE,
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

        hb_rows = (
            (await session.execute(list_heartbeats_statement(self.worker_name))).scalars().all()
        )
        views: list[WorkerHeartbeatStatus] = []
        for beat in hb_rows:
            stale = heartbeat_is_stale(
                beat.last_seen_at, now=moment, stale_seconds=self.heartbeat_stale_seconds
            )
            views.append(
                WorkerHeartbeatStatus(
                    worker_id=beat.worker_id,
                    worker_type=beat.worker_type,
                    last_seen_at=beat.last_seen_at,
                    status="stale" if stale else "healthy",
                )
            )
        views_tuple = tuple(sorted(views, key=lambda item: item.worker_id))
        state = heartbeat_state(views_tuple)
        latest = max((item.last_seen_at for item in views_tuple), default=None)
        healthy = sum(1 for item in views_tuple if item.status == "healthy")
        stale_n = sum(1 for item in views_tuple if item.status == "stale")
        counters = await load_counters(session, channel_id="")

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
            heartbeat_last_seen_at=latest,
            heartbeat_stale=state != "healthy",
            heartbeat_state=state,
            heartbeats=views_tuple,
            healthy_publisher_count=healthy,
            stale_publisher_count=stale_n,
            counters=counters,
            as_of=moment,
            worker_name=self.worker_name,
        )


async def touch_publisher_heartbeat(
    session: AsyncSession,
    *,
    worker_id: str,
    worker_type: str = DEFAULT_WORKER_TYPE,
    interval_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
    now: datetime | None = None,
) -> bool:
    """Write ``last_seen_at`` at most once per ``interval_seconds`` per process.

    ``worker_id`` is the process identity. ``interval_seconds <= 0`` disables.
    """
    if interval_seconds <= 0:
        return False
    identity = worker_id.strip()
    wtype = worker_type.strip()
    if not identity:
        msg = "worker_id must not be empty"
        raise ValueError(msg)
    if not wtype:
        msg = "worker_type must not be empty"
        raise ValueError(msg)
    moment = _aware(now)
    cutoff = moment - timedelta(seconds=interval_seconds)
    statement = (
        insert(PublisherHeartbeat)
        .values(worker_id=identity, worker_type=wtype, last_seen_at=moment)
        .on_conflict_do_update(
            index_elements=["worker_id"],
            set_={"last_seen_at": moment, "worker_type": wtype},
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
