"""Unit tests for Telegram channel publishing. No real network, no database."""

from __future__ import annotations

import ast
import json
import pathlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from core.identity import PROTOCOL_MTPROTO, ProxySecret, compute_fingerprint
from core.models import SCORING_VERSION_V1, PublicationStatus
from modules.discovery.models import SecretType
from modules.publishing.bot_api import (
    BOT_API_BASE,
    BotApiTelegramPublisher,
    TelegramPublishError,
)
from modules.publishing.fake import FakeTelegramPublisher
from modules.publishing.message import format_channel_message
from modules.publishing.service import PublishingService
from modules.reporting.models import Report, ReportItem
from modules.reporting.urls import canonical_tg_proxy_url
from modules.scoring.models import ScoreFreshness

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
DD_SECRET = "dd" + "ab" * 16
LEGACY_SECRET = "aa" * 16
EE_SECRET = "ee" + "11" * 16
FAKE_TOKEN = "123456789:AATestTokenNotARealSecretValue"
CHANNEL = "@proxy_channel"
PUBLISHING_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "modules" / "publishing"


def _item(
    proxy_id: int,
    *,
    score: str = "85.000",
    server: str = "1.1.1.1",
    port: int = 443,
    secret: str = DD_SECRET,
    secret_type: str | None = None,
    reliability: str | None = "90.00",
    latency_p50: str | None = "2100.000",
) -> ReportItem:
    url = canonical_tg_proxy_url(server=server, port=port, secret=secret)
    classified = secret_type
    if classified is None:
        classified = (
            SecretType.SECURE_RANDOMIZED.value
            if secret.startswith("dd")
            else SecretType.LEGACY.value
        )
    return ReportItem(
        proxy_id=proxy_id,
        server=server,
        port=port,
        secret=ProxySecret(secret),
        protocol=PROTOCOL_MTPROTO,
        secret_type=classified,
        fingerprint=compute_fingerprint(server=server, port=port, secret=secret),
        score=Decimal(score),
        scoring_version=SCORING_VERSION_V1,
        reliability_24h=None if reliability is None else Decimal(reliability),
        sample_count_24h=10,
        latency_p50_ms=None if latency_p50 is None else Decimal(latency_p50),
        latency_p95_ms=Decimal("2500.000"),
        last_success_at=NOW - timedelta(hours=0.5),
        freshness=ScoreFreshness.RECENT,
        url=url,
    )


def _report(*items: ReportItem) -> Report:
    return Report(
        items=items,
        generated_at=NOW,
        limit=20,
        max_success_age_hours=6.0,
        scoring_version=SCORING_VERSION_V1,
    )


class _Harness(PublishingService):
    """In-memory persistence so cycle tests do not need PostgreSQL."""

    def __init__(
        self,
        publisher: FakeTelegramPublisher,
        *,
        already: set[int] | None = None,
    ) -> None:
        self.publisher = publisher
        self.channel_id = CHANNEL
        self.db = None  # type: ignore[assignment]
        self.reporting = None  # type: ignore[assignment]
        self._already = set(already or ())
        self.rows: list[dict[str, Any]] = []

    async def _successful_proxy_ids(self, proxy_ids: Sequence[int]) -> set[int]:
        del proxy_ids
        return set(self._already)

    async def _record(
        self,
        *,
        proxy_id: int,
        status: PublicationStatus,
        telegram_message_id: int | None,
        error_message_safe: str | None,
    ) -> bool:
        self.rows.append(
            {
                "proxy_id": proxy_id,
                "status": status,
                "telegram_message_id": telegram_message_id,
                "error_message_safe": error_message_safe,
            }
        )
        if status is PublicationStatus.SUCCESS:
            self._already.add(proxy_id)
        return True


def _harness(
    publisher: FakeTelegramPublisher,
    *,
    already: set[int] | None = None,
) -> _Harness:
    return _Harness(publisher, already=already)


