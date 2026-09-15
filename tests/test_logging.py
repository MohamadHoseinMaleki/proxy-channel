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
    DEFAULT_ERROR_MESSAGE_LIMIT,
    REDACTED,
    bind_worker_context,
    configure_logging,
    get_logger,
    redact,
    redact_secrets,
    safe_error_message,
    scrub_secrets,
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


class TestScrubSecrets:
    """String-level scrubbing.

    Shared with :func:`safe_error_message` on purpose: the same patterns protect
    log output and the ``error_message_safe`` column, so a fix in one place
    cannot leak through the other.
    """

    def test_masks_a_dsn_password(self) -> None:
        text = "postgresql+asyncpg://mtproto:sup3r-s3cret@db.example.com/mtproto"
        scrubbed = scrub_secrets(text)
        assert "sup3r-s3cret" not in scrubbed
        assert REDACTED in scrubbed

    def test_keeps_the_non_secret_parts_of_a_dsn(self) -> None:
        text = "postgresql+asyncpg://mtproto:sup3r-s3cret@db.example.com:5432/mtproto"
        scrubbed = scrub_secrets(text)
        for keep in ("db.example.com", "5432", "mtproto"):
            assert keep in scrubbed

    def test_masks_a_url_of_any_scheme(self) -> None:
        # Not just postgresql:// -- a misconfigured DSN of any kind lands in an
        # error message.
        for text in (
            "mysql+asyncmy://u:sup3r-s3cret@h/db",
            "http://u:sup3r-s3cret@proxy.example.com:8080",
            "redis://u:sup3r-s3cret@h:6379/0",
        ):
            assert "sup3r-s3cret" not in scrub_secrets(text), text

    def test_masks_an_embedded_mtproto_secret_parameter(self) -> None:
        # tg://proxy links carry the secret as a query parameter; discovery logs
        # the raw channel message, so this path is reachable.
        secret = "ee" + "a1" * 15
        text = f"tg://proxy?server=1.2.3.4&port=443&secret={secret}"
        scrubbed = scrub_secrets(text)
        assert secret not in scrubbed
        assert "server=1.2.3.4" in scrubbed

    @pytest.mark.parametrize("prefix", ["secret=", "password=", "token=", "api_key=", "api_hash="])
    def test_masks_known_embedded_keys(self, prefix: str) -> None:
        assert "hunter2" not in scrub_secrets(f"{prefix}hunter2 trailing")

    def test_masks_a_bot_token_in_a_url_path(self) -> None:
        token = "A" * 35
        text = f"https://api.telegram.org/bot123456:{token}/getMe"
        assert token not in scrub_secrets(text)

    def test_is_idempotent(self) -> None:
        text = "postgresql://u:sup3r-s3cret@h/db"
        assert scrub_secrets(scrub_secrets(text)) == scrub_secrets(text)

    def test_leaves_ordinary_text_alone(self) -> None:
        for text in (
            "connected to proxy.example.com:443",
            "error_category=DNS_ERROR",
            "claim_due_proxies returned 12 rows",
            "",
        ):
            assert scrub_secrets(text) == text

    def test_does_not_mask_a_column_named_source_url(self) -> None:
        # `url` is not in the sensitive-key list; `source_url` is provenance, not
        # a credential, and masking it would destroy debugging value.
        assert scrub_secrets("source_url=https://t.me/some_channel") == (
            "source_url=https://t.me/some_channel"
        )


class TestSafeErrorMessage:
    """The value persisted into ``proxy_observations.error_message_safe``."""

    def test_none_passes_through(self) -> None:
        assert safe_error_message(None) is None

    def test_exception_becomes_type_and_message(self) -> None:
        assert safe_error_message(TimeoutError("handshake timed out")) == (
            "TimeoutError: handshake timed out"
        )

    def test_plain_string_is_preserved(self) -> None:
        assert safe_error_message("something failed") == "something failed"

    def test_empty_string_becomes_none(self) -> None:
        assert safe_error_message("") is None
        assert safe_error_message("   ") is None

    def test_an_exception_with_no_message_keeps_its_type(self) -> None:
        # The type name is the only diagnostic available for a bare `raise
        # TimeoutError`, so dropping it would lose real information -- but the
        # dangling colon from a naive f-string must not survive.
        assert safe_error_message(ValueError()) == "ValueError"
        assert safe_error_message(TimeoutError()) == "TimeoutError"
        rendered = safe_error_message(ValueError())
        assert rendered is not None
        assert not rendered.endswith(":")

    def test_scrubs_a_secret_out_of_an_exception_message(self) -> None:
        secret = "ee" + "a1" * 15
        message = safe_error_message(RuntimeError(f"failed to connect with secret={secret}"))
        assert message is not None
        assert secret not in message

    def test_scrubs_a_dsn_out_of_an_exception_message(self) -> None:
        message = safe_error_message(
            OSError("could not connect to postgresql://u:sup3r-s3cret@h/db")
        )
        assert message is not None
        assert "sup3r-s3cret" not in message

    def test_collapses_newlines_so_a_traceback_stays_one_line(self) -> None:
        message = safe_error_message(RuntimeError("line one\nline two\n\nline three"))
        assert message == "RuntimeError: line one line two line three"
        assert "\n" not in message

    def test_respects_the_default_limit(self) -> None:
        message = safe_error_message(RuntimeError("x" * 5000))
        assert message is not None
        assert len(message) <= DEFAULT_ERROR_MESSAGE_LIMIT

    def test_respects_an_explicit_limit(self) -> None:
        message = safe_error_message("y" * 500, limit=40)
        assert message is not None
        assert len(message) <= 40

    def test_truncation_is_marked(self) -> None:
        message = safe_error_message("z" * 5000, limit=40)
        assert message is not None
        assert message.endswith("\u2026")

    def test_short_messages_are_not_truncated(self) -> None:
        assert safe_error_message("fine", limit=100) == "fine"

    def test_default_limit_matches_the_database_check_constraint(self) -> None:
        # core.models mirrors this constant into a CHECK constraint; if they
        # diverge the database starts rejecting rows the app thought were valid.
        from core.models import ERROR_MESSAGE_MAX_LENGTH

        assert DEFAULT_ERROR_MESSAGE_LIMIT == ERROR_MESSAGE_MAX_LENGTH

    def test_exception_type_is_preserved_for_subclasses(self) -> None:
        class CustomMessageError(RuntimeError):
            pass

        message = safe_error_message(CustomMessageError("nope"))
        assert message is not None
        assert message.startswith("CustomMessageError:")

    def test_base_exceptions_are_handled(self) -> None:
        # CancelledError is a BaseException, not an Exception; a worker shutdown
        # can reach here with one, and `except Exception` handling would miss it.
        import asyncio

        assert safe_error_message(asyncio.CancelledError()) == "CancelledError"
        assert safe_error_message(KeyboardInterrupt()) == "KeyboardInterrupt"


