"""Unit tests for publication metrics, classification, and health SQL.

No PostgreSQL. No Telegram.
"""

from __future__ import annotations

import ast
import json
import pathlib
import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.dialects import postgresql

from core.logger import REDACTED, get_logger
from modules.publishing.health import (
    DEFAULT_HEARTBEAT_SECONDS,
    DEFAULT_HEARTBEAT_STALE_SECONDS,
    WorkerHeartbeatStatus,
    heartbeat_is_stale,
    heartbeat_state,
    list_heartbeats_statement,
    snapshot_status_statement,
    stale_sending_count_statement,
)
from modules.publishing.metrics import (
    PublicationMetrics,
    increment_counters_statement,
    load_counters_statement,
)
from modules.publishing.observe import (
    EVENT_FAILED,
    EVENT_PUBLISHED,
    EVENT_RECOVERED,
    EVENT_REJECTED,
    EVENT_RETRY,
    EVENT_SCHEDULED,
    EVENT_TELEGRAM_RATE_LIMITED,
    PublicationErrorClass,
    classify_publish_result,
)
from modules.publishing.protocol import PublishResult

PINNED = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)
CHANNEL = "@proxy_channel"
DIALECT = postgresql.dialect()
ROOT = pathlib.Path(__file__).resolve().parents[1]
PUBLISHING = ROOT / "src" / "modules" / "publishing"


