"""HTTP ranking transport against a real PostgreSQL."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from core.database import Database
from core.models import SCORING_VERSION_V1, Proxy, ProxyObservation, ProxyScore, utcnow
from modules.api.app import create_app
from modules.ranking.policy import MAX_AGE_HOURS
from tests.conftest import make_settings
from tests.integration.conftest import make_proxy

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


@asynccontextmanager
async def api_client(db: Database) -> AsyncIterator[AsyncClient]:
    app = create_app(settings=make_settings(), database=db)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_healthz_does_not_need_rows(db: Database) -> None:
    async with api_client(db) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_readyz_when_postgres_is_up(db: Database) -> None:
    async with api_client(db) as client:
        response = await client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


@pytest.mark.asyncio
async def test_empty_database_lists_empty(db: Database) -> None:
    async with api_client(db) as client:
        response = await client.get("/v1/proxies")
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["count"] == 0
    assert "as_of" not in body


@pytest.mark.asyncio
async def test_http_matches_ranking_order_and_omits_secrets(
    db: Database, json_logs: pytest.CaptureFixture[str]
) -> None:
    now = utcnow()
    keep_high = await _add_proxy(db, server="203.0.113.10")
    keep_low = await _add_proxy(db, server="203.0.113.11")
    await _add_score(db, keep_high.id, score="90.000", calculated_at=now)
    await _add_score(db, keep_low.id, score="10.000", calculated_at=now)

    inactive = await _add_proxy(db, server="203.0.113.12", active=False)
    await _add_score(db, inactive.id, score="99.000", calculated_at=now)
    stale = await _add_proxy(db, server="203.0.113.13")
    await _add_score(
        db,
        stale.id,
        score="99.000",
        calculated_at=now - timedelta(hours=MAX_AGE_HOURS, seconds=1),
    )

    async with api_client(db) as client:
        response = await client.get("/v1/proxies", params={"limit": 10})
    assert response.status_code == 200
    body = response.json()
    assert [item["proxy_id"] for item in body["items"]] == [keep_high.id, keep_low.id]
    assert [item["score"] for item in body["items"]] == ["90.000", "10.000"]
    assert body["count"] == 2
    assert "as_of" not in body
    dumped = json.dumps(body) + json_logs.readouterr().out
    assert SECRET not in dumped
    assert "tg://" not in dumped
    assert "secret" not in body["items"][0]
    assert "fingerprint" not in body["items"][0]


@pytest.mark.asyncio
async def test_limit_one(db: Database) -> None:
    now = utcnow()
    first = await _add_proxy(db, server="203.0.113.20")
    second = await _add_proxy(db, server="203.0.113.21")
    await _add_score(db, first.id, score="80.000", calculated_at=now)
    await _add_score(db, second.id, score="20.000", calculated_at=now)
    async with api_client(db) as client:
        body = (await client.get("/v1/proxies", params={"limit": 1})).json()
    assert body["count"] == 1
    assert body["items"][0]["proxy_id"] == first.id
    assert body["limit"] == 1


@pytest.mark.asyncio
async def test_http_does_not_write(db: Database) -> None:
    proxy = await _add_proxy(db, server="203.0.113.30")
    await _add_score(db, proxy.id, score="40.000")
    async with db.session_scope() as session:
        row = (await session.execute(select(Proxy).where(Proxy.id == proxy.id))).scalar_one()
        next_before = row.next_test_at
        lock_before = row.test_lock_until
        attempts_before = row.test_attempts
    async with api_client(db) as client:
        assert (await client.get("/v1/proxies")).status_code == 200
    async with db.session_scope() as session:
        row = (await session.execute(select(Proxy).where(Proxy.id == proxy.id))).scalar_one()
        assert row.next_test_at == next_before
        assert row.test_lock_until == lock_before
        assert row.test_attempts == attempts_before
        assert (
            await session.execute(select(func.count()).select_from(ProxyScore))
        ).scalar_one() == 1
        assert (
            await session.execute(select(func.count()).select_from(ProxyObservation))
        ).scalar_one() == 0
