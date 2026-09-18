"""Integration tests for publishing against real PostgreSQL. Fake Telegram only."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from core.database import Database
from core.identity import PROTOCOL_MTPROTO, ProxySecret, compute_fingerprint
from core.models import (
    SCORING_VERSION_V1,
    Proxy,
    ProxyObservation,
    ProxyPublication,
    ProxyScore,
    PublicationSchedule,
    PublicationStatus,
    utcnow,
)
from modules.discovery.models import SecretType
from modules.publishing.claim import claim_due_publications, recover_stale_publications
from modules.publishing.fake import FakeTelegramPublisher
from modules.publishing.protocol import PublishResult
from modules.publishing.scheduler import schedule_new_publications
from modules.publishing.service import PublishingService
from modules.publishing.validation import PublicationRejection
from modules.reporting.models import Report, ReportItem
from modules.reporting.service import ReportingService
from modules.reporting.urls import canonical_tg_proxy_url
from modules.scoring.models import ScoreFreshness
from tests.conftest import make_settings
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


def _service(
    db: Database,
    publisher: FakeTelegramPublisher,
    **overrides: object,
) -> PublishingService:
    settings = make_settings(**overrides)
    return PublishingService(
        db,
        publisher=publisher,
        channel_id=CHANNEL,
        reporting=ReportingService(db),
        settings=settings,
    )


@pytest.mark.asyncio
async def test_select_top_is_published_and_not_duplicated(db: Database) -> None:
    now = utcnow()
    first = await _add_proxy(db, server="1.1.1.1")
    second = await _add_proxy(db, server="1.0.0.1", secret="dd" + "01" * 16)
    await _ready(db, first.id, score="90.000", now=now)
    await _ready(db, second.id, score="40.000", now=now)

    publisher = FakeTelegramPublisher()
    service = _service(db, publisher, telegram_publication_interval_seconds=60)
    first_tick = await service.publish_cycle(as_of=now, limit=10, now=now)
    assert first_tick.published == 1
    later = now + timedelta(seconds=60)
    second_tick = await service.publish_cycle(as_of=later, limit=10, now=later)
    assert second_tick.published == 1
    assert first_tick.failed == 0
    assert second_tick.failed == 0
    assert len(publisher.messages) == 2
    assert "1.1.1.1" in publisher.messages[0]

    async with db.session_scope() as session:
        rows = (
            (await session.execute(select(ProxyPublication).order_by(ProxyPublication.id)))
            .scalars()
            .all()
        )
        assert [row.status for row in rows] == [
            PublicationStatus.PUBLISHED,
            PublicationStatus.PUBLISHED,
        ]
        assert [row.telegram_message_id for row in rows] == [1, 2]
        assert all(row.attempt_count == 1 for row in rows)

    again = await service.publish_cycle(as_of=later, limit=10, now=later)
    assert again.published == 0
    assert again.skipped == 2
    assert len(publisher.messages) == 2


@pytest.mark.asyncio
async def test_transient_failure_retries_then_succeeds(db: Database) -> None:
    now = utcnow()
    proxy = await _add_proxy(db, server="1.0.0.1", secret="dd" + "11" * 16)
    await _ready(db, proxy.id, score="90.000", now=now)

    failing = FakeTelegramPublisher(
        results=[
            PublishResult(ok=False, telegram_message_id=None, error_safe="timeout", retryable=True)
        ]
    )
    service = _service(db, failing, telegram_retry_base_seconds=10, telegram_max_retries=4)
    first = await service.publish_cycle(as_of=now, limit=10, now=now)
    assert first.published == 0
    assert first.retried == 1
    assert first.failed == 0

    async with db.session_scope() as session:
        row = (await session.execute(select(ProxyPublication))).scalar_one()
        assert row.status == PublicationStatus.PENDING
        assert row.next_attempt_at == now + timedelta(seconds=10)
        assert row.attempt_count == 1
        assert row.error_message_safe == "timeout"

    later = now + timedelta(seconds=11)
    service.publisher = FakeTelegramPublisher()
    second = await service.publish_cycle(as_of=later, limit=10, now=later)
    assert second.published == 1
    assert len(service.publisher.messages) == 1


@pytest.mark.asyncio
async def test_permanent_failure_is_not_retried(db: Database) -> None:
    now = utcnow()
    proxy = await _add_proxy(db, server="1.0.0.2", secret="dd" + "22" * 16)
    await _ready(db, proxy.id, score="90.000", now=now)
    publisher = FakeTelegramPublisher(
        results=[
            PublishResult(
                ok=False,
                telegram_message_id=None,
                error_safe="HTTP 400: chat not found",
                error_code=400,
                retryable=False,
            )
        ]
    )
    service = _service(db, publisher)
    result = await service.publish_cycle(as_of=now, limit=10, now=now)
    assert result.failed == 1
    assert result.published == 0
    async with db.session_scope() as session:
        row = (await session.execute(select(ProxyPublication))).scalar_one()
        assert row.status == PublicationStatus.FAILED
        assert row.telegram_message_id is None


@pytest.mark.asyncio
async def test_exhausted_retries_become_failed(db: Database) -> None:
    now = utcnow()
    proxy = await _add_proxy(db, server="8.8.8.8", secret="dd" + "33" * 16)
    await _ready(db, proxy.id, score="50.000", now=now)
    publisher = FakeTelegramPublisher(
        results=[
            PublishResult(ok=False, telegram_message_id=None, error_safe="boom", retryable=True)
        ]
    )
    service = _service(db, publisher, telegram_max_retries=1, telegram_retry_base_seconds=1)
    result = await service.publish_cycle(as_of=now, limit=5, now=now)
    assert result.failed == 1
    async with db.session_scope() as session:
        row = (await session.execute(select(ProxyPublication))).scalar_one()
        assert row.status == PublicationStatus.FAILED


@pytest.mark.asyncio
async def test_http_429_schedules_retry_after(db: Database) -> None:
    now = utcnow()
    proxy = await _add_proxy(db, server="1.1.1.1", secret="dd" + "44" * 16)
    await _ready(db, proxy.id, score="70.000", now=now)
    publisher = FakeTelegramPublisher(
        results=[
            PublishResult(
                ok=False,
                telegram_message_id=None,
                error_safe="HTTP 429: flood",
                error_code=429,
                retry_after=25,
                retryable=True,
            )
        ]
    )
    service = _service(db, publisher, telegram_retry_base_seconds=2, telegram_retry_max_seconds=300)
    result = await service.publish_cycle(as_of=now, limit=5, now=now)
    assert result.retried == 1
    async with db.session_scope() as session:
        row = (await session.execute(select(ProxyPublication))).scalar_one()
        assert row.next_attempt_at == now + timedelta(seconds=25)


@pytest.mark.asyncio
async def test_one_failure_does_not_stop_the_batch(db: Database) -> None:
    now = utcnow()
    high = await _add_proxy(db, server="1.0.0.1", secret="dd" + "55" * 16)
    low = await _add_proxy(db, server="1.0.0.2", secret="dd" + "66" * 16)
    await _ready(db, high.id, score="90.000", now=now)
    await _ready(db, low.id, score="20.000", now=now)
    publisher = FakeTelegramPublisher(
        results=[
            PublishResult(
                ok=False,
                telegram_message_id=None,
                error_safe="HTTP 400: bad",
                error_code=400,
                retryable=False,
            ),
            PublishResult(ok=True, telegram_message_id=9),
        ]
    )
    service = _service(db, publisher, telegram_publication_interval_seconds=60)
    first_tick = await service.publish_cycle(as_of=now, limit=10, now=now)
    later = now + timedelta(seconds=60)
    second_tick = await service.publish_cycle(as_of=later, limit=10, now=later)
    assert first_tick.failed == 1
    assert second_tick.published == 1
    assert len(publisher.messages) == 2


@pytest.mark.asyncio
async def test_stale_sending_is_recovered(db: Database) -> None:
    now = utcnow()
    proxy = await _add_proxy(db, server="1.0.0.3", secret="dd" + "77" * 16)
    await _ready(db, proxy.id, score="40.000", now=now)
    async with db.session_scope() as session:
        session.add(
            ProxyPublication(
                proxy_id=proxy.id,
                channel_id=CHANNEL,
                status=PublicationStatus.SENDING,
                lease_until=now - timedelta(seconds=1),
                next_attempt_at=now - timedelta(seconds=30),
                attempt_count=1,
            )
        )

    publisher = FakeTelegramPublisher()
    service = _service(db, publisher)
    result = await service.publish_cycle(as_of=now, limit=10, now=now)
    assert result.recovered == 1
    assert result.published == 1
    assert len(publisher.messages) == 1
    async with db.session_scope() as session:
        row = (await session.execute(select(ProxyPublication))).scalar_one()
        assert row.status == PublicationStatus.PUBLISHED


@pytest.mark.asyncio
async def test_crash_after_telegram_accept_retries_at_least_once(db: Database) -> None:
    """Telegram accepted, DB success never committed.

    Recovery will send again. Bot API has no idempotency key, so this is
    at-least-once, not exactly-once.
    """
    now = utcnow()
    proxy = await _add_proxy(db, server="8.8.4.4", secret="dd" + "88" * 16)
    await _ready(db, proxy.id, score="55.000", now=now)
    publisher = FakeTelegramPublisher()
    service = _service(db, publisher, telegram_publication_lease_seconds=30)

    report = await service.reporting.select_top(limit=10, as_of=now)
    await service._enqueue(list(report.items), now=now)
    async with db.session_scope() as session:
        claimed = await claim_due_publications(
            session,
            channel_id=CHANNEL,
            proxy_ids=[proxy.id],
            lease_seconds=30,
            now=now,
        )
    assert len(claimed) == 1
    accepted = await publisher.publish("crash-window")
    assert accepted.ok is True
    # Crash: no mark_published.

    later = now + timedelta(seconds=31)
    async with db.session_scope() as session:
        recovered = await recover_stale_publications(session, channel_id=CHANNEL, now=later)
    assert recovered == [claimed[0].id]

    second = FakeTelegramPublisher()
    service.publisher = second
    result = await service.publish_cycle(as_of=later, limit=10, now=later)
    assert result.published == 1
    assert len(second.messages) == 1
    assert len(publisher.messages) == 1
    async with db.session_scope() as session:
        count = (
            await session.execute(select(func.count()).select_from(ProxyPublication))
        ).scalar_one()
        status = (await session.execute(select(ProxyPublication.status))).scalar_one()
    assert count == 1
    assert status == PublicationStatus.PUBLISHED


@pytest.mark.asyncio
async def test_two_sessions_cannot_claim_the_same_publication(db: Database) -> None:
    now = utcnow()
    proxy = await _add_proxy(db, server="1.0.0.4", secret="dd" + "99" * 16)
    await _ready(db, proxy.id, score="10.000", now=now)
    async with db.session_scope() as session:
        session.add(
            ProxyPublication(
                proxy_id=proxy.id,
                channel_id=CHANNEL,
                status=PublicationStatus.PENDING,
                next_attempt_at=now,
            )
        )

    first = db.session()
    second = db.session()
    try:
        claimed_first = await claim_due_publications(
            first, channel_id=CHANNEL, proxy_ids=[proxy.id], now=now
        )
        claimed_second = await asyncio.wait_for(
            claim_due_publications(second, channel_id=CHANNEL, proxy_ids=[proxy.id], now=now),
            timeout=5.0,
        )
        assert len(claimed_first) == 1
        assert claimed_second == []
        await first.commit()
        await second.commit()
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_logs_do_not_include_secrets(
    db: Database, json_logs: pytest.CaptureFixture[str]
) -> None:
    from core.logger import configure_logging

    configure_logging(make_settings(log_format="json", log_level="DEBUG"), force=True)
    now = utcnow()
    secret = "dd" + "cd" * 16
    proxy = await _add_proxy(db, server="8.8.8.8", secret=secret)
    await _ready(db, proxy.id, score="33.000", now=now)
    publisher = FakeTelegramPublisher()
    service = _service(db, publisher)
    report = await service.publish_cycle(as_of=now, limit=5, now=now)
    output = json_logs.readouterr().out + repr(report)
    assert secret not in output
    assert "tg://" not in output
    token = "123456789:AATestTokenNotARealSecretValue"
    assert token not in output


@pytest.mark.asyncio
async def test_invalid_and_fake_tls_never_call_telegram(db: Database) -> None:
    now = utcnow()
    proxy = await _add_proxy(db, server="1.1.1.1", secret="dd" + "ab" * 16)
    await _ready(db, proxy.id, score="90.000", now=now)
    publisher = FakeTelegramPublisher()
    service = _service(db, publisher)
    selected = await service.reporting.select_top(limit=10, as_of=now)
    assert len(selected.items) == 1
    original = selected.items[0]
    fake_tls = ReportItem(
        proxy_id=original.proxy_id + 9000,
        server="8.8.8.8",
        port=443,
        secret=ProxySecret("ee" + "11" * 16),
        protocol=PROTOCOL_MTPROTO,
        secret_type=SecretType.FAKE_TLS.value,
        fingerprint=compute_fingerprint(server="8.8.8.8", port=443, secret="ee" + "11" * 16),
        score=original.score,
        scoring_version=SCORING_VERSION_V1,
        reliability_24h=original.reliability_24h,
        sample_count_24h=original.sample_count_24h,
        latency_p50_ms=original.latency_p50_ms,
        latency_p95_ms=original.latency_p95_ms,
        last_success_at=original.last_success_at,
        freshness=ScoreFreshness.RECENT,
        url=canonical_tg_proxy_url(server="8.8.8.8", port=443, secret="ee" + "11" * 16),
    )
    bad_host = ReportItem(
        proxy_id=original.proxy_id + 9001,
        server="127.0.0.1",
        port=443,
        secret=ProxySecret("dd" + "22" * 16),
        protocol=PROTOCOL_MTPROTO,
        secret_type=SecretType.SECURE_RANDOMIZED.value,
        fingerprint=compute_fingerprint(server="127.0.0.1", port=443, secret="dd" + "22" * 16),
        score=original.score,
        scoring_version=SCORING_VERSION_V1,
        reliability_24h=original.reliability_24h,
        sample_count_24h=original.sample_count_24h,
        latency_p50_ms=original.latency_p50_ms,
        latency_p95_ms=original.latency_p95_ms,
        last_success_at=original.last_success_at,
        freshness=ScoreFreshness.RECENT,
        url=canonical_tg_proxy_url(server="1.1.1.1", port=443, secret="dd" + "22" * 16),
    )
    mixed = Report(
        items=(fake_tls, original, bad_host),
        generated_at=selected.generated_at,
        limit=selected.limit,
        max_success_age_hours=selected.max_success_age_hours,
        scoring_version=selected.scoring_version,
    )
    result = await service.publish_report(mixed, now=now)
    assert result.published == 1
    assert result.skipped >= 2
    assert len(publisher.messages) == 1
    assert "1.1.1.1" in publisher.messages[0]
    assert publisher.messages[0].splitlines()[-1].startswith("tg://proxy?")
    assert PublicationRejection.FAKE_TLS.value == "fake_tls"


@pytest.mark.asyncio
async def test_select_top_item_is_not_mutated(db: Database) -> None:
    now = utcnow()
    proxy = await _add_proxy(db, server="8.8.4.4", secret="dd" + "99" * 16)
    await _ready(db, proxy.id, score="41.000", now=now)
    service = _service(db, FakeTelegramPublisher())
    before = await service.reporting.select_top(limit=5, as_of=now)
    snapshot = (
        before.items[0].proxy_id,
        before.items[0].server,
        before.items[0].port,
        before.items[0].score,
        before.items[0].url,
    )
    await service.publish_report(before, now=now)
    assert (
        before.items[0].proxy_id,
        before.items[0].server,
        before.items[0].port,
        before.items[0].score,
        before.items[0].url,
    ) == snapshot


@pytest.mark.asyncio
async def test_enqueue_conflict_does_not_reset_published(db: Database) -> None:
    now = utcnow()
    proxy = await _add_proxy(db, server="1.0.0.5", secret="dd" + "ab" * 16)
    await _ready(db, proxy.id, score="12.000", now=now)
    service = _service(db, FakeTelegramPublisher())
    await service.publish_cycle(as_of=now, limit=5, now=now)
    again = await service.publish_cycle(as_of=now, limit=5, now=now)
    assert again.published == 0
    async with db.session_scope() as session:
        rows = (await session.execute(select(ProxyPublication))).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == PublicationStatus.PUBLISHED


@pytest.mark.asyncio
async def test_interval_blocks_a_second_new_publication(db: Database) -> None:
    now = utcnow()
    first = await _add_proxy(db, server="1.1.1.1")
    second = await _add_proxy(db, server="1.0.0.1", secret="dd" + "01" * 16)
    await _ready(db, first.id, score="90.000", now=now)
    await _ready(db, second.id, score="40.000", now=now)
    publisher = FakeTelegramPublisher()
    service = _service(db, publisher, telegram_publication_interval_seconds=300)
    first_tick = await service.publish_cycle(as_of=now, limit=10, now=now)
    assert first_tick.published == 1
    too_soon = await service.publish_cycle(as_of=now + timedelta(seconds=10), limit=10, now=now)
    assert too_soon.published == 0
    async with db.session_scope() as session:
        count = (
            await session.execute(select(func.count()).select_from(ProxyPublication))
        ).scalar_one()
        last = (await session.execute(select(PublicationSchedule.last_scheduled_at))).scalar_one()
    assert count == 1
    assert last == now
    later = now + timedelta(seconds=300)
    second_tick = await service.publish_cycle(as_of=later, limit=10, now=later)
    assert second_tick.published == 1
    assert len(publisher.messages) == 2


@pytest.mark.asyncio
async def test_already_published_is_not_enqueued_again(db: Database) -> None:
    now = utcnow()
    proxy = await _add_proxy(db, server="8.8.8.8", secret="dd" + "aa" * 16)
    await _ready(db, proxy.id, score="70.000", now=now)
    service = _service(db, FakeTelegramPublisher(), telegram_publication_interval_seconds=1)
    await service.publish_cycle(as_of=now, limit=5, now=now)
    later = now + timedelta(seconds=2)
    again = await service.publish_cycle(as_of=later, limit=5, now=later)
    assert again.published == 0
    async with db.session_scope() as session:
        rows = (await session.execute(select(ProxyPublication))).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == PublicationStatus.PUBLISHED


@pytest.mark.asyncio
async def test_failed_publication_does_not_spawn_infinite_rows(db: Database) -> None:
    now = utcnow()
    proxy = await _add_proxy(db, server="1.0.0.9", secret="dd" + "bb" * 16)
    await _ready(db, proxy.id, score="60.000", now=now)
    fail = PublishResult(ok=False, telegram_message_id=None, error_safe="timeout", retryable=True)
    publisher = FakeTelegramPublisher(results=[fail, fail])
    service = _service(
        db,
        publisher,
        telegram_publication_interval_seconds=1,
        telegram_retry_base_seconds=1,
        telegram_max_retries=8,
    )
    await service.publish_cycle(as_of=now, limit=5, now=now)
    later = now + timedelta(seconds=2)
    await service.publish_cycle(as_of=later, limit=5, now=later)
    async with db.session_scope() as session:
        rows = (await session.execute(select(ProxyPublication))).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == PublicationStatus.PENDING
    assert rows[0].attempt_count >= 1


@pytest.mark.asyncio
async def test_pending_threshold_blocks_new_enqueues(db: Database) -> None:
    now = utcnow()
    first = await _add_proxy(db, server="1.1.1.1")
    second = await _add_proxy(db, server="1.0.0.1", secret="dd" + "cc" * 16)
    await _ready(db, first.id, score="90.000", now=now)
    await _ready(db, second.id, score="80.000", now=now)
    publisher = FakeTelegramPublisher(
        results=[
            PublishResult(ok=False, telegram_message_id=None, error_safe="timeout", retryable=True)
        ]
    )
    service = _service(
        db,
        publisher,
        telegram_publication_interval_seconds=1,
        telegram_publication_max_pending=1,
        telegram_retry_base_seconds=60,
        telegram_max_retries=8,
    )
    first_tick = await service.publish_cycle(as_of=now, limit=10, now=now)
    assert first_tick.retried == 1
    later = now + timedelta(seconds=2)
    second_tick = await service.publish_cycle(as_of=later, limit=10, now=later)
    assert second_tick.published == 0
    async with db.session_scope() as session:
        rows = (await session.execute(select(ProxyPublication))).scalars().all()
    assert len(rows) == 1
    assert rows[0].proxy_id == first.id


@pytest.mark.asyncio
async def test_schedule_state_survives_a_new_service_instance(db: Database) -> None:
    now = utcnow()
    first = await _add_proxy(db, server="1.1.1.1")
    second = await _add_proxy(db, server="9.9.9.9", secret="dd" + "dd" * 16)
    await _ready(db, first.id, score="90.000", now=now)
    await _ready(db, second.id, score="80.000", now=now)
    await _service(
        db, FakeTelegramPublisher(), telegram_publication_interval_seconds=300
    ).publish_cycle(as_of=now, limit=10, now=now)
    restarted = _service(db, FakeTelegramPublisher(), telegram_publication_interval_seconds=300)
    blocked = await restarted.publish_cycle(as_of=now, limit=10, now=now)
    assert blocked.published == 0
    async with db.session_scope() as session:
        count = (
            await session.execute(select(func.count()).select_from(ProxyPublication))
        ).scalar_one()
        stamp = (await session.execute(select(PublicationSchedule.last_scheduled_at))).scalar_one()
    assert count == 1
    assert stamp == now


@pytest.mark.asyncio
async def test_two_sessions_cannot_both_open_the_cadence_slot(db: Database) -> None:
    now = utcnow()
    first = await _add_proxy(db, server="1.1.1.1")
    second = await _add_proxy(db, server="1.0.0.1", secret="dd" + "ee" * 16)
    await _ready(db, first.id, score="90.000", now=now)
    await _ready(db, second.id, score="80.000", now=now)
    service = _service(db, FakeTelegramPublisher())
    report = await service.reporting.select_top(limit=10, as_of=now)
    items = list(report.items)
    assert len(items) == 2
    async with db.session_scope() as session:
        session.add(PublicationSchedule(channel_id=CHANNEL, last_scheduled_at=None))

    left = db.session()
    right = db.session()
    try:
        scheduled_left = await schedule_new_publications(
            left,
            items,
            channel_id=CHANNEL,
            interval_seconds=300,
            now=now,
        )
        scheduled_right = await asyncio.wait_for(
            schedule_new_publications(
                right,
                items,
                channel_id=CHANNEL,
                interval_seconds=300,
                now=now,
            ),
            timeout=5.0,
        )
        assert len(scheduled_left) == 1
        assert scheduled_right == []
        await left.commit()
        await right.commit()
    finally:
        await left.close()
        await right.close()

    async with db.session_scope() as session:
        count = (
            await session.execute(select(func.count()).select_from(ProxyPublication))
        ).scalar_one()
    assert count == 1


@pytest.mark.asyncio
async def test_scheduler_does_not_reorder_select_top(db: Database) -> None:
    now = utcnow()
    low = await _add_proxy(db, server="1.0.0.1", secret="dd" + "01" * 16)
    high = await _add_proxy(db, server="1.1.1.1")
    await _ready(db, low.id, score="10.000", now=now)
    await _ready(db, high.id, score="99.000", now=now)
    service = _service(db, FakeTelegramPublisher(), telegram_publication_interval_seconds=60)
    report = await service.reporting.select_top(limit=10, as_of=now)
    assert [item.proxy_id for item in report.items] == [high.id, low.id]
    async with db.session_scope() as session:
        scheduled = await schedule_new_publications(
            session,
            list(report.items),
            channel_id=CHANNEL,
            interval_seconds=60,
            now=now,
        )
    assert [item.proxy_id for item in scheduled] == [high.id]