class TestBareHexScrubbing:
    """Secrets that arrive with no key in front of them.

    When a CHECK constraint rejects a row, PostgreSQL appends
    ``DETAIL: Failing row contains (...)`` and echoes every column, so a stored
    MTProto secret reaches an exception message bare. Key-based patterns cannot
    match that; this class pins the shape-based fallback that can.
    """

    def test_masks_a_bare_mtproto_secret(self) -> None:
        secret = "ee" + "a1" * 15  # 32 hex chars, the canonical 16-byte form
        assert secret not in scrub_secrets(secret)
        assert scrub_secrets(secret) == REDACTED

    def test_masks_a_longer_fake_tls_secret(self) -> None:
        secret = "ee" + "b2" * 15 + "676f6f676c652e636f6d"
        assert secret not in scrub_secrets(secret)

    def test_masks_uppercase_hex(self) -> None:
        secret = ("EE" + "A1" * 15).upper()
        assert secret not in scrub_secrets(secret)

    def test_masks_a_fingerprint_too(self) -> None:
        # The documented cost of the pattern: 64-char fingerprints are masked in
        # error text as well. Accepted -- a fingerprint is derivable from the row
        # and rarely belongs in an error message; a secret leak is not recoverable.
        assert "f" * 64 not in scrub_secrets("f" * 64)

    @pytest.mark.parametrize("length", [1, 8, 16, 30, 31])
    def test_leaves_short_hex_alone(self, length: int) -> None:
        # Ports, ids, latency values and short hashes stay readable; masking them
        # would gut ordinary debugging output.
        value = "a" * length
        assert scrub_secrets(value) == value

    def test_thirty_two_characters_is_the_boundary(self) -> None:
        assert scrub_secrets("a" * 31) == "a" * 31
        assert "a" * 32 not in scrub_secrets("a" * 32)

    def test_masks_inside_a_postgres_check_violation_detail(self) -> None:
        # Shaped like real server output, verified against PostgreSQL 16.
        secret = "ee" + "a1" * 15
        raw = (
            'new row for relation "proxies" violates check constraint '
            '"ck_proxies_port_range" DETAIL: Failing row contains '
            f"(1, mtproto, proxy.example.com, 0, {secret}, {'b' * 64}, t)."
        )
        scrubbed = scrub_secrets(raw)
        assert secret not in scrubbed
        assert "b" * 64 not in scrubbed
        # The diagnostic parts survive, so the message is still actionable.
        assert "ck_proxies_port_range" in scrubbed
        assert "proxy.example.com" in scrubbed

    def test_masks_every_secret_in_a_message_not_just_the_first(self) -> None:
        first, second = "ee" + "a1" * 15, "ee" + "c3" * 15
        scrubbed = scrub_secrets(f"tried {first} then {second}")
        assert first not in scrubbed
        assert second not in scrubbed

    def test_does_not_corrupt_the_redaction_marker(self) -> None:
        # The marker contains no 32+ hex run, so ordering the hex pass last
        # cannot double-substitute or shred earlier replacements.
        once = scrub_secrets("secret=" + "ee" + "a1" * 15)
        assert scrub_secrets(once) == once
        assert REDACTED in once

    def test_a_hex_run_embedded_in_a_longer_word_is_not_split(self) -> None:
        # \b boundaries mean alphanumerics either side keep it out of scope,
        # which stops mangling ordinary identifiers.
        assert scrub_secrets("prefix_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa_suffix") == (
            "prefix_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa_suffix"
        )
