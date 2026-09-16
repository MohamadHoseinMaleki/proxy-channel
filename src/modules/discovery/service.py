"""Discovery service: atomic persistence and provenance tracking.

Uses PostgreSQL to guarantee:
1. No race conditions between concurrent discovery processes.
2. 10,000 sightings of the same proxy resolve to one canonical ``Proxy`` row.
3. Every sighting creates an append-only ``ProxyDiscovery`` provenance record.
4. Existing tester scheduling, observations, and scores are NEVER overwritten.
5. HTTP/source I/O never runs inside a database transaction.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import literal_column
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import Database
from core.logger import get_logger, safe_error_message
from core.models import Proxy, ProxyDiscovery, utcnow
from modules.discovery.http import SsrfSafeHttpClient
from modules.discovery.models import DiscoveredProxyCandidate
from modules.discovery.sources.base import BaseSource

__all__ = [
    "DiscoveryBatchResult",
    "DiscoveryService",
    "persist_candidate",
    "persist_candidates",
]

_logger = get_logger("modules.discovery.service")


@dataclass(frozen=True, slots=True)
class DiscoveryBatchResult:
    """Summary of a discovery persistence run."""

    total_candidates: int
    new_proxies: int
    updated_proxies: int
    discoveries_recorded: int
    sources_attempted: int = 0
    source_failures: int = 0


def _require_aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        msg = f"{name} must be timezone-aware; use core.models.utcnow()"
        raise ValueError(msg)
    return value


async def persist_candidate(
    session: AsyncSession,
    candidate: DiscoveredProxyCandidate,
    *,
    now: datetime | None = None,
) -> tuple[int, bool]:
    """Atomically upsert a single candidate and record its discovery event.

    Always uses ``INSERT … ON CONFLICT (fingerprint) DO UPDATE``. ``is_new``
    is taken from PostgreSQL ``xmax = 0`` (inserted in this command), not from
    a prior SELECT. Concurrent workers therefore cannot both report a first
    sighting.

    Conflict updates only ``last_seen_at`` and ``is_active``. Tester columns
    (``next_test_at``, ``test_lock_*``, ``last_test_*``, ``test_attempts``)
    are left alone.

    Returns:
        (proxy_id, is_new)
    """
    timestamp = _require_aware(now or utcnow(), "now")

    proxy_upsert: Any = (
        pg_insert(Proxy)
        .values(
            protocol=candidate.proxy.protocol,
            server=candidate.proxy.server,
            port=candidate.proxy.port,
            secret=candidate.proxy.secret,
            fingerprint=candidate.proxy.fingerprint,
            is_active=True,
            first_seen_at=timestamp,
            last_seen_at=timestamp,
            next_test_at=timestamp,
        )
        .on_conflict_do_update(
            index_elements=[Proxy.fingerprint],
            set_={
                "last_seen_at": timestamp,
                "is_active": True,
            },
        )
        .returning(Proxy.id, literal_column("(xmax = 0)").label("inserted"))
    )
    row = (await session.execute(proxy_upsert)).one()
    proxy_id = int(row[0])
    is_new = bool(row[1])

    discovery_insert = pg_insert(ProxyDiscovery).values(
        proxy_id=proxy_id,
        source_type=str(candidate.source_type),
        source_name=candidate.source_name,
        source_url=candidate.source_url,
        raw_reference=candidate.raw_reference,
        discovered_at=timestamp,
    )
    await session.execute(discovery_insert)

    return proxy_id, is_new


async def persist_candidates(
    session: AsyncSession,
    candidates: Sequence[DiscoveredProxyCandidate],
    *,
    now: datetime | None = None,
) -> DiscoveryBatchResult:
    """Persist a sequence of discovered candidates within an existing session scope."""
    timestamp = _require_aware(now or utcnow(), "now")
    new_count = 0
    updated_count = 0
    discoveries_count = 0

    for candidate in candidates:
        _proxy_id, is_new = await persist_candidate(session, candidate, now=timestamp)
        if is_new:
            new_count += 1
        else:
            updated_count += 1
        discoveries_count += 1

    return DiscoveryBatchResult(
        total_candidates=len(candidates),
        new_proxies=new_count,
        updated_proxies=updated_count,
        discoveries_recorded=discoveries_count,
    )


class DiscoveryService:
    """Coordinates proxy discovery fetching and persistence."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def save_candidates(
        self,
        candidates: Sequence[DiscoveredProxyCandidate],
        *,
        now: datetime | None = None,
    ) -> DiscoveryBatchResult:
        """Persist a batch of discovered candidates in a transactional session."""
        async with self.db.session_scope() as session:
            result = await persist_candidates(session, candidates, now=now)
            _logger.info(
                "discovery_candidates_persisted",
                total=result.total_candidates,
                new=result.new_proxies,
                updated=result.updated_proxies,
                discoveries=result.discoveries_recorded,
            )
            return result

    async def harvest(
        self,
        sources: Sequence[BaseSource],
        *,
        http_client: SsrfSafeHttpClient,
        now: datetime | None = None,
        concurrency: int = 1,
    ) -> DiscoveryBatchResult:
        """Fetch every source *outside* a transaction, then persist.

        One failing source is logged and skipped; it does not abort the tick.
        ``CancelledError`` / ``KeyboardInterrupt`` / ``SystemExit`` propagate.
        """
        limit = max(1, concurrency)
        semaphore = asyncio.Semaphore(limit)

        async def _fetch(source: BaseSource) -> list[DiscoveredProxyCandidate]:
            async with semaphore:
                return await source.fetch_candidates(http_client)

        gathered = await asyncio.gather(
            *(_fetch(source) for source in sources),
            return_exceptions=True,
        )

        candidates: list[DiscoveredProxyCandidate] = []
        failures = 0
        for source, item in zip(sources, gathered, strict=True):
            if isinstance(item, BaseException) and not isinstance(item, Exception):
                raise item
            if isinstance(item, Exception):
                failures += 1
                _logger.error(
                    "discovery_source_failed",
                    source_name=source.source_name,
                    source_type=str(source.source_type),
                    error=safe_error_message(item),
                )
                continue
            candidates.extend(item)

        if not candidates:
            return DiscoveryBatchResult(
                total_candidates=0,
                new_proxies=0,
                updated_proxies=0,
                discoveries_recorded=0,
                sources_attempted=len(sources),
                source_failures=failures,
            )

        persisted = await self.save_candidates(candidates, now=now)
        return DiscoveryBatchResult(
            total_candidates=persisted.total_candidates,
            new_proxies=persisted.new_proxies,
            updated_proxies=persisted.updated_proxies,
            discoveries_recorded=persisted.discoveries_recorded,
            sources_attempted=len(sources),
            source_failures=failures,
        )
