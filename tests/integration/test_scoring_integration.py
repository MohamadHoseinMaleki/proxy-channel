"""Integration tests for scoring against real PostgreSQL."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

import workers.scorer as scorer_worker
from core.database import Database
from core.lifecycle import WorkerLifecycle
from core.models import (
    SCORING_VERSION_V1,
    ErrorCategory,
    Proxy,
    ProxyObservation,
    ProxyScore,
    utcnow,
)
from modules.scoring.calculator import score_observations
from modules.scoring.models import ObservationInput
from modules.scoring.service import ScoringService
from tests.conftest import make_settings
from tests.integration.conftest import make_proxy

ScoringService.__test__ = False  # type: ignore[attr-defined]

pytestmark = pytest.mark.integration


def _observation(
    proxy_id: int,
    *,
    success: bool,
    hours_ago: float = 0.1,
    mtproto: float | None = 2100.0,
    tcp: float | None = 12.0,
    category: str | None = None,
    now: datetime | None = None,
) -> ProxyObservation:
    moment = now if now is not None else utcnow()
    return ProxyObservation(
        proxy_id=proxy_id,
        observed_at=moment - timedelta(hours=hours_ago),
        success=success,
        tcp_connect_ms=tcp,
        mtproto_connect_ms=mtproto if success else None,
        total_latency_ms=(tcp or 0.0) + (mtproto or 0.0) if success else None,
        error_category=None if success else (category or ErrorCategory.MT_PROTO_TIMEOUT),
        tester_version="v1",
    )


async def _seed_tested_proxy(
    db: Database,
    *,
    server: str,
    successes: int,
    failures: int = 0,
) -> Proxy:
    now = utcnow()
    proxy = make_proxy(server=server, port=443, secret="dd" + "11" * 16)
    proxy.last_test_finished_at = now
    async with db.session_scope() as session:
        session.add(proxy)
        await session.flush()
        rows: list[ProxyObservation] = []
        for i in range(successes):
            rows.append(_observation(proxy.id, success=True, hours_ago=0.05 + i * 0.01, now=now))
        for i in range(failures):
            rows.append(
                _observation(
                    proxy.id,
                    success=False,
                    hours_ago=0.4 + i * 0.01,
                    now=now,
                    category=ErrorCategory.TCP_REFUSED,
                )
            )
        session.add_all(rows)
    return proxy


@pytest.mark.asyncio
async def test_score_snapshot_is_persisted_and_observations_unchanged(db: Database) -> None:
    proxy = await _seed_tested_proxy(db, server="198.51.100.10", successes=10)
    service = ScoringService(db, batch_size=10)
    results = await service.run_batch()
    assert len(results) == 1
    assert results[0].proxy_id == proxy.id
    assert results[0].score > Decimal("0.000")

    async with db.session_scope() as session:
        scores = (await session.execute(select(ProxyScore))).scalars().all()
        assert len(scores) == 1
        assert scores[0].scoring_version == SCORING_VERSION_V1
        assert scores[0].sample_count_24h == 10
        assert scores[0].reliability_24h == Decimal("100.000")

        obs = (await session.execute(select(ProxyObservation))).scalars().all()
        assert len(obs) == 10
        assert all(row.success is True for row in obs)
        assert all(row.mtproto_connect_ms == 2100.0 for row in obs)


@pytest.mark.asyncio
async def test_historical_scores_remain_intact(db: Database) -> None:
    proxy = await _seed_tested_proxy(db, server="198.51.100.11", successes=8)
    service = ScoringService(db, batch_size=10)
    first = await service.run_batch()
    assert len(first) == 1

    async with db.session_scope() as session:
        row = (await session.execute(select(Proxy).where(Proxy.id == proxy.id))).scalar_one()
        row.last_test_finished_at = utcnow()
        session.add(
            _observation(proxy.id, success=False, hours_ago=0.0, category=ErrorCategory.TCP_RESET)
        )

    second = await service.run_batch()
    assert len(second) == 1
    assert second[0].score != first[0].score

    async with db.session_scope() as session:
        scores = (
            (await session.execute(select(ProxyScore).order_by(ProxyScore.calculated_at)))
            .scalars()
            .all()
        )
        assert len(scores) == 2
        assert scores[0].id != scores[1].id
        assert scores[0].score == first[0].score
        obs_count = (
            await session.execute(select(func.count()).select_from(ProxyObservation))
        ).scalar_one()
        assert obs_count == 9


@pytest.mark.asyncio
async def test_repeated_scoring_is_deterministic_and_not_duplicated_until_new_tests(
    db: Database,
) -> None:
    proxy = await _seed_tested_proxy(db, server="198.51.100.12", successes=6, failures=2)
    service = ScoringService(db, batch_size=10)
    first = await service.run_batch()
    second = await service.run_batch()
    assert len(first) == 1
    assert second == []

    async with db.session_scope() as session:
        scores = (await session.execute(select(ProxyScore))).scalars().all()
        assert len(scores) == 1
        obs = (await session.execute(select(ProxyObservation))).scalars().all()
        inputs = [
            ObservationInput(
                observed_at=row.observed_at,
                success=row.success,
                mtproto_connect_ms=row.mtproto_connect_ms,
                tcp_connect_ms=row.tcp_connect_ms,
                total_latency_ms=row.total_latency_ms,
                error_category=row.error_category,
            )
            for row in obs
        ]
        recomputed = score_observations(proxy.id, inputs, now=first[0].calculated_at)
        assert recomputed.score == first[0].score
        assert recomputed.reliability_score == first[0].reliability_score
        assert recomputed.latency_score == first[0].latency_score


@pytest.mark.asyncio
async def test_concurrent_scoring_does_not_corrupt_or_double_claim(db: Database) -> None:
    now = utcnow()
    async with db.session_scope() as session:
        for i in range(20):
            proxy = make_proxy(
                server=f"198.51.100.{i + 20}",
                port=443,
                secret="dd" + f"{i:02x}" * 16,
            )
            proxy.last_test_finished_at = now
            session.add(proxy)
            await session.flush()
            session.add(_observation(proxy.id, success=True, now=now))

    left = ScoringService(db, batch_size=20)
    right = ScoringService(db, batch_size=20)
    first, second = await asyncio.gather(left.run_batch(), right.run_batch())
    scored_ids = [item.proxy_id for item in first] + [item.proxy_id for item in second]
    assert len(scored_ids) == 20
    assert len(set(scored_ids)) == 20

    async with db.session_scope() as session:
        score_count = (
            await session.execute(select(func.count()).select_from(ProxyScore))
        ).scalar_one()
        obs_count = (
            await session.execute(select(func.count()).select_from(ProxyObservation))
        ).scalar_one()
        assert score_count == 20
        assert obs_count == 20


@pytest.mark.asyncio
async def test_inactive_proxy_is_not_scored(db: Database) -> None:
    proxy = await _seed_tested_proxy(db, server="198.51.100.40", successes=4)
    async with db.session_scope() as session:
        row = (await session.execute(select(Proxy).where(Proxy.id == proxy.id))).scalar_one()
        row.is_active = False

    results = await ScoringService(db, batch_size=10).run_batch()
    assert results == []
    async with db.session_scope() as session:
        count = (await session.execute(select(func.count()).select_from(ProxyScore))).scalar_one()
        assert count == 0


@pytest.mark.asyncio
async def test_score_constraints_accept_the_snapshot(db: Database) -> None:
    await _seed_tested_proxy(db, server="198.51.100.41", successes=3, failures=1)
    await ScoringService(db, batch_size=5).run_batch()
    async with db.session_scope() as session:
        score = (await session.execute(select(ProxyScore))).scalar_one()
        assert Decimal("0") <= score.score <= Decimal("100")
        if score.latency_p50_ms is not None and score.latency_p95_ms is not None:
            assert score.latency_p95_ms >= score.latency_p50_ms
        assert score.sample_count_24h > 0
        assert score.reliability_24h is not None


@pytest.mark.asyncio
async def test_indexes_serve_latest_score_lookup(db: Database) -> None:
    await _seed_tested_proxy(db, server="198.51.100.42", successes=2)
    await ScoringService(db, batch_size=5).run_batch()
    async with db.session_scope() as session:
        await session.execute(text("SET LOCAL enable_seqscan = off"))
        plan = (
            await session.execute(
                text(
                    "EXPLAIN SELECT * FROM proxy_scores "
                    "WHERE proxy_id = 1 ORDER BY calculated_at DESC LIMIT 1"
                )
            )
        ).fetchall()
        rendered = " ".join(row[0] for row in plan)
        assert "ix_proxy_scores_proxy_id_calculated_at" in rendered


@pytest.mark.asyncio
async def test_scorer_worker_tick_persists_and_does_not_rescore_until_new_test(
    db: Database,
) -> None:
    await _seed_tested_proxy(db, server="198.51.100.43", successes=5)
    settings = make_settings(worker_poll_interval_seconds=0.001, scorer_batch_size=10)
    async with WorkerLifecycle("scoring-worker", settings=settings) as life:
        await scorer_worker.tick(life, db=db)
        await scorer_worker.tick(life, db=db)

    async with db.session_scope() as session:
        scores = (await session.execute(select(ProxyScore))).scalars().all()
        assert len(scores) == 1
        obs = (await session.execute(select(ProxyObservation))).scalars().all()
        assert len(obs) == 5
