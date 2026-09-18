"""Atomic claim and stale-lease recovery for publication outbox rows.

Mirrors :mod:`modules.scheduling`: one statement, ``FOR UPDATE SKIP LOCKED``,
network I/O **outside** the transaction. A crashed worker's ``sending`` row
becomes claimable again when ``lease_until`` expires.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.logger import get_logger
from core.models import ProxyPublication, PublicationStatus, utcnow
from modules.publishing.backoff import DEFAULT_LEASE_SECONDS

__all__ = ["claim_due_publications", "recover_stale_publications"]

_logger = get_logger("modules.publishing.claim")


async def recover_stale_publications(
    session: AsyncSession,
    *,
    channel_id: str,
    now: datetime | None = None,
) -> list[int]:
    """Return ``sending`` rows whose lease has expired to ``pending``.

    Does not send anything. ``CancelledError`` is not relevant here: this is
    a short UPDATE. Rows with a still-valid lease are left alone.
    """
    moment = _aware(now)
    statement = (
        update(ProxyPublication)
        .where(
            ProxyPublication.channel_id == channel_id,
            ProxyPublication.status == PublicationStatus.SENDING,
            or_(
                ProxyPublication.lease_until.is_(None),
                ProxyPublication.lease_until < moment,
            ),
        )
        .values(status=PublicationStatus.PENDING, lease_until=None)
        .returning(ProxyPublication.id, ProxyPublication.proxy_id)
        .execution_options(populate_existing=True)
    )
    rows = (await session.execute(statement)).all()
    recovered_ids = [int(row.id) for row in rows]
    for row in rows:
        _logger.info(
            "stale_publication_recovered",
            publication_id=row.id,
            proxy_id=row.proxy_id,
        )
    return recovered_ids


async def claim_due_publications(
    session: AsyncSession,
    *,
    channel_id: str,
    proxy_ids: list[int],
    limit: int = 25,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    now: datetime | None = None,
) -> list[ProxyPublication]:
    """Lease up to ``limit`` due ``pending`` rows for this worker.

    Only rows whose ``proxy_id`` is in ``proxy_ids`` (this tick's
    ``select_top``) are claimed, so a pending outbox row for a proxy that
    is no longer publishable is not sent.
    """
    if limit <= 0:
        msg = f"limit must be positive, got {limit}"
        raise ValueError(msg)
    if lease_seconds <= 0:
        msg = f"lease_seconds must be positive, got {lease_seconds}"
        raise ValueError(msg)
    if not proxy_ids:
        return []

    moment = _aware(now)
    lease_until = moment + timedelta(seconds=lease_seconds)
    table = ProxyPublication.__table__
    candidates = (
        select(table.c.id)
        .where(
            table.c.channel_id == channel_id,
            table.c.status == PublicationStatus.PENDING,
            table.c.next_attempt_at <= moment,
            table.c.proxy_id.in_(tuple(proxy_ids)),
            or_(table.c.lease_until.is_(None), table.c.lease_until < moment),
        )
        .order_by(table.c.next_attempt_at, table.c.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .cte("claim_candidates")
    )
    statement = (
        update(ProxyPublication)
        .where(ProxyPublication.id == candidates.c.id)
        .values(
            status=PublicationStatus.SENDING,
            lease_until=lease_until,
            last_attempt_at=moment,
        )
        .returning(ProxyPublication)
        .execution_options(populate_existing=True)
    )
    result = await session.execute(statement)
    claimed = list(result.scalars().unique().all())
    claimed.sort(key=lambda row: (row.next_attempt_at, row.id))
    _logger.info(
        "publication_claimed",
        claimed=len(claimed),
        publication_ids=[row.id for row in claimed],
        proxy_ids=[row.proxy_id for row in claimed],
    )
    return claimed


def _aware(now: datetime | None) -> datetime:
    moment = now or utcnow()
    if moment.tzinfo is None:
        msg = "now must be timezone-aware; use core.models.utcnow()"
        raise ValueError(msg)
    return moment
