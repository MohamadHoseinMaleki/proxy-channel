"""Unit tests for Telegram channel publishing. No real network, no database."""

from __future__ import annotations

import ast
import json
import pathlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr

from core.identity import PROTOCOL_MTPROTO, ProxySecret, compute_fingerprint
from core.models import SCORING_VERSION_V1
from modules.discovery.models import SecretType
from modules.publishing.backoff import retry_delay_seconds
from modules.publishing.bot_api import (
    BOT_API_BASE,
    BotApiTelegramPublisher,
    TelegramPublishError,
    is_retryable_status,
)
from modules.publishing.message import format_channel_message
from modules.publishing.protocol import PublishResult
from modules.reporting.models import ReportItem
from modules.reporting.urls import canonical_tg_proxy_url
from modules.scoring.models import ScoreFreshness

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
DD_SECRET = "dd" + "ab" * 16
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
        for name in ("message.py", "protocol.py", "fake.py", "backoff.py"):
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
        assert "proxy: 8.8.8.8:443" in first
        assert first.splitlines()[-1].startswith("tg://proxy?")

    def test_missing_optional_metrics_are_omitted(self) -> None:
        item = _item(1, reliability=None, latency_p50=None)
        text = format_channel_message(item)
        assert "reliability_24h:" not in text
        assert "latency_p50_ms:" not in text
        assert "quality: score 85.000" in text

    def test_secret_is_only_in_the_canonical_url(self) -> None:
        item = _item(1)
        assert DD_SECRET not in repr(item)
        text = format_channel_message(item)
        body, _, last = text.rpartition("\n")
        assert DD_SECRET not in body
        assert last.startswith("tg://proxy?")
        assert DD_SECRET in last


class TestBackoff:
    def test_is_exponential_and_capped(self) -> None:
        assert retry_delay_seconds(attempt=1, base_seconds=2, max_seconds=100) == 2
        assert retry_delay_seconds(attempt=2, base_seconds=2, max_seconds=100) == 4
        assert retry_delay_seconds(attempt=3, base_seconds=2, max_seconds=100) == 8
        assert retry_delay_seconds(attempt=10, base_seconds=2, max_seconds=30) == 30

    def test_retry_after_wins_when_larger(self) -> None:
        assert retry_delay_seconds(attempt=1, base_seconds=2, max_seconds=100, retry_after=12) == 12
        assert retry_delay_seconds(attempt=5, base_seconds=2, max_seconds=100, retry_after=3) == 32

    def test_does_not_sleep(self) -> None:
        # The function is pure: calling it 100 times is still instant.
        delays = [
            retry_delay_seconds(attempt=i, base_seconds=1, max_seconds=8) for i in range(1, 8)
        ]
        assert delays == [1, 2, 4, 8, 8, 8, 8]


class TestRetryableStatus:
    @pytest.mark.parametrize("code", [None, 429, 500, 502, 503])
    def test_transient(self, code: int | None) -> None:
        assert is_retryable_status(code) is True

    @pytest.mark.parametrize("code", [400, 401, 403, 404])
    def test_permanent(self, code: int) -> None:
        assert is_retryable_status(code) is False


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

    async def test_telegram_400_is_permanent(self) -> None:
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
        assert result.retryable is False
        assert result.error_code == 400
        assert result.error_safe is not None
        assert "400" in result.error_safe
        await publisher.aclose()

    async def test_http_429_is_retryable_and_honours_retry_after(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                429,
                json={
                    "ok": False,
                    "error_code": 429,
                    "description": "Too Many Requests: retry after 12",
                    "parameters": {"retry_after": 12},
                },
            )

        publisher = BotApiTelegramPublisher(
            token=FAKE_TOKEN,
            channel_id=CHANNEL,
            transport=httpx.MockTransport(handler),
        )
        result = await publisher.publish("hello")
        assert result.ok is False
        assert result.retryable is True
        assert result.error_code == 429
        assert result.retry_after == 12
        await publisher.aclose()

    async def test_retry_after_header_is_used(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                429,
                headers={"Retry-After": "7"},
                json={"ok": False, "error_code": 429, "description": "flood"},
            )

        publisher = BotApiTelegramPublisher(
            token=FAKE_TOKEN,
            channel_id=CHANNEL,
            transport=httpx.MockTransport(handler),
        )
        result = await publisher.publish("hello")
        assert result.retry_after == 7
        await publisher.aclose()

    async def test_http_5xx_is_retryable(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(502, json={"ok": False, "error_code": 502, "description": "bad"})

        publisher = BotApiTelegramPublisher(
            token=FAKE_TOKEN,
            channel_id=CHANNEL,
            transport=httpx.MockTransport(handler),
        )
        result = await publisher.publish("hello")
        assert result.retryable is True
        assert result.error_code == 502
        await publisher.aclose()

    async def test_transport_error_is_retryable(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        publisher = BotApiTelegramPublisher(
            token=FAKE_TOKEN,
            channel_id=CHANNEL,
            transport=httpx.MockTransport(handler),
        )
        result = await publisher.publish("hello")
        assert result.ok is False
        assert result.retryable is True
        assert result.telegram_message_id is None
        await publisher.aclose()

    async def test_timeout_is_retryable(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow")

        publisher = BotApiTelegramPublisher(
            token=FAKE_TOKEN,
            channel_id=CHANNEL,
            transport=httpx.MockTransport(handler),
        )
        result = await publisher.publish("hello")
        assert result.retryable is True
        await publisher.aclose()

    async def test_malformed_json_does_not_crash(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not-json")

        publisher = BotApiTelegramPublisher(
            token=FAKE_TOKEN,
            channel_id=CHANNEL,
            transport=httpx.MockTransport(handler),
        )
        result = await publisher.publish("hello")
        assert result.ok is False
        assert result.retryable is True
        assert result.error_safe is not None
        assert "invalid JSON" in result.error_safe
        await publisher.aclose()

    async def test_malformed_ok_payload_does_not_crash(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True, "result": "nope"})

        publisher = BotApiTelegramPublisher(
            token=FAKE_TOKEN,
            channel_id=CHANNEL,
            transport=httpx.MockTransport(handler),
        )
        result = await publisher.publish("hello")
        assert result.ok is False
        assert result.retryable is True
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
        assert result.retryable is False
        assert result.error_safe == "empty_message"
        await publisher.aclose()

    def test_rejects_malformed_token_and_empty_channel(self) -> None:
        with pytest.raises(TelegramPublishError, match="token"):
            BotApiTelegramPublisher(token="not-a-token", channel_id=CHANNEL)
        with pytest.raises(TelegramPublishError, match="channel"):
            BotApiTelegramPublisher(token=FAKE_TOKEN, channel_id="  ")

    def test_publish_result_repr_has_no_message_body(self) -> None:
        result = PublishResult(ok=True, telegram_message_id=1)
        assert DD_SECRET not in repr(result)
        assert FAKE_TOKEN not in repr(result)
