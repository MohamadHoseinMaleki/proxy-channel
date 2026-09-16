"""Integration tests for :mod:`modules.discovery.service` against real PostgreSQL.

Exercises:
* First-time proxy discovery (insertion into proxies + proxy_discoveries).
* Repeated sighting from same and different sources (updates last_seen_at, records provenance).
* Preservation of initial first_seen_at across repeated sightings.
* Atomic deduplication on unique fingerprint without race conditions.
* Batch persistence and DiscoveryService transactions.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from core.database import Database
from core.identity import ProxySecret
from core.models import Proxy, ProxyDiscovery, SourceType
from modules.discovery.models import (
    DiscoveredProxyCandidate,
    MTProtoProxy,
    SecretType,
)
from modules.discovery.service import (
    DiscoveryService,
    persist_candidate,
)

pytestmark = pytest.mark.integration


def _make_candidate(
    server: str = "1.2.3.4",
    port: int = 443,
    secret_hex: str = "000102030405060708090a0b0c0d0e0f",
    source_name: str = "@test_channel",
    source_type: SourceType = SourceType.TELEGRAM_CHANNEL,
    source_url: str | None = "https://t.me/s/test_channel",
) -> DiscoveredProxyCandidate:
    proxy = MTProtoProxy(
        server=server,
        port=port,
        secret=ProxySecret(secret_hex),
        secret_type=SecretType.LEGACY,
    )
    return DiscoveredProxyCandidate(
        proxy=proxy,
        source_type=source_type,
        source_name=source_name,
        source_url=source_url,
        raw_reference=f"tg://proxy?server={server}&port={port}&secret={secret_hex}",
    )


class TestDiscoveryPersistence:
    async def test_first_discovery_creates_proxy_and_provenance(self, db: Database) -> None:
        candidate = _make_candidate()
        t0 = datetime(2026, 9, 16, 10, 0, 0, tzinfo=UTC)

        async with db.session_scope() as session:
            proxy_id, is_new = await persist_candidate(session, candidate, now=t0)
            assert is_new is True

        async with db.session_scope() as session:
            proxy = await session.get(Proxy, proxy_id)
            assert proxy is not None
            assert proxy.server == "1.2.3.4"
            assert proxy.port == 443
            assert proxy.secret.reveal() == "000102030405060708090a0b0c0d0e0f"
            assert proxy.fingerprint == candidate.proxy.fingerprint
            assert proxy.is_active is True
            assert proxy.first_seen_at == t0
            assert proxy.last_seen_at == t0
            assert proxy.next_test_at == t0

            discoveries_stmt = select(ProxyDiscovery).where(ProxyDiscovery.proxy_id == proxy_id)
            discoveries = (await session.execute(discoveries_stmt)).scalars().all()
            assert len(discoveries) == 1
            d = discoveries[0]
            assert d.source_type == SourceType.TELEGRAM_CHANNEL
            assert d.source_name == "@test_channel"
            assert d.source_url == "https://t.me/s/test_channel"
            assert d.discovered_at == t0

    async def test_duplicate_discovery_updates_last_seen_and_preserves_first_seen(
        self, db: Database
    ) -> None:
        candidate = _make_candidate()
        t0 = datetime(2026, 9, 16, 10, 0, 0, tzinfo=UTC)
        t1 = t0 + timedelta(hours=2)

        # First sighting at t0
        async with db.session_scope() as session:
            proxy_id1, is_new1 = await persist_candidate(session, candidate, now=t0)
            assert is_new1 is True

        # Second sighting of the same proxy at t1 from another channel
        candidate2 = _make_candidate(source_name="@second_channel")
        async with db.session_scope() as session:
            proxy_id2, is_new2 = await persist_candidate(session, candidate2, now=t1)
            assert is_new2 is False
            assert proxy_id1 == proxy_id2

        # Verify database state
        async with db.session_scope() as session:
            proxy = await session.get(Proxy, proxy_id1)
            assert proxy is not None
            assert proxy.first_seen_at == t0
            assert proxy.last_seen_at == t1  # updated

            count_stmt = select(func.count()).select_from(Proxy)
            assert await session.scalar(count_stmt) == 1  # Still 1 unique proxy row

            discoveries_stmt = (
                select(ProxyDiscovery)
                .where(ProxyDiscovery.proxy_id == proxy_id1)
                .order_by(ProxyDiscovery.discovered_at)
            )
            discoveries = (await session.execute(discoveries_stmt)).scalars().all()
            assert len(discoveries) == 2
            assert discoveries[0].source_name == "@test_channel"
            assert discoveries[0].discovered_at == t0
            assert discoveries[1].source_name == "@second_channel"
            assert discoveries[1].discovered_at == t1

    async def test_batch_persistence_metrics(self, db: Database) -> None:
        c1 = _make_candidate(server="1.1.1.1")
        c2 = _make_candidate(server="2.2.2.2")
        c3 = _make_candidate(server="1.1.1.1", source_name="@another")  # duplicate of c1

        service = DiscoveryService(db)
        result = await service.save_candidates([c1, c2, c3])

        assert result.total_candidates == 3
        assert result.new_proxies == 2
        assert result.updated_proxies == 1
        assert result.discoveries_recorded == 3

        async with db.session_scope() as session:
            proxies_count = await session.scalar(select(func.count()).select_from(Proxy))
            assert proxies_count == 2
            disc_count = await session.scalar(select(func.count()).select_from(ProxyDiscovery))
            assert disc_count == 3

    async def test_concurrent_discovery_safety(self, db: Database) -> None:
        """Simulate multiple discovery workers finding the same proxy concurrently."""

        async def worker_task(worker_id: int) -> tuple[int, bool]:
            async with db.session_scope() as session:
                cand = _make_candidate(source_name=f"@worker_{worker_id}")
                return await persist_candidate(session, cand)

        results = await asyncio.gather(*(worker_task(i) for i in range(5)))

        proxy_ids = {r[0] for r in results}
        assert len(proxy_ids) == 1  # Exactly one unique proxy ID allocated
        assert sum(1 for _proxy_id, is_new in results if is_new) == 1

        async with db.session_scope() as session:
            proxies_count = await session.scalar(select(func.count()).select_from(Proxy))
            assert proxies_count == 1
            disc_count = await session.scalar(select(func.count()).select_from(ProxyDiscovery))
            assert disc_count == 5

    async def test_rediscovery_does_not_reset_tester_schedule(self, db: Database) -> None:
        candidate = _make_candidate()
        t0 = datetime(2026, 9, 16, 10, 0, 0, tzinfo=UTC)
        t1 = t0 + timedelta(hours=6)
        scheduled = t0 + timedelta(hours=1)

        async with db.session_scope() as session:
            proxy_id, is_new = await persist_candidate(session, candidate, now=t0)
            assert is_new is True
            proxy = await session.get(Proxy, proxy_id)
            assert proxy is not None
            proxy.next_test_at = scheduled
            proxy.test_attempts = 4
            proxy.last_test_finished_at = t0
            proxy.test_lock_until = t0 + timedelta(minutes=2)

        async with db.session_scope() as session:
            proxy_id2, is_new2 = await persist_candidate(session, candidate, now=t1)
            assert is_new2 is False
            assert proxy_id2 == proxy_id
            proxy = await session.get(Proxy, proxy_id2)
            assert proxy is not None
            assert proxy.next_test_at == scheduled
            assert proxy.test_attempts == 4
            assert proxy.last_test_finished_at == t0
            assert proxy.test_lock_until == t0 + timedelta(minutes=2)
            assert proxy.last_seen_at == t1
            assert proxy.is_active is True

    async def test_rejects_naive_timestamp(self, db: Database) -> None:
        candidate = _make_candidate()
        naive = datetime(2026, 9, 16, 10, 0, 0)
        async with db.session_scope() as session:
            with pytest.raises(ValueError, match="timezone-aware"):
                await persist_candidate(session, candidate, now=naive)

    async def test_harvest_from_raw_text_fixture(self, db: Database) -> None:
        """Local smoke: in-memory text, no public Telegram and no loopback HTTP."""
        from modules.discovery.http import SsrfSafeHttpClient
        from modules.discovery.service import DiscoveryService
        from modules.discovery.sources.raw_http import RawTextSource

        text = (
            "tg://proxy?server=1.2.3.4&port=443&secret=000102030405060708090a0b0c0d0e0f\n"
            "https://t.me/proxy?server=1.2.3.4&port=443&secret=000102030405060708090a0b0c0d0e0f\n"
        )
        source = RawTextSource(text, source_name="fixture_list")
        result = await DiscoveryService(db).harvest(
            [source], http_client=SsrfSafeHttpClient(), concurrency=1
        )
        assert result.sources_attempted == 1
        assert result.source_failures == 0
        assert result.total_candidates == 1
        assert result.new_proxies == 1
        assert result.discoveries_recorded == 1
