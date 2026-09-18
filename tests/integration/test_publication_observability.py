"""Publication observability against real PostgreSQL. Fake Telegram only."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from core.database import Database
from core.models import (
    ProxyPublication,
    PublicationStatus,
    PublisherHeartbeat,
    utcnow,
)
from modules.publishing.fake import FakeTelegramPublisher
from modules.publishing.health import PublicationHealthService, touch_publisher_heartbeat
from modules.publishing.metrics import PublicationMetrics
from modules.publishing.protocol import PublishResult
from tests.integration.test_publishing_integration import CHANNEL, _add_proxy, _ready, _service

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_metrics_count_success_and_rejection(db: Database) -> None:
    now = utcnow()
    proxy = await _add_proxy(db, server="1.1.1.1")
    await _ready(db, proxy.id, score="90.000", now=now)
    metrics = PublicationMetrics()
    service = _service(db, FakeTelegramPublisher())
    service.metrics = metrics
    from core.identity import PROTOCOL_MTPROTO, ProxySecret, compute_fingerprint
    from core.models import SCORING_VERSION_V1
    from modules.discovery.models import SecretType
    from modules.reporting.models import Report, ReportItem
    from modules.reporting.urls import canonical_tg_proxy_url
    from modules.scoring.models import ScoreFreshness

    selected = await service.reporting.select_top(limit=5, as_of=now)
    original = selected.items[0]
    secret = "ee" + "11" * 16
    fake_tls = ReportItem(
        proxy_id=original.proxy_id + 9000,
        server="8.8.8.8",
        port=443,
        secret=ProxySecret(secret),
        protocol=PROTOCOL_MTPROTO,
        secret_type=SecretType.FAKE_TLS.value,
        fingerprint=compute_fingerprint(server="8.8.8.8", port=443, secret=secret),
        score=original.score,
        scoring_version=SCORING_VERSION_V1,
        reliability_24h=original.reliability_24h,
        sample_count_24h=original.sample_count_24h,
        latency_p50_ms=original.latency_p50_ms,
        latency_p95_ms=original.latency_p95_ms,
        last_success_at=original.last_success_at,
        freshness=ScoreFreshness.RECENT,
        url=canonical_tg_proxy_url(server="8.8.8.8", port=443, secret=secret),
    )
    mixed = Report(
        items=(fake_tls, original),
        generated_at=selected.generated_at,
        limit=selected.limit,
        max_success_age_hours=selected.max_success_age_hours,
        scoring_version=selected.scoring_version,
    )
    result = await service.publish_report(mixed, now=now)
    assert result.published == 1
    assert result.skipped >= 1
    assert metrics.publication_success_total == 1
    assert metrics.publications_scheduled_total == 1
    assert metrics.publications_rejected_total == 1
    assert metrics.publication_failures_total == 0


@pytest.mark.asyncio
async def test_metrics_count_retry_rate_limit_and_failure(db: Database) -> None:
    now = utcnow()
    proxy = await _add_proxy(db, server="1.0.0.1", secret="dd" + "44" * 16)
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
    metrics = PublicationMetrics()
    service = _service(db, publisher, telegram_retry_base_seconds=2)
    service.metrics = metrics
    result = await service.publish_cycle(as_of=now, limit=5, now=now)
    assert result.retried == 1
    assert metrics.telegram_rate_limits_total == 1
    assert metrics.publication_retries_total == 1
    assert metrics.publication_failures_total == 0
    assert metrics.publication_success_total == 0

    later = now + timedelta(seconds=26)
    fail = FakeTelegramPublisher(
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
    service.publisher = fail
    failed = await service.publish_cycle(as_of=later, limit=5, now=later)
    assert failed.failed == 1
    assert metrics.publication_failures_total == 1
    assert metrics.publication_success_total == 0


@pytest.mark.asyncio
async def test_health_snapshot_is_read_only(db: Database) -> None:
    now = utcnow()
    pending = await _add_proxy(db, server="1.1.1.1")
    sending = await _add_proxy(db, server="1.0.0.1", secret="dd" + "01" * 16)
    published = await _add_proxy(db, server="8.8.8.8", secret="dd" + "02" * 16)
    failed = await _add_proxy(db, server="9.9.9.9", secret="dd" + "03" * 16)
    async with db.session_scope() as session:
        session.add(
            ProxyPublication(
                proxy_id=pending.id,
                channel_id=CHANNEL,
                status=PublicationStatus.PENDING,
                next_attempt_at=now - timedelta(hours=2),
                created_at=now - timedelta(hours=2),
            )
        )
        session.add(
            ProxyPublication(
                proxy_id=sending.id,
                channel_id=CHANNEL,
                status=PublicationStatus.SENDING,
                lease_until=now - timedelta(seconds=5),
                next_attempt_at=now - timedelta(minutes=1),
                last_attempt_at=now - timedelta(minutes=1),
                attempt_count=1,
            )
        )
        session.add(
            ProxyPublication(
                proxy_id=published.id,
                channel_id=CHANNEL,
                status=PublicationStatus.PUBLISHED,
                telegram_message_id=11,
                last_attempt_at=now - timedelta(minutes=3),
                attempt_count=1,
            )
        )
        session.add(
            ProxyPublication(
                proxy_id=failed.id,
                channel_id=CHANNEL,
                status=PublicationStatus.FAILED,
                error_message_safe="HTTP 400",
                last_attempt_at=now - timedelta(minutes=4),
                attempt_count=2,
            )
        )

    async with db.session_scope() as session:
        before_pub = (
            await session.execute(select(func.count()).select_from(ProxyPublication))
        ).scalar_one()
        before_hb = (
            await session.execute(select(func.count()).select_from(PublisherHeartbeat))
        ).scalar_one()
        statuses = sorted(
            (
                await session.execute(select(ProxyPublication.proxy_id, ProxyPublication.status))
            ).all()
        )
        health = await PublicationHealthService(
            channel_id=CHANNEL,
            stale_pending_seconds=3600,
            heartbeat_stale_seconds=180,
        ).snapshot(session, now=now)
        after_pub = (
            await session.execute(select(func.count()).select_from(ProxyPublication))
        ).scalar_one()
        after_hb = (
            await session.execute(select(func.count()).select_from(PublisherHeartbeat))
        ).scalar_one()
        after_statuses = sorted(
            (
                await session.execute(select(ProxyPublication.proxy_id, ProxyPublication.status))
            ).all()
        )

    assert before_pub == after_pub == 4
    assert before_hb == after_hb == 0
    assert statuses == after_statuses
    assert health.pending_count == 1
    assert health.sending_count == 1
    assert health.published_count == 1
    assert health.failed_count == 1
    assert health.stale_sending_count == 1
    assert health.stale_pending_count == 1
    assert health.oldest_pending_age_seconds is not None
    assert health.oldest_pending_age_seconds >= 7200
    assert health.last_successful_publication_at == now - timedelta(minutes=3)
    assert health.last_failed_publication_at == now - timedelta(minutes=4)
    assert health.heartbeat_last_seen_at is None
    assert health.heartbeat_stale is True

    async with db.session_scope() as session:
        still_sending = (
            await session.execute(
                select(ProxyPublication.status).where(ProxyPublication.proxy_id == sending.id)
            )
        ).scalar_one()
    assert still_sending == PublicationStatus.SENDING


@pytest.mark.asyncio
async def test_heartbeat_throttles_and_does_not_imply_health(db: Database) -> None:
    now = utcnow()
    async with db.session_scope() as session:
        wrote = await touch_publisher_heartbeat(
            session,
            worker_name="publishing-worker",
            run_id="abc",
            interval_seconds=60,
            now=now,
        )
        assert wrote is True
        skipped = await touch_publisher_heartbeat(
            session,
            worker_name="publishing-worker",
            run_id="def",
            interval_seconds=60,
            now=now + timedelta(seconds=10),
        )
        assert skipped is False
        row = (await session.execute(select(PublisherHeartbeat))).scalar_one()
        assert row.last_seen_at == now
        assert row.run_id == "abc"

        later = now + timedelta(seconds=61)
        wrote_again = await touch_publisher_heartbeat(
            session,
            worker_name="publishing-worker",
            run_id="ghi",
            interval_seconds=60,
            now=later,
        )
        assert wrote_again is True

    async with db.session_scope() as session:
        health = await PublicationHealthService(
            channel_id=CHANNEL, heartbeat_stale_seconds=180
        ).snapshot(session, now=later)
        assert health.heartbeat_last_seen_at == later
        assert health.heartbeat_stale is False
        stale = await PublicationHealthService(
            channel_id=CHANNEL, heartbeat_stale_seconds=30
        ).snapshot(session, now=later + timedelta(seconds=31))
        assert stale.heartbeat_last_seen_at == later
        assert stale.heartbeat_stale is True


@pytest.mark.asyncio
async def test_zero_interval_does_not_write_heartbeat(db: Database) -> None:
    async with db.session_scope() as session:
        wrote = await touch_publisher_heartbeat(session, interval_seconds=0, now=utcnow())
        assert wrote is False
        count = (
            await session.execute(select(func.count()).select_from(PublisherHeartbeat))
        ).scalar_one()
    assert count == 0


@pytest.mark.asyncio
async def test_classification_does_not_change_retry(db: Database) -> None:
    now = utcnow()
    proxy = await _add_proxy(db, server="8.8.4.4", secret="dd" + "88" * 16)
    await _ready(db, proxy.id, score="55.000", now=now)
    publisher = FakeTelegramPublisher(
        results=[
            PublishResult(
                ok=False,
                telegram_message_id=None,
                error_safe="timeout",
                retryable=True,
            )
        ]
    )
    service = _service(db, publisher, telegram_retry_base_seconds=10, telegram_max_retries=4)
    result = await service.publish_cycle(as_of=now, limit=5, now=now)
    assert result.retried == 1
    async with db.session_scope() as session:
        row = (await session.execute(select(ProxyPublication))).scalar_one()
        assert row.status == PublicationStatus.PENDING
        assert row.next_attempt_at == now + timedelta(seconds=10)
        assert row.error_message_safe == "timeout"
