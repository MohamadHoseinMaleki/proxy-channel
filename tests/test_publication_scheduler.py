"""Unit tests for publication cadence and dedup. No database, no Telegram."""

from __future__ import annotations

import ast
import pathlib
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.dialects import postgresql

from core.identity import PROTOCOL_MTPROTO, ProxySecret, compute_fingerprint
from core.models import SCORING_VERSION_V1, PublicationStatus
from modules.discovery.models import SecretType
from modules.publishing.scheduler import (
    DEFAULT_DEDUP_SECONDS,
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_MAX_PENDING,
    MAX_NEW_PER_SLOT,
    ExistingPublication,
    PublicationScheduler,
    choose_schedule_candidates,
    lock_schedule_statement,
    schedule_new_publications,
)
from modules.reporting.models import ReportItem
from modules.reporting.urls import canonical_tg_proxy_url
from modules.scoring.models import ScoreFreshness

PINNED = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)
DD = "dd" + "ab" * 16
CHANNEL = "@proxy_channel"
SCHEDULER_SRC = (
    pathlib.Path(__file__).resolve().parents[1] / "src" / "modules" / "publishing" / "scheduler.py"
)
DIALECT = postgresql.dialect()


def _item(proxy_id: int, *, server: str = "1.1.1.1") -> ReportItem:
    secret = DD
    url = canonical_tg_proxy_url(server=server, port=443, secret=secret)
    return ReportItem(
        proxy_id=proxy_id,
        server=server,
        port=443,
        secret=ProxySecret(secret),
        protocol=PROTOCOL_MTPROTO,
        secret_type=SecretType.SECURE_RANDOMIZED.value,
        fingerprint=compute_fingerprint(server=server, port=443, secret=secret),
        score=Decimal("85.000"),
        scoring_version=SCORING_VERSION_V1,
        reliability_24h=Decimal("90.00"),
        sample_count_24h=10,
        latency_p50_ms=Decimal("2100.000"),
        latency_p95_ms=Decimal("2500.000"),
        last_success_at=PINNED - timedelta(hours=0.5),
        freshness=ScoreFreshness.RECENT,
        url=url,
    )


class TestDefaults:
    def test_cadence_defaults_are_conservative(self) -> None:
        assert DEFAULT_INTERVAL_SECONDS == 300.0
        assert DEFAULT_DEDUP_SECONDS == 86400.0
        assert DEFAULT_MAX_PENDING == 20
        assert MAX_NEW_PER_SLOT == 1


class TestPurity:
    def test_does_not_import_scoring_or_ranking(self) -> None:
        names: set[str] = set()
        for node in ast.walk(ast.parse(SCHEDULER_SRC.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
        joined = " ".join(sorted(names))
        assert "modules.scoring" not in joined
        assert "modules.ranking" not in joined
        assert "telethon" not in names
        assert "httpx" not in names


class TestChooseCandidates:
    def test_preserves_caller_order_and_does_not_invent_ids(self) -> None:
        items = [_item(3, server="3.3.3.3"), _item(1), _item(2, server="2.2.2.2")]
        chosen = choose_schedule_candidates(items, [], now=PINNED, dedup_seconds=86400, limit=2)
        assert [item.proxy_id for item in chosen] == [3, 1]

    def test_skips_any_existing_outbox_row(self) -> None:
        items = [_item(1), _item(2, server="8.8.8.8")]
        existing = [
            ExistingPublication(
                proxy_id=1,
                status=PublicationStatus.FAILED,
                last_attempt_at=PINNED,
                created_at=PINNED,
            )
        ]
        chosen = choose_schedule_candidates(
            items, existing, now=PINNED, dedup_seconds=86400, limit=5
        )
        assert [item.proxy_id for item in chosen] == [2]

    def test_already_published_is_not_rescheduled(self) -> None:
        items = [_item(1)]
        existing = [
            ExistingPublication(
                proxy_id=1,
                status=PublicationStatus.PUBLISHED,
                last_attempt_at=PINNED - timedelta(days=30),
                created_at=PINNED - timedelta(days=30),
            )
        ]
        chosen = choose_schedule_candidates(
            items, existing, now=PINNED, dedup_seconds=3600, limit=5
        )
        assert chosen == []

    def test_recently_published_is_inside_the_dedup_window(self) -> None:
        items = [_item(1), _item(2, server="8.8.8.8")]
        existing = [
            ExistingPublication(
                proxy_id=1,
                status=PublicationStatus.PUBLISHED,
                last_attempt_at=PINNED - timedelta(seconds=10),
                created_at=PINNED - timedelta(seconds=10),
            )
        ]
        chosen = choose_schedule_candidates(items, existing, now=PINNED, dedup_seconds=60, limit=5)
        assert [item.proxy_id for item in chosen] == [2]

    def test_limit_zero_returns_empty(self) -> None:
        assert (
            choose_schedule_candidates([_item(1)], [], now=PINNED, dedup_seconds=1, limit=0) == []
        )

    def test_never_selects_independently_of_the_given_list(self) -> None:
        # An existing published proxy that is *not* in items must not be chosen.
        existing = [
            ExistingPublication(
                proxy_id=99,
                status=PublicationStatus.PUBLISHED,
                last_attempt_at=PINNED,
                created_at=PINNED,
            )
        ]
        chosen = choose_schedule_candidates(
            [_item(1)], existing, now=PINNED, dedup_seconds=86400, limit=5
        )
        assert [item.proxy_id for item in chosen] == [1]


class TestLockSql:
    def test_locks_with_for_update_skip_locked(self) -> None:
        compiled = lock_schedule_statement(CHANNEL).compile(dialect=DIALECT)
        sql = re.sub(r"\s+", " ", str(compiled))
        assert "FOR UPDATE" in sql
        assert "SKIP LOCKED" in sql
        assert "publication_schedules" in sql
        assert "channel_id" in sql


class TestSchedulerConstruction:
    def test_rejects_blank_channel(self) -> None:
        with pytest.raises(ValueError, match="channel_id"):
            PublicationScheduler(channel_id="  ")

    def test_rejects_non_positive_interval(self) -> None:
        with pytest.raises(ValueError, match="interval_seconds"):
            PublicationScheduler(channel_id=CHANNEL, interval_seconds=0)

    def test_rejects_negative_dedup(self) -> None:
        with pytest.raises(ValueError, match="dedup_seconds"):
            PublicationScheduler(channel_id=CHANNEL, dedup_seconds=-1)

    def test_rejects_non_positive_max_pending(self) -> None:
        with pytest.raises(ValueError, match="max_pending"):
            PublicationScheduler(channel_id=CHANNEL, max_pending=0)

    async def test_empty_items_does_not_query(self) -> None:
        class _Session:
            def __init__(self) -> None:
                self.statements: list[object] = []

            async def execute(self, statement: object, *_a: object, **_k: object) -> object:
                self.statements.append(statement)
                raise AssertionError("must not query")

        session = _Session()
        scheduled = await schedule_new_publications(
            session,  # type: ignore[arg-type]
            [],
            channel_id=CHANNEL,
            now=PINNED,
        )
        assert scheduled == []
        assert session.statements == []

    async def test_rejects_a_naive_clock(self) -> None:
        class _Session:
            async def execute(self, *_a: object, **_k: object) -> object:
                raise AssertionError("must not query")

        with pytest.raises(ValueError, match="timezone-aware"):
            await schedule_new_publications(
                _Session(),  # type: ignore[arg-type]
                [_item(1)],
                channel_id=CHANNEL,
                now=datetime(2026, 9, 18, 12, 0, 0),
            )
