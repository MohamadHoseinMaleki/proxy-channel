"""Integration tests for ranking against real PostgreSQL."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

from core.database import Database
from core.models import SCORING_VERSION_V1, Proxy, ProxyObservation, ProxyScore, utcnow
from modules.ranking.policy import MAX_AGE_HOURS, MAX_LIMIT
from modules.ranking.service import RankingService
from tests.integration.conftest import make_proxy

RankingService.__test__ = False  # type: ignore[attr-defined]

pytestmark = pytest.mark.integration

SECRET = "dd" + "ab" * 16


async def _add_proxy(
    db: Database,
    *,
    server: str,
    secret: str = SECRET,
    active: bool = True,
) -> Proxy:
    proxy = make_proxy(server=server, port=443, secret=secret)
    proxy.is_active = active
    async with db.session_scope() as session:
        session.add(proxy)
        await session.flush()
        await session.refresh(proxy)
        return proxy


async def _add_score(
    db: Database,
    proxy_id: int,
    *,
    score: str,
    hours_ago: float = 0.5,
    samples: int = 8,
    version: str = SCORING_VERSION_V1,
    calculated_at: datetime | None = None,
) -> None:
    moment = calculated_at if calculated_at is not None else utcnow() - timedelta(hours=hours_ago)
    reliability = Decimal("80.00") if samples else None
    async with db.session_scope() as session:
        session.add(
            ProxyScore(
                proxy_id=proxy_id,
                score=Decimal(score),
                calculated_at=moment,
                scoring_version=version,
                reliability_24h=reliability,
                sample_count_24h=samples,
                latency_p50_ms=Decimal("2100.000") if samples else None,
                latency_p95_ms=Decimal("2500.000") if samples else None,
            )
        )


@pytest.mark.asyncio
async def test_empty_database_ranks_empty(db: Database) -> None:
    page = await RankingService(db).list_top(as_of=utcnow(), limit=10)
    assert page.items == ()


@pytest.mark.asyncio
async def test_latest_snapshot_wins_and_is_not_duplicated(db: Database) -> None:
    proxy = await _add_proxy(db, server="203.0.113.10")
    await _add_score(db, proxy.id, score="40.000", hours_ago=3.0)
    await _add_score(db, proxy.id, score="85.000", hours_ago=0.2)
    page = await RankingService(db).list_top(as_of=utcnow(), limit=10)
    assert len(page.items) == 1
    assert page.items[0].proxy_id == proxy.id
    assert page.items[0].score == Decimal("85.000")
    assert page.items[0].server == "203.0.113.10"
    assert page.items[0].port == 443


@pytest.mark.asyncio
async def test_ordering_and_limit(db: Database) -> None:
    now = utcnow()
    for i, score in enumerate(("10.000", "90.000", "50.000", "70.000")):
        proxy = await _add_proxy(db, server=f"203.0.113.{20 + i}")
        await _add_score(db, proxy.id, score=score, calculated_at=now - timedelta(minutes=i + 1))
    page = await RankingService(db).list_top(as_of=now, limit=3)
    assert [item.score for item in page.items] == [
        Decimal("90.000"),
        Decimal("70.000"),
        Decimal("50.000"),
    ]


@pytest.mark.asyncio
async def test_inactive_wrong_version_stale_and_empty_window_excluded(db: Database) -> None:
    now = utcnow()
    inactive = await _add_proxy(db, server="203.0.113.30", active=False)
    await _add_score(db, inactive.id, score="99.000", calculated_at=now)

    old_version = await _add_proxy(db, server="203.0.113.31")
    await _add_score(db, old_version.id, score="99.000", version="v0", calculated_at=now)

    stale = await _add_proxy(db, server="203.0.113.32")
    await _add_score(
        db,
        stale.id,
        score="99.000",
        calculated_at=now - timedelta(hours=MAX_AGE_HOURS, seconds=1),
    )

    empty = await _add_proxy(db, server="203.0.113.33")
    await _add_score(db, empty.id, score="0.000", samples=0, calculated_at=now)

    keep = await _add_proxy(db, server="203.0.113.34")
    await _add_score(db, keep.id, score="12.000", calculated_at=now)

    page = await RankingService(db).list_top(as_of=now, limit=20)
    assert [item.proxy_id for item in page.items] == [keep.id]


@pytest.mark.asyncio
async def test_invalid_limit_is_rejected(db: Database) -> None:
    service = RankingService(db)
    with pytest.raises(ValueError, match=r"1\.\.100"):
        await service.list_top(limit=0, as_of=utcnow())
    with pytest.raises(ValueError, match=r"1\.\.100"):
        await service.list_top(limit=MAX_LIMIT + 1, as_of=utcnow())


@pytest.mark.asyncio
async def test_ranking_does_not_mutate_or_delete(db: Database) -> None:
    proxy = await _add_proxy(db, server="203.0.113.40")
    await _add_score(db, proxy.id, score="40.000", hours_ago=2.0)
    await _add_score(db, proxy.id, score="60.000", hours_ago=0.2)
    async with db.session_scope() as session:
        row = (await session.execute(select(Proxy).where(Proxy.id == proxy.id))).scalar_one()
        next_before = row.next_test_at
        lock_before = row.test_lock_until
        attempts_before = row.test_attempts

    await RankingService(db).list_top(as_of=utcnow(), limit=10)

    async with db.session_scope() as session:
        row = (await session.execute(select(Proxy).where(Proxy.id == proxy.id))).scalar_one()
        assert row.next_test_at == next_before
        assert row.test_lock_until == lock_before
        assert row.test_attempts == attempts_before
        score_count = (
            await session.execute(select(func.count()).select_from(ProxyScore))
        ).scalar_one()
        assert score_count == 2
        obs_count = (
            await session.execute(select(func.count()).select_from(ProxyObservation))
        ).scalar_one()
        assert obs_count == 0


@pytest.mark.asyncio
async def test_secret_never_appears_in_listing_or_logs(
    db: Database, json_logs: pytest.CaptureFixture[str]
) -> None:
    from core.logger import configure_logging
    from tests.conftest import make_settings

    configure_logging(make_settings(log_format="json", log_level="DEBUG"), force=True)
    secret = "dd" + "cd" * 16
    proxy = await _add_proxy(db, server="203.0.113.41", secret=secret)
    await _add_score(db, proxy.id, score="33.000")
    page = await RankingService(db).list_top(as_of=utcnow(), limit=5)
    payload = json.dumps(page.to_public_dict())
    rendered = repr(page) + payload + json_logs.readouterr().out
    assert secret not in rendered
    assert "tg://" not in rendered
    assert page.items[0].secret_type == "dd"


@pytest.mark.asyncio
async def test_repeated_query_is_deterministic(db: Database) -> None:
    now = utcnow()
    same = now - timedelta(minutes=3)
    for i in (4, 1, 9):
        proxy = await _add_proxy(db, server=f"203.0.113.{50 + i}")
        await _add_score(db, proxy.id, score="40.000", calculated_at=same)
    service = RankingService(db)
    first = await service.list_top(as_of=now, limit=10)
    second = await service.list_top(as_of=now, limit=10)
    assert [item.proxy_id for item in first.items] == [item.proxy_id for item in second.items]
    assert first.items[0].proxy_id < first.items[1].proxy_id < first.items[2].proxy_id


@pytest.mark.asyncio
async def test_concurrent_reads_agree(db: Database) -> None:
    now = utcnow()
    for i in range(5):
        proxy = await _add_proxy(db, server=f"203.0.113.{60 + i}")
        await _add_score(db, proxy.id, score=str(10 * (i + 1)), calculated_at=now)
    left = RankingService(db)
    right = RankingService(db)
    first, second = await asyncio.gather(
        left.list_top(as_of=now, limit=10),
        right.list_top(as_of=now, limit=10),
    )
    assert [item.proxy_id for item in first.items] == [item.proxy_id for item in second.items]


@pytest.mark.asyncio
async def test_latest_score_lookup_uses_the_composite_index(db: Database) -> None:
    proxy = await _add_proxy(db, server="203.0.113.70")
    await _add_score(db, proxy.id, score="10.000")
    async with db.session_scope() as session:
        await session.execute(text("SET LOCAL enable_seqscan = off"))
        plan = (
            await session.execute(
                text(
                    "EXPLAIN SELECT DISTINCT ON (proxy_id) id FROM proxy_scores "
                    "WHERE scoring_version = 'v1' "
                    "ORDER BY proxy_id, calculated_at DESC, id DESC"
                )
            )
        ).fetchall()
        rendered = " ".join(row[0] for row in plan)
        assert "ix_proxy_scores_proxy_id_calculated_at" in rendered