class TestPurity:
    def test_message_and_protocol_have_no_io_imports(self) -> None:
        forbidden = {
            "sqlalchemy",
            "asyncpg",
            "telethon",
            "httpx",
            "socket",
            "aiohttp",
            "requests",
        }
        for name in ("message.py", "protocol.py", "fake.py"):
            path = PUBLISHING_SRC / name
            names: set[str] = set()
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    names.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names.add(node.module.split(".")[0])
            found = forbidden & names
            assert not found, f"{name} imports {sorted(found)}"


class TestMessageFormat:
    def test_is_deterministic(self) -> None:
        item = _item(1, score="50.125", server="8.8.8.8")
        first = format_channel_message(item)
        second = format_channel_message(item)
        assert first == second
        assert first.splitlines()[0] == "MTProto proxy"
        assert "server: 8.8.8.8" in first
        assert "port: 443" in first
        assert f"secret: {DD_SECRET}" in first
        assert "score: 50.125" in first
        assert "reliability_24h: 90.00" in first
        assert "sample_count_24h: 10" in first
        assert "latency_p50_ms: 2100.000" in first
        assert "freshness: RECENT" in first
        assert "secret_type: dd" in first
        assert first.splitlines()[-1].startswith("tg://proxy?")
        assert first.splitlines()[-2] == ""

    def test_missing_optional_metrics_render_as_dash(self) -> None:
        item = _item(1, reliability=None, latency_p50=None)
        text = format_channel_message(item)
        assert "reliability_24h: -" in text
        assert "latency_p50_ms: -" in text

    def test_repr_of_item_does_not_include_secret(self) -> None:
        item = _item(1)
        assert DD_SECRET not in repr(item)
        assert DD_SECRET in format_channel_message(item)


