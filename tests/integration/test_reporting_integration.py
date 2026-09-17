"""Integration tests for reporting against real PostgreSQL."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from core.database import Database
from core.models import (
    SCORING_VERSION_V1,
    ErrorCategory,
    Proxy,
    ProxyObservation,
    ProxyScore,
    utcnow,
)
from modules.discovery.parser import parse_proxy_url
from modules.reporting.policy import MAX_LIMIT, MAX_SUCCESS_AGE_HOURS
from modules.reporting.service import ReportingService
from tests.integration.conftest import make_proxy

ReportingService.__test__ = False  # type: ignore[attr-defined]

pytestmark = pytest.mark.integration

DD = "dd" + "ab" * 16
EE = "ee" + "11" * 16


async def _add_proxy(
    db: Database,
    *,
    server: str,
    secret: str = DD,
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
    latency_p50: str | None = "2100.000",
) -> None:
    moment = calculated_at if calculated_at is not None else utcnow() - timedelta(hours=hours_ago)
    reliability = Decimal("80.00") if samples else None
    p50 = None if latency_p50 is None else Decimal(latency_p50)
    p95 = None if p50 is None else max(p50, Decimal("2500.000"))
    async with db.session_scope() as session:
        session.add(
            ProxyScore(
                proxy_id=proxy_id,
                score=Decimal(score),
                calculated_at=moment,
                scoring_version=version,
                reliability_24h=reliability,
                sample_count_24h=samples,
                latency_p50_ms=p50,
                latency_p95_ms=p95,
            )
        )


async def _add_observation(
    db: Database,
    proxy_id: int,
    *,
    success: bool,
    hours_ago: float,
    category: str | None = None,
    now: datetime | None = None,
) -> None:
    moment = (now if now is not None else utcnow()) - timedelta(hours=hours_ago)
    async with db.session_scope() as session:
        session.add(
            ProxyObservation(
                proxy_id=proxy_id,
                observed_at=moment,
                success=success,
                mtproto_connect_ms=2100.0 if success else None,
                error_category=None if success else (category or ErrorCategory.MT_PROTO_TIMEOUT),
                tester_version="v1",
            )
        )


@pytest.mark.asyncio
async def test_empty_database_reports_empty(db: Database) -> None:
    report = await ReportingService(db).select_top(as_of=utcnow(), limit=10)
    assert report.items == ()
    assert report.to_json_dict()["count"] == 0
    assert report.to_txt() == ""


@pytest.mark.asyncio
async def test_latest_score_wins_and_is_not_duplicated(db: Database) -> None:
    now = utcnow()
    proxy = await _add_proxy(db, server="1.1.1.1")
    await _add_score(db, proxy.id, score="40.000", hours_ago=3.0)
    await _add_score(db, proxy.id, score="85.000", hours_ago=0.2)
    await _add_observation(db, proxy.id, success=True, hours_ago=0.2, now=now)
    report = await ReportingService(db).select_top(as_of=now, limit=10)
    assert len(report.items) == 1
    assert report.items[0].score == Decimal("85.000")
    assert report.items[0].server == "1.1.1.1"


@pytest.mark.asyncio
async def test_ordering_limit_and_exclusions(db: Database) -> None:
    now = utcnow()
    keep_high = await _add_proxy(db, server="1.0.0.1", secret="dd" + "01" * 16)
    await _add_score(db, keep_high.id, score="90.000", calculated_at=now)
    await _add_observation(db, keep_high.id, success=True, hours_ago=0.1, now=now)

    keep_mid = await _add_proxy(db, server="1.0.0.2", secret="dd" + "02" * 16)
    await _add_score(db, keep_mid.id, score="50.000", calculated_at=now)
    await _add_observation(db, keep_mid.id, success=True, hours_ago=0.1, now=now)

    keep_low = await _add_proxy(db, server="1.0.0.3", secret="dd" + "03" * 16)
    await _add_score(db, keep_low.id, score="20.000", calculated_at=now)
    await _add_observation(db, keep_low.id, success=True, hours_ago=0.1, now=now)

    stale = await _add_proxy(db, server="8.8.8.8", secret="dd" + "04" * 16)
    await _add_score(db, stale.id, score="99.000", calculated_at=now)
    await _add_observation(db, stale.id, success=True, hours_ago=MAX_SUCCESS_AGE_HOURS + 1, now=now)

    fake_tls = await _add_proxy(db, server="8.8.4.4", secret=EE)
    await _add_score(db, fake_tls.id, score="99.000", calculated_at=now)
    await _add_observation(db, fake_tls.id, success=True, hours_ago=0.1, now=now)

    cancelled = await _add_proxy(db, server="9.9.9.9", secret="dd" + "05" * 16)
    await _add_score(db, cancelled.id, score="99.000", calculated_at=now)
    await _add_observation(
        db,
        cancelled.id,
        success=False,
        hours_ago=0.05,
        now=now,
        category=ErrorCategory.CANCELLED,
    )

    unsupported = await _add_proxy(db, server="4.2.2.1", secret="dd" + "06" * 16)
    await _add_score(db, unsupported.id, score="99.000", calculated_at=now)
    await _add_observation(
        db,
        unsupported.id,
        success=False,
        hours_ago=0.05,
        now=now,
        category=ErrorCategory.UNSUPPORTED_TRANSPORT,
    )

    failed_after = await _add_proxy(db, server="4.2.2.2", secret="dd" + "07" * 16)
    await _add_score(db, failed_after.id, score="99.000", calculated_at=now)
    await _add_observation(db, failed_after.id, success=True, hours_ago=1.0, now=now)
    await _add_observation(
        db,
        failed_after.id,
        success=False,
        hours_ago=0.05,
        now=now,
        category=ErrorCategory.TCP_TIMEOUT,
    )

    wrong_version = await _add_proxy(db, server="1.0.0.4", secret="dd" + "08" * 16)
    await _add_score(db, wrong_version.id, score="99.000", version="v0", calculated_at=now)
    await _add_observation(db, wrong_version.id, success=True, hours_ago=0.1, now=now)

    report = await ReportingService(db).select_top(as_of=now, limit=2)
    assert [item.server for item in report.items] == ["1.0.0.1", "1.0.0.2"]
    assert all(item.scoring_version == SCORING_VERSION_V1 for item in report.items)


@pytest.mark.asyncio
async def test_cancelled_does_not_hide_a_recent_success(db: Database) -> None:
    now = utcnow()
    proxy = await _add_proxy(db, server="1.1.1.1")
    await _add_score(db, proxy.id, score="40.000", calculated_at=now)
    await _add_observation(db, proxy.id, success=True, hours_ago=0.5, now=now)
    await _add_observation(
        db,
        proxy.id,
        success=False,
        hours_ago=0.1,
        now=now,
        category=ErrorCategory.CANCELLED,
    )
    report = await ReportingService(db).select_top(as_of=now, limit=5)
    assert [item.proxy_id for item in report.items] == [proxy.id]


@pytest.mark.asyncio
async def test_json_txt_round_trip_and_no_mutation(db: Database) -> None:
    now = utcnow()
    proxy = await _add_proxy(db, server="8.8.8.8", secret=DD)
    await _add_score(db, proxy.id, score="33.000", calculated_at=now)
    await _add_observation(db, proxy.id, success=True, hours_ago=0.2, now=now)

    async with db.session_scope() as session:
        row = (await session.execute(select(Proxy).where(Proxy.id == proxy.id))).scalar_one()
        next_before = row.next_test_at
        lock_before = row.test_lock_until

    report = await ReportingService(db).select_top(as_of=now, limit=5)
    payload = report.to_json_dict()
    assert payload["count"] == 1
    assert payload["proxies"][0]["secret"] == DD
    parsed = parse_proxy_url(report.to_txt().strip())
    assert parsed.server == "8.8.8.8"
    assert parsed.secret.reveal() == DD

    async with db.session_scope() as session:
        row = (await session.execute(select(Proxy).where(Proxy.id == proxy.id))).scalar_one()
        assert row.next_test_at == next_before
        assert row.test_lock_until == lock_before
        assert (
            await session.execute(select(func.count()).select_from(ProxyScore))
        ).scalar_one() == 1
        assert (
            await session.execute(select(func.count()).select_from(ProxyObservation))
        ).scalar_one() == 1


@pytest.mark.asyncio
async def test_logs_do_not_include_secrets(
    db: Database, json_logs: pytest.CaptureFixture[str]
) -> None:
    from core.logger import configure_logging
    from tests.conftest import make_settings

    configure_logging(make_settings(log_format="json", log_level="DEBUG"), force=True)
    now = utcnow()
    secret = "dd" + "cd" * 16
    proxy = await _add_proxy(db, server="1.1.1.1", secret=secret)
    await _add_score(db, proxy.id, score="40.000", calculated_at=now)
    await _add_observation(db, proxy.id, success=True, hours_ago=0.1, now=now)
    report = await ReportingService(db).select_top(as_of=now, limit=5)
    output = json_logs.readouterr().out + repr(report)
    assert secret not in output
    assert "tg://" not in output


@pytest.mark.asyncio
async def test_invalid_limit_is_rejected(db: Database) -> None:
    service = ReportingService(db)
    with pytest.raises(ValueError, match=r"1\.\.100"):
        await service.select_top(limit=0, as_of=utcnow())
    with pytest.raises(ValueError, match=r"1\.\.100"):
        await service.select_top(limit=MAX_LIMIT + 1, as_of=utcnow())


@pytest.mark.asyncio
async def test_latency_tie_break_from_persisted_scores(db: Database) -> None:
    now = utcnow()
    slow = await _add_proxy(db, server="1.0.0.1", secret="dd" + "11" * 16)
    await _add_score(db, slow.id, score="50.000", calculated_at=now, latency_p50="4000.000")
    await _add_observation(db, slow.id, success=True, hours_ago=0.2, now=now)
    fast = await _add_proxy(db, server="1.0.0.2", secret="dd" + "22" * 16)
    await _add_score(db, fast.id, score="50.000", calculated_at=now, latency_p50="2100.000")
    await _add_observation(db, fast.id, success=True, hours_ago=0.2, now=now)
    report = await ReportingService(db).select_top(as_of=now, limit=10)
    assert [item.server for item in report.items] == ["1.0.0.2", "1.0.0.1"]