def _imports(path: pathlib.Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


class TestMetrics:
    def test_starts_at_zero(self) -> None:
        metrics = PublicationMetrics()
        assert metrics.as_dict() == {
            "publications_scheduled_total": 0,
            "publications_rejected_total": 0,
            "publication_retries_total": 0,
            "publication_failures_total": 0,
            "publication_success_total": 0,
            "telegram_rate_limits_total": 0,
        }

    def test_increments_are_independent(self) -> None:
        metrics = PublicationMetrics()
        metrics.inc_scheduled(2)
        metrics.inc_rejected()
        metrics.inc_retries(3)
        metrics.inc_failures()
        metrics.inc_success(4)
        metrics.inc_rate_limits()
        assert metrics.publications_scheduled_total == 2
        assert metrics.publications_rejected_total == 1
        assert metrics.publication_retries_total == 3
        assert metrics.publication_failures_total == 1
        assert metrics.publication_success_total == 4
        assert metrics.telegram_rate_limits_total == 1

    def test_drain_does_not_reset_totals(self) -> None:
        metrics = PublicationMetrics()
        metrics.inc_success(2)
        metrics.inc_retries(1)
        assert metrics.drain_deltas() == {
            "publication_success_total": 2,
            "publication_retries_total": 1,
        }
        assert metrics.publication_success_total == 2
        assert metrics.drain_deltas() == {}
        metrics.inc_success(1)
        assert metrics.drain_deltas() == {"publication_success_total": 1}
        assert metrics.publication_success_total == 3


class TestClassification:
    def test_rate_limit_is_telegram_not_retry_policy(self) -> None:
        result = PublishResult(
            ok=False,
            telegram_message_id=None,
            error_safe="HTTP 429",
            error_code=429,
            retryable=True,
        )
        assert classify_publish_result(result) is PublicationErrorClass.TELEGRAM

    def test_retryable_non_429_is_transient(self) -> None:
        result = PublishResult(
            ok=False, telegram_message_id=None, error_safe="timeout", retryable=True
        )
        assert classify_publish_result(result) is PublicationErrorClass.TRANSIENT

    def test_permanent_is_permanent(self) -> None:
        result = PublishResult(
            ok=False,
            telegram_message_id=None,
            error_safe="HTTP 400",
            error_code=400,
            retryable=False,
        )
        assert classify_publish_result(result) is PublicationErrorClass.PERMANENT

    def test_does_not_classify_success(self) -> None:
        with pytest.raises(ValueError, match="successful"):
            classify_publish_result(PublishResult(ok=True, telegram_message_id=1))


class TestHeartbeatStale:
    def test_missing_row_is_stale(self) -> None:
        assert heartbeat_is_stale(None, now=PINNED, stale_seconds=180) is True

    def test_fresh_row_is_not_stale(self) -> None:
        seen = PINNED - timedelta(seconds=30)
        assert heartbeat_is_stale(seen, now=PINNED, stale_seconds=180) is False

    def test_old_row_is_stale_even_if_it_exists(self) -> None:
        seen = PINNED - timedelta(seconds=181)
        assert heartbeat_is_stale(seen, now=PINNED, stale_seconds=180) is True

    def test_defaults(self) -> None:
        assert DEFAULT_HEARTBEAT_SECONDS == 60.0
        assert DEFAULT_HEARTBEAT_STALE_SECONDS == 180.0

    def test_system_state_is_not_healthy_just_because_a_row_exists(self) -> None:
        assert heartbeat_state(()) == "none"
        stale = WorkerHeartbeatStatus(
            worker_id="a",
            worker_type="publishing-worker",
            last_seen_at=PINNED,
            status="stale",
        )
        healthy = WorkerHeartbeatStatus(
            worker_id="b",
            worker_type="publishing-worker",
            last_seen_at=PINNED,
            status="healthy",
        )
        assert heartbeat_state((stale,)) == "stale"
        assert heartbeat_state((stale, healthy)) == "healthy"


class TestHealthSqlIsReadOnly:
    def test_status_snapshot_is_select(self) -> None:
        sql = re.sub(
            r"\s+", " ", str(snapshot_status_statement(CHANNEL).compile(dialect=DIALECT))
        ).upper()
        assert sql.startswith("SELECT")
        assert "INSERT" not in sql
        assert "UPDATE" not in sql
        assert "DELETE" not in sql
        assert "FOR UPDATE" not in sql

    def test_stale_sending_is_select_on_lease(self) -> None:
        sql = re.sub(
            r"\s+",
            " ",
            str(stale_sending_count_statement(CHANNEL, PINNED).compile(dialect=DIALECT)),
        ).upper()
        assert "SELECT" in sql
        assert "LEASE_UNTIL" in sql
        assert "INSERT" not in sql
        assert "UPDATE" not in sql
        assert "DELETE" not in sql

    def test_heartbeats_and_counters_are_select(self) -> None:
        compiled = list_heartbeats_statement("publishing-worker").compile(dialect=DIALECT)
        hb = re.sub(r"\s+", " ", str(compiled)).upper()
        counters = re.sub(
            r"\s+", " ", str(load_counters_statement().compile(dialect=DIALECT))
        ).upper()
        for sql in (hb, counters):
            assert sql.startswith("SELECT")
            assert "INSERT" not in sql
            assert "UPDATE" not in sql
            assert "DELETE" not in sql
            assert "FOR UPDATE" not in sql

    def test_counter_upsert_is_atomic_add(self) -> None:
        statement = increment_counters_statement(
            {"publication_success_total": 2}, channel_id=CHANNEL, now=PINNED
        )
        assert statement is not None
        sql = re.sub(r"\s+", " ", str(statement.compile(dialect=DIALECT))).upper()
        assert "INSERT" in sql
        assert "ON CONFLICT" in sql
        assert "PUBLICATION_COUNTERS.VALUE" in sql or "VALUE +" in sql or "+ EXCLUDED" in sql
        assert "PROXY_PUBLICATIONS" not in sql


class TestPipelineIsolation:
    def test_health_does_not_import_claim_or_scheduler(self) -> None:
        names = _imports(PUBLISHING / "health.py")
        joined = " ".join(sorted(names))
        assert "modules.publishing.claim" not in joined
        assert "modules.publishing.scheduler" not in joined
        assert "modules.publishing.service" not in joined
        assert "telethon" not in names
        assert "httpx" not in names

    def test_claim_and_scheduler_do_not_import_observability(self) -> None:
        for path in (PUBLISHING / "claim.py", PUBLISHING / "scheduler.py"):
            names = _imports(path)
            joined = " ".join(sorted(names))
            assert "modules.publishing.health" not in joined
            assert "modules.publishing.metrics" not in joined
            assert "modules.publishing.observe" not in joined


class TestEventNames:
    def test_required_events(self) -> None:
        assert EVENT_SCHEDULED == "publication_scheduled"
        assert EVENT_REJECTED == "publication_rejected"
        assert EVENT_PUBLISHED == "publication_published"
        assert EVENT_RETRY == "publication_retry"
        assert EVENT_FAILED == "publication_failed"
        assert EVENT_RECOVERED == "publication_recovered"
        assert EVENT_TELEGRAM_RATE_LIMITED == "telegram_rate_limited"


class TestLoggingRedaction:
    def test_secret_fields_are_redacted(self, json_logs: pytest.CaptureFixture[str]) -> None:
        from core.logger import configure_logging
        from tests.conftest import make_settings

        configure_logging(make_settings(log_format="json", log_level="INFO"), force=True)
        logger = get_logger("tests.publication.observability")
        secret = "dd" + "ab" * 16
        token = "123456789:AATestTokenNotARealSecretValue"
        logger.info(
            EVENT_FAILED,
            publication_id=1,
            proxy_id=2,
            channel_id=CHANNEL,
            attempt=1,
            classification=PublicationErrorClass.TELEGRAM,
            token=token,
            secret=secret,
            url=f"tg://proxy?server=1.1.1.1&port=443&secret={secret}",
        )
        lines = [line for line in json_logs.readouterr().out.splitlines() if line.strip()]
        payload = "\n".join(lines)
        assert secret not in payload
        assert token not in payload
        assert "tg://proxy?server=1.1.1.1&port=443&secret=" not in payload or REDACTED in payload
        record = json.loads(lines[-1])
        assert record["event"] == EVENT_FAILED
        assert record["token"] == REDACTED
        assert record["secret"] == REDACTED
        assert record["classification"] == "telegram"
        assert secret not in record["url"]