class TestBotApiPublisher:
    async def test_send_message_posts_to_bot_api_and_returns_id(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            body = json.loads(request.content)
            assert body["chat_id"] == CHANNEL
            assert body["disable_web_page_preview"] is True
            assert "MTProto proxy" in body["text"]
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

        publisher = BotApiTelegramPublisher(
            token=SecretStr(FAKE_TOKEN),
            channel_id=CHANNEL,
            transport=httpx.MockTransport(handler),
        )
        result = await publisher.publish(format_channel_message(_item(1)))
        assert result.ok is True
        assert result.telegram_message_id == 42
        assert seen[0].url.host == "api.telegram.org"
        assert str(seen[0].url).startswith(f"{BOT_API_BASE}/bot")
        assert seen[0].url.path.endswith("/sendMessage")
        await publisher.aclose()

    async def test_telegram_error_is_not_raised(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={"ok": False, "error_code": 400, "description": "chat not found"},
            )

        publisher = BotApiTelegramPublisher(
            token=FAKE_TOKEN,
            channel_id=CHANNEL,
            transport=httpx.MockTransport(handler),
        )
        result = await publisher.publish("hello")
        assert result.ok is False
        assert result.telegram_message_id is None
        assert result.error_safe is not None
        assert "400" in result.error_safe
        await publisher.aclose()

    async def test_transport_error_is_recorded(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        publisher = BotApiTelegramPublisher(
            token=FAKE_TOKEN,
            channel_id=CHANNEL,
            transport=httpx.MockTransport(handler),
        )
        result = await publisher.publish("hello")
        assert result.ok is False
        assert result.telegram_message_id is None
        await publisher.aclose()

    async def test_empty_message_is_rejected_without_http(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise AssertionError("must not call Telegram")

        publisher = BotApiTelegramPublisher(
            token=FAKE_TOKEN,
            channel_id=CHANNEL,
            transport=httpx.MockTransport(handler),
        )
        result = await publisher.publish("  ")
        assert result.ok is False
        assert result.error_safe == "empty_message"
        await publisher.aclose()

    def test_rejects_malformed_token_and_empty_channel(self) -> None:
        with pytest.raises(TelegramPublishError, match="token"):
            BotApiTelegramPublisher(token="not-a-token", channel_id=CHANNEL)
        with pytest.raises(TelegramPublishError, match="channel"):
            BotApiTelegramPublisher(token=FAKE_TOKEN, channel_id="  ")


class TestPublishCycle:
    async def test_selected_proxies_are_published(self) -> None:
        publisher = FakeTelegramPublisher()
        service = _harness(publisher)
        result = await service.publish_report(_report(_item(1), _item(2, server="1.0.0.1")))
        assert result.published == 2
        assert result.failed == 0
        assert result.skipped == 0
        assert len(publisher.messages) == 2
        assert [row["telegram_message_id"] for row in service.rows] == [1, 2]
        assert all(row["status"] is PublicationStatus.SUCCESS for row in service.rows)

    async def test_duplicate_success_is_not_resent(self) -> None:
        publisher = FakeTelegramPublisher()
        service = _harness(publisher, already={1})
        result = await service.publish_report(_report(_item(1), _item(2, server="1.0.0.1")))
        assert result.published == 1
        assert result.skipped == 1
        assert len(publisher.messages) == 1
        assert "1.0.0.1" in publisher.messages[0]

    async def test_second_cycle_does_not_duplicate(self) -> None:
        publisher = FakeTelegramPublisher()
        service = _harness(publisher)
        report = _report(_item(1))
        first = await service.publish_report(report)
        second = await service.publish_report(report)
        assert first.published == 1
        assert second.published == 0
        assert second.skipped == 1
        assert len(publisher.messages) == 1

    async def test_fake_tls_is_not_posted(self) -> None:
        publisher = FakeTelegramPublisher()
        service = _harness(publisher)
        result = await service.publish_report(
            _report(_item(1, secret=EE_SECRET, secret_type=SecretType.FAKE_TLS.value))
        )
        assert result.published == 0
        assert result.skipped == 1
        assert publisher.messages == []
        assert service.rows == []

    async def test_telegram_failure_is_recorded_and_batch_continues(self) -> None:
        publisher = FakeTelegramPublisher(fail_on_index=(0,))
        service = _harness(publisher)
        result = await service.publish_report(
            _report(_item(1), _item(2, server="1.0.0.1", secret=LEGACY_SECRET))
        )
        assert result.published == 1
        assert result.failed == 1
        assert len(publisher.messages) == 2
        assert service.rows[0]["status"] is PublicationStatus.FAILURE
        assert service.rows[0]["telegram_message_id"] is None
        assert service.rows[0]["error_message_safe"] == "telegram_unavailable"
        assert service.rows[1]["status"] is PublicationStatus.SUCCESS
        assert service.rows[1]["telegram_message_id"] == 1

    async def test_raised_transport_error_does_not_stop_the_batch(self) -> None:
        publisher = FakeTelegramPublisher(raise_on_index=(0,))
        service = _harness(publisher)
        result = await service.publish_report(
            _report(_item(1), _item(2, server="1.0.0.1", secret=LEGACY_SECRET))
        )
        assert result.published == 1
        assert result.failed == 1
        assert service.rows[0]["status"] is PublicationStatus.FAILURE
        assert service.rows[1]["telegram_message_id"] == 1

    async def test_failed_attempt_can_be_retried_later(self) -> None:
        failing = FakeTelegramPublisher(fail_on_index=(0,))
        service = _harness(failing)
        first = await service.publish_report(_report(_item(1)))
        assert first.failed == 1
        service.publisher = FakeTelegramPublisher()
        second = await service.publish_report(_report(_item(1)))
        assert second.published == 1
        assert len(service.publisher.messages) == 1

    async def test_empty_report_is_a_no_op(self) -> None:
        publisher = FakeTelegramPublisher()
        service = _harness(publisher)
        result = await service.publish_report(_report())
        assert result.selected == 0
        assert result.published == 0
        assert publisher.messages == []

    async def test_logs_do_not_include_secrets(self, json_logs: pytest.CaptureFixture[str]) -> None:
        publisher = FakeTelegramPublisher()
        service = _harness(publisher)
        result = await service.publish_report(_report(_item(1)))
        output = json_logs.readouterr().out + repr(result) + repr(service.rows)
        assert DD_SECRET not in output
        assert "tg://proxy" not in output
        assert FAKE_TOKEN not in output
