"""Publication observability against real PostgreSQL. Fake Telegram only."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import func, select

from core.database import Database
from core.models import (
    ProxyPublication,
    PublicationCounter,
    PublicationSchedule,
    PublicationStatus,
    PublisherHeartbeat,
    utcnow,
)
from modules.publishing.fake import FakeTelegramPublisher
from modules.publishing.health import PublicationHealthService, touch_publisher_heartbeat
from modules.publishing.metrics import PublicationMetrics, load_counters, persist_counter_deltas
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
    async with db.session_scope() as session:
        stored = await load_counters(session)
        channel = await load_counters(session, channel_id=CHANNEL)
    assert stored["publication_success_total"] == 1
    assert stored["publications_rejected_total"] == 1
    assert channel["publication_success_total"] == 1


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
    async with db.session_scope() as session:
        stored = await load_counters(session)
    assert stored["telegram_rate_limits_total"] == 1
    assert stored["publication_retries_total"] == 1
    assert stored["publication_failures_total"] == 1


@pytest.mark.asyncio
async def test_persistent_counters_survive_a_new_metrics_instance(db: Database) -> None:
    async with db.session_scope() as session:
        await persist_counter_deltas(session, {"publication_success_total": 3})
    fresh = PublicationMetrics()
    assert fresh.publication_success_total == 0
    async with db.session_scope() as session:
        stored = await load_counters(session)
    assert stored["publication_success_total"] == 3


@pytest.mark.asyncio
async def test_concurrent_counter_increments_do_not_lose_updates(db: Database) -> None:
    async def bump() -> None:
        async with db.session_scope() as session:
            await persist_counter_deltas(session, {"publication_success_total": 1})

    await asyncio.gather(bump(), bump())
    async with db.session_scope() as session:
        stored = await load_counters(session)
    assert stored["publication_success_total"] == 2


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
        session.add(
            PublisherHeartbeat(
                worker_id="w1",
                worker_type="publishing-worker",
                last_seen_at=now - timedelta(seconds=10),
            )
        )
        session.add(PublicationCounter(name="publication_success_total", channel_id="", value=9))

    async with db.session_scope() as session:
        before_pub = (
            await session.execute(select(func.count()).select_from(ProxyPublication))
        ).scalar_one()
        before_hb = (await session.execute(select(PublisherHeartbeat.last_seen_at))).scalar_one()
        before_counter = (await session.execute(select(PublicationCounter.value))).scalar_one()
        before_schedules = (
            await session.execute(select(func.count()).select_from(PublicationSchedule))
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
        after_hb = (await session.execute(select(PublisherHeartbeat.last_seen_at))).scalar_one()
        after_counter = (await session.execute(select(PublicationCounter.value))).scalar_one()
        after_schedules = (
            await session.execute(select(func.count()).select_from(PublicationSchedule))
        ).scalar_one()
        after_statuses = sorted(
            (
                await session.execute(select(ProxyPublication.proxy_id, ProxyPublication.status))
            ).all()
        )

    assert before_pub == after_pub == 4
    assert before_hb == after_hb
    assert before_counter == after_counter == 9
    assert before_schedules == after_schedules == 0
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
    assert health.heartbeat_state == "healthy"
    assert health.counters["publication_success_total"] == 9

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
            worker_id="abc",
            worker_type="publishing-worker",
            interval_seconds=60,
            now=now,
        )
        assert wrote is True
        skipped = await touch_publisher_heartbeat(
            session,
            worker_id="abc",
            worker_type="publishing-worker",
            interval_seconds=60,
            now=now + timedelta(seconds=10),
        )
        assert skipped is False
        row = (await session.execute(select(PublisherHeartbeat))).scalar_one()
        assert row.last_seen_at == now
        assert row.worker_id == "abc"
        assert row.worker_type == "publishing-worker"

        later = now + timedelta(seconds=61)
        wrote_again = await touch_publisher_heartbeat(
            session,
            worker_id="abc",
            worker_type="publishing-worker",
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
        assert health.heartbeat_state == "healthy"
        stale = await PublicationHealthService(
            channel_id=CHANNEL, heartbeat_stale_seconds=30
        ).snapshot(session, now=later + timedelta(seconds=31))
        assert stale.heartbeat_last_seen_at == later
        assert stale.heartbeat_stale is True
        assert stale.heartbeat_state == "stale"
        assert stale.heartbeats[0].status == "stale"


@pytest.mark.asyncio
async def test_multiple_publisher_heartbeats_are_distinct(db: Database) -> None:
    now = utcnow()
    async with db.session_scope() as session:
        await touch_publisher_heartbeat(session, worker_id="proc-a", interval_seconds=1, now=now)
        await touch_publisher_heartbeat(
            session, worker_id="proc-b", interval_seconds=1, now=now - timedelta(seconds=200)
        )
        health = await PublicationHealthService(
            channel_id=CHANNEL, heartbeat_stale_seconds=180
        ).snapshot(session, now=now)
    assert {item.worker_id for item in health.heartbeats} == {"proc-a", "proc-b"}
    by_id = {item.worker_id: item.status for item in health.heartbeats}
    assert by_id["proc-a"] == "healthy"
    assert by_id["proc-b"] == "stale"
    assert health.healthy_publisher_count == 1
    assert health.stale_publisher_count == 1
    assert health.heartbeat_state == "healthy"


@pytest.mark.asyncio
async def test_restart_creates_a_new_heartbeat_identity(db: Database) -> None:
    now = utcnow()
    async with db.session_scope() as session:
        await touch_publisher_heartbeat(
            session, worker_id="run-old", interval_seconds=1, now=now - timedelta(seconds=10)
        )
        await touch_publisher_heartbeat(session, worker_id="run-new", interval_seconds=1, now=now)
        count = (
            await session.execute(select(func.count()).select_from(PublisherHeartbeat))
        ).scalar_one()
        health = await PublicationHealthService(channel_id=CHANNEL).snapshot(session, now=now)
    assert count == 2
    assert {item.worker_id for item in health.heartbeats} == {"run-old", "run-new"}


@pytest.mark.asyncio
async def test_zero_interval_does_not_write_heartbeat(db: Database) -> None:
    async with db.session_scope() as session:
        wrote = await touch_publisher_heartbeat(
            session, worker_id="abc", interval_seconds=0, now=utcnow()
        )
        assert wrote is False
        count = (
            await session.execute(select(func.count()).select_from(PublisherHeartbeat))
        ).scalar_one()
    assert count == 0


@pytest.mark.asyncio
async def test_metric_persist_failure_does_not_change_publication(db: Database) -> None:
    now = utcnow()
    proxy = await _add_proxy(db, server="1.1.1.1")
    await _ready(db, proxy.id, score="90.000", now=now)
    service = _service(db, FakeTelegramPublisher())
    with patch(
        "modules.publishing.service.persist_counter_deltas",
        side_effect=RuntimeError("metrics boom"),
    ):
        result = await service.publish_cycle(as_of=now, limit=5, now=now)
    assert result.published == 1
    async with db.session_scope() as session:
        row = (await session.execute(select(ProxyPublication))).scalar_one()
        stored = await load_counters(session)
    assert row.status == PublicationStatus.PUBLISHED
    assert stored["publication_success_total"] == 0


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
