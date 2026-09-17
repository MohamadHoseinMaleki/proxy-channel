"""Integration tests for TesterService and tester worker against real PostgreSQL."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select

import workers.tester as tester_worker
from core.database import Database
from core.lifecycle import WorkerLifecycle
from core.models import ErrorCategory, Proxy, ProxyObservation, utcnow
from modules.tester.models import TesterResult, TransportType
from modules.tester.service import TesterService
from tests.conftest import make_settings
from tests.integration.conftest import make_proxy

TesterService.__test__ = False  # type: ignore[attr-defined]
TesterResult.__test__ = False  # type: ignore[attr-defined]

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_tester_batch_claiming_and_observation_persistence(db: Database) -> None:
    """Test full cycle: claim due proxies, run probe, write observation, update proxy."""
    # 1. Seed two due proxies
    p1 = make_proxy(server="198.51.100.1", port=443, secret="dd" + "11" * 16)
    p2 = make_proxy(server="198.51.100.2", port=443, secret="dd" + "22" * 16)

    async with db.session_scope() as session:
        session.add_all([p1, p2])

    service = TesterService(db, batch_size=10)

    # 2. Mock probe_proxy to return 1 success and 1 failure
    async def mock_probe(
        *,
        proxy_id: int,
        server: str,
        port: int,
        **_kwargs: object,
    ) -> TesterResult:
        del port
        if server == "198.51.100.1":
            return TesterResult(
                proxy_id=proxy_id,
                transport_type=TransportType.RANDOMIZED_INTERMEDIATE,
                success=True,
                tcp_connect_ms=12.5,
                mtproto_connect_ms=35.0,
                total_latency_ms=47.5,
            )
        return TesterResult(
            proxy_id=proxy_id,
            transport_type=TransportType.RANDOMIZED_INTERMEDIATE,
            success=False,
            tcp_connect_ms=14.0,
            error_category=ErrorCategory.MT_PROTO_TIMEOUT,
            error_message_safe="MTProto handshake timeout",
        )

    with patch("modules.tester.service.probe_proxy", side_effect=mock_probe):
        results = await service.run_batch()

    assert len(results) == 2
    successes = [r for r in results if r.success]
    failures = [r for r in results if not r.success]
    assert len(successes) == 1
    assert len(failures) == 1

    # 3. Verify observations were stored in database
    async with db.session_scope() as session:
        obs_rows = (
            (await session.execute(select(ProxyObservation).order_by(ProxyObservation.proxy_id)))
            .scalars()
            .all()
        )

        assert len(obs_rows) == 2
        obs_p1 = obs_rows[0]
        assert obs_p1.success is True
        assert obs_p1.tcp_connect_ms == 12.5
        assert obs_p1.mtproto_connect_ms == 35.0
        assert obs_p1.total_latency_ms == 47.5
        assert obs_p1.error_category is None

        obs_p2 = obs_rows[1]
        assert obs_p2.success is False
        assert obs_p2.error_category == "MT_PROTO_TIMEOUT"
        assert obs_p2.error_message_safe == "MTProto handshake timeout"

        # 4. Verify proxy rows were updated and leases released
        proxies = (await session.execute(select(Proxy).order_by(Proxy.id))).scalars().all()

        assert len(proxies) == 2
        # Both proxies must have cleared their lease
        assert proxies[0].test_lock_until is None
        assert proxies[1].test_lock_until is None

        # Both proxies have last_test_finished_at set
        assert proxies[0].last_test_finished_at is not None
        assert proxies[1].last_test_finished_at is not None

        # Success proxy scheduled 1h forward, failure 15min forward
        now = utcnow()
        assert proxies[0].last_success_at is not None
        assert proxies[0].next_test_at > now + timedelta(minutes=50)

        assert proxies[1].last_failure_at is not None
        assert proxies[1].last_error_category == "MT_PROTO_TIMEOUT"
        assert proxies[1].next_test_at <= now + timedelta(minutes=20)


@pytest.mark.asyncio
async def test_tester_worker_tick_integration(db: Database) -> None:
    """Test worker tick execution with an injected live Database."""
    p = make_proxy(server="198.51.100.3", port=443, secret="dd" + "33" * 16)
    async with db.session_scope() as session:
        session.add(p)

    settings = make_settings(
        worker_poll_interval_seconds=0.001,
        tester_batch_size=5,
    )

    async def mock_probe(*_args: object, **_kwargs: object) -> TesterResult:
        return TesterResult(
            proxy_id=1,
            transport_type=TransportType.RANDOMIZED_INTERMEDIATE,
            success=True,
            tcp_connect_ms=10.0,
            mtproto_connect_ms=20.0,
            total_latency_ms=30.0,
        )

    with patch("modules.tester.service.probe_proxy", side_effect=mock_probe):
        async with WorkerLifecycle("tester-worker", settings=settings) as life:
            await tester_worker.tick(life, db=db)

    # Verify tick completed and updated DB
    async with db.session_scope() as session:
        obs = (await session.execute(select(ProxyObservation))).scalars().all()
        assert len(obs) == 1
        assert obs[0].success is True


@pytest.mark.asyncio
async def test_cancelled_batch_releases_lease(db: Database) -> None:
    """In-process cancellation must not leave test_lock_until set."""
    p = make_proxy(server="198.51.100.9", port=443, secret="dd" + "99" * 16)
    async with db.session_scope() as session:
        session.add(p)

    service = TesterService(db, batch_size=5)

    async def hang(*_args: object, **_kwargs: object) -> TesterResult:
        await asyncio.sleep(30)
        msg = "probe was not cancelled"
        raise AssertionError(msg)

    with patch("modules.tester.service.probe_proxy", side_effect=hang):
        runner = asyncio.create_task(service.run_batch())
        await asyncio.sleep(0.05)
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner

    async with db.session_scope() as session:
        stored = (await session.execute(select(Proxy))).scalar_one()
        assert stored.test_lock_until is None
        obs = (await session.execute(select(ProxyObservation))).scalars().all()
        assert len(obs) == 1
        assert obs[0].success is False
        assert obs[0].error_category == "CANCELLED"


@pytest.mark.asyncio
async def test_unrecorded_claim_is_reclaimable_after_lease_expiry(db: Database) -> None:
    """Process kill never clears the lease; expiry must make the row due again."""
    from modules.scheduling import claim_due_proxies

    p = make_proxy(server="198.51.100.10", port=443, secret="dd" + "aa" * 16)
    async with db.session_scope() as session:
        session.add(p)

    moment = utcnow()
    async with db.session_scope() as session:
        claimed = await claim_due_proxies(session, lease_seconds=30, now=moment)
        assert len(claimed) == 1
        assert claimed[0].test_lock_until == moment + timedelta(seconds=30)

    # Still leased: a second tester must skip it.
    async with db.session_scope() as session:
        assert await claim_due_proxies(session, now=moment + timedelta(seconds=5)) == []

    # After expiry (and without any record_result / cleanup): reclaimable.
    async with db.session_scope() as session:
        reclaimed = await claim_due_proxies(session, now=moment + timedelta(seconds=31))
        assert len(reclaimed) == 1
        assert reclaimed[0].server == "198.51.100.10"
