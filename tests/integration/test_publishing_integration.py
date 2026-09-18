"""Integration tests for publishing against real PostgreSQL. Fake Telegram only."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from core.database import Database
from core.models import (
    SCORING_VERSION_V1,
    Proxy,
    ProxyObservation,
    ProxyPublication,
    ProxyScore,
    PublicationStatus,
    utcnow,
)
from modules.publishing.fake import FakeTelegramPublisher
from modules.publishing.service import PublishingService
from modules.reporting.service import ReportingService
from tests.integration.conftest import make_proxy

PublishingService.__test__ = False  # type: ignore[attr-defined]

pytestmark = pytest.mark.integration

DD = "dd" + "ab" * 16
CHANNEL = "@proxy_channel"


async def _add_proxy(db: Database, *, server: str, secret: str = DD) -> Proxy:
    proxy = make_proxy(server=server, port=443, secret=secret)
    async with db.session_scope() as session:
        session.add(proxy)
        await session.flush()
        await session.refresh(proxy)
        return proxy


async def _ready(db: Database, proxy_id: int, *, score: str, now: datetime) -> None:
    p50 = Decimal("2100.000")
    async with db.session_scope() as session:
        session.add(
            ProxyScore(
                proxy_id=proxy_id,
                score=Decimal(score),
                calculated_at=now,
                scoring_version=SCORING_VERSION_V1,
                reliability_24h=Decimal("80.00"),
                sample_count_24h=8,
                latency_p50_ms=p50,
                latency_p95_ms=Decimal("2500.000"),
            )
        )
        session.add(
            ProxyObservation(
                proxy_id=proxy_id,
                observed_at=now - timedelta(hours=0.2),
                success=True,
                mtproto_connect_ms=2100.0,
                tester_version="v1",
            )
        )


@pytest.mark.asyncio
async def test_select_top_is_published_and_not_duplicated(db: Database) -> None:
    now = utcnow()
    first = await _add_proxy(db, server="1.1.1.1")
    second = await _add_proxy(db, server="1.0.0.1", secret="dd" + "01" * 16)
    await _ready(db, first.id, score="90.000", now=now)
    await _ready(db, second.id, score="40.000", now=now)

    publisher = FakeTelegramPublisher()
    service = PublishingService(
        db,
        publisher=publisher,
        channel_id=CHANNEL,
        reporting=ReportingService(db),
    )
    result = await service.publish_cycle(as_of=now, limit=10)
    assert result.published == 2
    assert result.failed == 0
    assert len(publisher.messages) == 2
    assert publisher.messages[0].startswith("MTProto proxy")
    assert "1.1.1.1" in publisher.messages[0]

    async with db.session_scope() as session:
        rows = (
            (await session.execute(select(ProxyPublication).order_by(ProxyPublication.id)))
            .scalars()
            .all()
        )
        assert [row.status for row in rows] == [
            PublicationStatus.SUCCESS,
            PublicationStatus.SUCCESS,
        ]
        assert [row.telegram_message_id for row in rows] == [1, 2]
        assert all(row.channel_id == CHANNEL for row in rows)
        scores = (await session.execute(select(func.count()).select_from(ProxyScore))).scalar_one()
        observations = (
            await session.execute(select(func.count()).select_from(ProxyObservation))
        ).scalar_one()
        assert scores == 2
        assert observations == 2

    again = await service.publish_cycle(as_of=now, limit=10)
    assert again.published == 0
    assert again.skipped == 2
    assert len(publisher.messages) == 2


@pytest.mark.asyncio
async def test_telegram_failure_is_persisted_and_batch_continues(db: Database) -> None:
    now = utcnow()
    high = await _add_proxy(db, server="1.0.0.1", secret="dd" + "11" * 16)
    low = await _add_proxy(db, server="1.0.0.2", secret="dd" + "22" * 16)
    await _ready(db, high.id, score="90.000", now=now)
    await _ready(db, low.id, score="20.000", now=now)

    publisher = FakeTelegramPublisher(fail_on_index=(0,))
    service = PublishingService(
        db,
        publisher=publisher,
        channel_id=CHANNEL,
        reporting=ReportingService(db),
    )
    result = await service.publish_cycle(as_of=now, limit=10)
    assert result.published == 1
    assert result.failed == 1

    async with db.session_scope() as session:
        rows = (
            (await session.execute(select(ProxyPublication).order_by(ProxyPublication.id)))
            .scalars()
            .all()
        )
        assert len(rows) == 2
        assert rows[0].status == PublicationStatus.FAILURE
        assert rows[0].telegram_message_id is None
        assert rows[0].error_message_safe == "telegram_unavailable"
        assert rows[1].status == PublicationStatus.SUCCESS
        assert rows[1].telegram_message_id == 1


@pytest.mark.asyncio
async def test_logs_do_not_include_secrets(
    db: Database, json_logs: pytest.CaptureFixture[str]
) -> None:
    from core.logger import configure_logging
    from tests.conftest import make_settings

    configure_logging(make_settings(log_format="json", log_level="DEBUG"), force=True)
    now = utcnow()
    secret = "dd" + "cd" * 16
    proxy = await _add_proxy(db, server="8.8.8.8", secret=secret)
    await _ready(db, proxy.id, score="33.000", now=now)
    publisher = FakeTelegramPublisher()
    service = PublishingService(
        db,
        publisher=publisher,
        channel_id=CHANNEL,
        reporting=ReportingService(db),
    )
    report = await service.publish_cycle(as_of=now, limit=5)
    output = json_logs.readouterr().out + repr(report)
    assert secret not in output
    assert "tg://" not in output
