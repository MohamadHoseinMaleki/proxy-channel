"""Tests for :mod:`core.logger` -- structured output and mandatory redaction.

The redaction tests are the security-critical ones: the platform handles MTProto
secrets, Telegram bot tokens/API hashes, AI provider keys and database
passwords, and none of them may ever reach a log line.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from core.logger import (
    REDACTED,
    bind_worker_context,
    configure_logging,
    get_logger,
    redact,
    redact_secrets,
    unbind_worker_context,
)

from .conftest import make_settings

TG_LINK = "tg://proxy?server=203.0.113.7&port=443&secret=ee112233445566778899aabbccddeeff00676f6f676c652e636f6d"
RAW_SECRET = "ee112233445566778899aabbccddeeff00676f6f676c652e636f6d"
DSN = "postgresql+asyncpg://mtproto:sup3r-s3cret-pw@db.example.com:5432/mtproto"
BOT_TOKEN = "123456789:AAExampleBotTokenValueThatMustNeverBeLogged"


def parse_lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    """Parse captured stdout as a list of JSON log records."""
    out = capsys.readouterr().out
    return [json.loads(line) for line in out.splitlines() if line.strip()]


class TestRedactKeys:
    @pytest.mark.parametrize(
        "key",
        [
            "secret",
            "proxy_secret",
            "mtproto_secret",
            "token",
            "bot_token",
            "telegram_bot_token",
            "access_token",
            "api_key",
            "qwen_api_key",
            "api_hash",
            "telegram_api_hash",
            "password",
            "db_password",
            "database_url",
            "dsn",
            "authorization",
            "session_string",
            "private_key",
        ],
    )
    def test_sensitive_keys_are_masked(self, key: str) -> None:
        assert redact({key: "sensitive-value"}) == {key: REDACTED}

    @pytest.mark.parametrize(
        "key",
        [
            "event",
            "worker",
            "source",
            "error_category",
            "exception_type",
            "duration_ms",
            "proxy_id",
            "tcp_latency_ms",
            "e2e_latency_ms",
            "is_success",
            "fingerprint",
            "source_url",
            "test_location",
            "score",
            "reliability_24h_pct",
        ],
    )
    def test_operational_keys_are_preserved(self, key: str) -> None:
        """Redaction must not blind the metrics we actually need to debug with."""
        assert redact({key: "keep-me"}) == {key: "keep-me"}

    def test_nested_structures_are_walked(self) -> None:
        payload = {
            "proxy": {"server": "203.0.113.7", "secret": RAW_SECRET},
            "candidates": [{"secret": RAW_SECRET}, {"server": "198.51.100.1"}],
        }
        result = redact(payload)
        assert result["proxy"]["secret"] == REDACTED
        assert result["proxy"]["server"] == "203.0.113.7"
        assert result["candidates"][0]["secret"] == REDACTED
        assert result["candidates"][1] == {"server": "198.51.100.1"}

    def test_tuples_and_sets_keep_their_type(self) -> None:
        assert isinstance(redact((1, "a")), tuple)
        assert isinstance(redact({1, 2}), set)

    def test_input_is_not_mutated(self) -> None:
        original = {"secret": RAW_SECRET, "nested": {"token": BOT_TOKEN}}
        redact(original)
        assert original == {"secret": RAW_SECRET, "nested": {"token": BOT_TOKEN}}

    def test_deeply_nested_input_is_bounded(self) -> None:
        """A pathological payload must not blow the stack or hang."""
        payload: Any = {"server": "203.0.113.7"}
        for _ in range(200):
            payload = {"nested": payload}
        result = redact(payload)
        assert isinstance(result, dict)
        assert RAW_SECRET not in json.dumps(result, default=str)

    def test_non_string_scalars_pass_through(self) -> None:
        assert redact(42) == 42
        assert redact(None) is None
        assert redact(True) is True


class TestRedactValues:
    def test_secret_parameter_scrubbed_from_tg_link(self) -> None:
        scrubbed = redact(TG_LINK)
        assert RAW_SECRET not in scrubbed
        assert REDACTED in scrubbed
        # The non-secret parts of the link stay readable for debugging.
        assert "server=203.0.113.7" in scrubbed
        assert "port=443" in scrubbed

    def test_secret_parameter_scrubbed_from_https_link(self) -> None:
        link = f"https://t.me/proxy?server=203.0.113.7&port=443&secret={RAW_SECRET}"
        assert RAW_SECRET not in redact(link)

    def test_password_scrubbed_from_dsn_string(self) -> None:
        scrubbed = redact(f"failed to connect using {DSN}")
        assert "sup3r-s3cret-pw" not in scrubbed
        assert "db.example.com" in scrubbed

    def test_bot_token_scrubbed_from_bot_api_url(self) -> None:
        """Task 011 builds ``/bot<token>/...`` URLs; they must be safe to log."""
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        scrubbed = redact(url)
        assert BOT_TOKEN not in scrubbed
        assert "AAExampleBotTokenValueThatMustNeverBeLogged" not in scrubbed
        assert "api.telegram.org/bot123456789:" in scrubbed
        assert REDACTED in scrubbed

    def test_bot_token_scrubbed_as_structured_value(self) -> None:
        assert redact({"token": BOT_TOKEN})["token"] == REDACTED


class TestRedactProcessor:
    def test_processor_masks_whole_event(self) -> None:
        event: dict[str, Any] = {
            "event": "proxy_tested",
            "proxy_id": 7,
            "secret": RAW_SECRET,
            "link": TG_LINK,
            "is_success": True,
        }
        result = redact_secrets(None, "info", event)
        assert result["secret"] == REDACTED
        assert RAW_SECRET not in result["link"]
        assert result["event"] == "proxy_tested"
        assert result["proxy_id"] == 7
        assert result["is_success"] is True


class TestConfiguredOutput:
    def test_json_records_carry_worker_event_and_timestamp(
        self, json_logs: pytest.CaptureFixture[str]
    ) -> None:
        bind_worker_context("tester-worker")
        get_logger("workers.tester").info("proxy_tested", proxy_id=3, is_success=False)
        unbind_worker_context()

        records = parse_lines(json_logs)
        assert len(records) == 1
        record = records[0]
        assert record["event"] == "proxy_tested"
        assert record["worker"] == "tester-worker"
        assert record["proxy_id"] == 3
        assert record["is_success"] is False
        assert record["level"] == "info"
        assert "timestamp" in record
        assert record["logger"] == "workers.tester"

    def test_secrets_never_appear_in_emitted_json(
        self, json_logs: pytest.CaptureFixture[str]
    ) -> None:
        log = get_logger("workers.discovery")
        log.info(
            "discovery_completed",
            source="example-channel",
            proxies_found=124,
            new_proxies=37,
            duplicates=87,
            secret=RAW_SECRET,
            link=TG_LINK,
            database_url=DSN,
            api_hash="0123456789abcdef0123456789abcdef",
        )
        raw = json_logs.readouterr().out
        assert RAW_SECRET not in raw
        assert "sup3r-s3cret-pw" not in raw
        assert "0123456789abcdef0123456789abcdef" not in raw
        assert REDACTED in raw
        # Operational facts survive.
        assert '"proxies_found": 124' in raw
        assert "example-channel" in raw

    def test_secret_inside_exception_text_is_scrubbed(
        self, json_logs: pytest.CaptureFixture[str]
    ) -> None:
        log = get_logger("workers.tester")
        try:
            msg = f"handshake failed for {TG_LINK}"
            raise RuntimeError(msg)
        except RuntimeError as exc:
            log.error("proxy_test_failed", exc_info=exc)

        raw = json_logs.readouterr().out
        assert RAW_SECRET not in raw
        assert "RuntimeError" in raw

    def test_console_renderer_is_used_in_development(
        self, console_logs: pytest.CaptureFixture[str]
    ) -> None:
        get_logger("workers.scorer").info("scoring_completed", proxies_scored=5)
        out = console_logs.readouterr().out
        assert "scoring_completed" in out
        assert "proxies_scored=5" in out

    def test_configure_logging_is_idempotent(self) -> None:
        configure_logging(make_settings(log_format="json"), force=True)
        configure_logging()  # must be a no-op, not a re-configuration
        get_logger("x").info("still_works")

    def test_third_party_debug_noise_is_suppressed(
        self, json_logs: pytest.CaptureFixture[str]
    ) -> None:
        """Regression: asyncio's DEBUG chatter must not pollute app log streams."""
        import logging

        assert logging.getLogger("asyncio").level >= logging.WARNING
        logging.getLogger("asyncio").debug("Using selector: EpollSelector")
        get_logger("workers.tester").info("app_event")

        records = parse_lines(json_logs)
        events = [record["event"] for record in records]
        assert events == ["app_event"]

    def test_third_party_level_is_configurable(self) -> None:
        import logging

        configure_logging(make_settings(third_party_log_level="DEBUG"), force=True)
        assert logging.getLogger("telethon").level == logging.DEBUG
        configure_logging(make_settings(third_party_log_level="WARNING"), force=True)
        assert logging.getLogger("telethon").level == logging.WARNING

    def test_json_format_is_machine_readable(self, json_logs: pytest.CaptureFixture[str]) -> None:
        get_logger("workers.discovery").info("discovery_completed", proxies_found=1)
        records = parse_lines(json_logs)
        assert isinstance(records[0], dict)
