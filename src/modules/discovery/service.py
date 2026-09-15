"""Discovery service: atomic persistence and provenance tracking.

Uses PostgreSQL to guarantee:
1. No race conditions between concurrent discovery processes.
2. 10,000 sightings of the same proxy resolve to one canonical ``Proxy`` row.
3. Every sighting creates an append-only ``ProxyDiscovery`` provenance record.
4. Existing test results, error categories, and observations are NEVER overwritten.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import Database
from core.logger import get_logger
from core.models import Proxy, ProxyDiscovery, utcnow
from modules.discovery.models import DiscoveredProxyCandidate

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


async def persist_candidate(
    session: AsyncSession,
    candidate: DiscoveredProxyCandidate,
    *,
    now: datetime | None = None,
) -> tuple[int, bool]:
    """Atomically upsert a single candidate and record its discovery event.

    Returns:
        (proxy_id, is_new)
    """
    timestamp = now or utcnow()

    # Check if proxy already exists by its deterministic fingerprint
    existing_id = await session.scalar(
        select(Proxy.id).where(Proxy.fingerprint == candidate.proxy.fingerprint)
    )

    if existing_id is not None:
        proxy_id = existing_id
        is_new = False
        await session.execute(
            update(Proxy)
            .where(Proxy.id == proxy_id)
            .values(
                last_seen_at=timestamp,
                is_active=True,
            )
        )
    else:
        # Insert with on_conflict_do_update to guard against concurrent workers
        proxy_upsert = (
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
            .returning(Proxy.id)
        )
        proxy_id = (await session.execute(proxy_upsert)).scalar_one()
        is_new = True

    # Append-only provenance recording into `proxy_discoveries`
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
    timestamp = now or utcnow()
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
