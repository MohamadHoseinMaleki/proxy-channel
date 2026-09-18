"""Unit tests for publication validation and formatting. No network, no database."""

from __future__ import annotations

import ast
import pathlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from core.identity import PROTOCOL_MTPROTO, ProxySecret, compute_fingerprint
from core.models import SCORING_VERSION_V1
from modules.discovery.models import SecretType
from modules.publishing.formatter import (
    TELEGRAM_MESSAGE_MAX_LENGTH,
    PublicationFormatter,
    escape_publication_field,
    format_channel_message,
)
from modules.publishing.validation import (
    PublicationRejection,
    PublicationVerdict,
    validate_publication,
)
from modules.reporting.models import ReportItem
from modules.reporting.urls import canonical_tg_proxy_url
from modules.scoring.models import ScoreFreshness

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
DD_SECRET = "dd" + "ab" * 16
LEGACY_SECRET = "aa" * 16
EE_SECRET = "ee" + "11" * 16
PUBLISHING_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "modules" / "publishing"


def _item(
    proxy_id: int = 1,
    *,
    server: str = "1.1.1.1",
    port: int = 443,
    secret: str = DD_SECRET,
    protocol: str = PROTOCOL_MTPROTO,
    secret_type: str | None = None,
    reliability: str | None = "90.00",
    latency_p50: str | None = "2100.000",
    score: str = "85.000",
) -> ReportItem:
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
        protocol=protocol,
        secret_type=classified,
        fingerprint=compute_fingerprint(
            server=server,
            port=port if 1 <= port <= 65535 else 443,
            secret=secret,
            protocol=protocol,
        )
        if 1 <= port <= 65535
        else "f" * 64,
        score=Decimal(score),
        scoring_version=SCORING_VERSION_V1,
        reliability_24h=None if reliability is None else Decimal(reliability),
        sample_count_24h=10,
        latency_p50_ms=None if latency_p50 is None else Decimal(latency_p50),
        latency_p95_ms=Decimal("2500.000"),
        last_success_at=NOW - timedelta(hours=0.5),
        freshness=ScoreFreshness.RECENT,
        url=canonical_tg_proxy_url(
            server=server if server.strip() else "1.1.1.1",
            port=port if 1 <= port <= 65535 else 443,
            secret=secret,
        )
        if 1 <= port <= 65535 and server.strip()
        else "tg://proxy?server=x&port=1&secret=aa",
    )


class TestPurity:
    def test_formatter_and_validation_have_no_io_imports(self) -> None:
        forbidden = {
            "sqlalchemy",
            "asyncpg",
            "telethon",
            "httpx",
            "socket",
            "aiohttp",
            "requests",
        }
        for name in ("formatter.py", "validation.py", "message.py"):
            path = PUBLISHING_SRC / name
            names: set[str] = set()
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    names.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names.add(node.module.split(".")[0])
            found = forbidden & names
            assert not found, f"{name} imports {sorted(found)}"

    def test_service_still_selects_via_select_top(self) -> None:
        source = (PUBLISHING_SRC / "service.py").read_text(encoding="utf-8")
        assert "select_top" in source
        assert "score_observations" not in source
        assert "list_top" not in source
        assert "claim_due_proxies" not in source


class TestValidation:
    def test_valid_proxy(self) -> None:
        result = validate_publication(_item())
        assert result.ok is True
        assert result.verdict is PublicationVerdict.VALID
        assert result.reason is None
        assert result.url is not None
        assert result.url.startswith("tg://proxy?")

    def test_invalid_port(self) -> None:
        result = validate_publication(_item(port=0))
        assert result.ok is False
        assert result.reason == PublicationRejection.INVALID_PORT

    def test_invalid_host(self) -> None:
        result = validate_publication(_item(server="127.0.0.1"))
        assert result.reason == PublicationRejection.INVALID_HOST
        loopback = validate_publication(_item(server="not a host"))
        assert loopback.reason == PublicationRejection.INVALID_HOST

    def test_invalid_protocol(self) -> None:
        result = validate_publication(_item(protocol="socks5"))
        assert result.reason == PublicationRejection.INVALID_PROTOCOL

    def test_invalid_secret(self) -> None:
        result = validate_publication(_item(secret="not-a-real-secret-value"))
        assert result.reason == PublicationRejection.INVALID_SECRET

    def test_fake_tls(self) -> None:
        result = validate_publication(
            _item(secret=EE_SECRET, secret_type=SecretType.FAKE_TLS.value)
        )
        assert result.reason == PublicationRejection.FAKE_TLS
        assert result.url is None

    def test_labeled_fake_tls_is_rejected_even_if_bytes_are_dd(self) -> None:
        result = validate_publication(
            _item(secret=DD_SECRET, secret_type=SecretType.FAKE_TLS.value)
        )
        assert result.reason == PublicationRejection.FAKE_TLS

    def test_malformed_non_item(self) -> None:
        result = validate_publication(object())
        assert result.reason == PublicationRejection.MALFORMED_PROXY

    def test_repr_has_no_secret(self) -> None:
        rendered = repr(validate_publication(_item()))
        assert DD_SECRET not in rendered


class TestFormatter:
    def test_deterministic_and_canonical_url_is_last_line(self) -> None:
        item = _item(score="50.125", server="8.8.8.8")
        formatter = PublicationFormatter()
        first = formatter.format(item)
        second = formatter.format(item)
        assert first == second
        lines = first.splitlines()
        assert lines[0] == "MTProto proxy"
        assert lines[1] == "proxy: 8.8.8.8:443"
        assert lines[2] == "protocol: mtproto"
        assert "status: RECENT" in first
        assert "last_checked:" in first
        assert "quality: score 50.125" in first
        assert lines[-2] == ""
        assert lines[-1] == canonical_tg_proxy_url(server="8.8.8.8", port=443, secret=DD_SECRET)

    def test_does_not_invent_country(self) -> None:
        text = format_channel_message(_item())
        assert "country" not in text.lower()
        assert "location" not in text.lower()

    def test_missing_optional_quality_is_omitted(self) -> None:
        text = format_channel_message(_item(reliability=None, latency_p50=None))
        assert "reliability_24h:" not in text
        assert "latency_p50_ms:" not in text

    def test_secret_is_not_a_labeled_field(self) -> None:
        text = format_channel_message(_item())
        body, _, last = text.rpartition("\n")
        assert "secret:" not in body.lower()
        assert DD_SECRET not in body
        assert last.startswith("tg://proxy?")
        assert DD_SECRET in last

    def test_escaping_strips_markup_and_newlines(self) -> None:
        assert "\n" not in escape_publication_field("a\n*b*<script>")
        assert "*" not in escape_publication_field("*bold*")
        assert "<" not in escape_publication_field("<b>x</b>")
        formatted = format_channel_message(_item())
        assert "<" not in formatted.splitlines()[1]
        assert "*" not in formatted.split("tg://")[0]

    def test_message_length_drops_optional_never_the_url(self) -> None:
        item = _item()
        full = format_channel_message(item)
        assert len(full) <= TELEGRAM_MESSAGE_MAX_LENGTH
        tight = format_channel_message(item, limit=160)
        assert len(tight) <= 160
        assert tight.splitlines()[-1].startswith("tg://proxy?")
        assert tight.splitlines()[0] == "MTProto proxy"
        assert "reliability_24h:" not in tight or len(full) <= 160

    def test_legacy_secret_formats(self) -> None:
        text = format_channel_message(_item(secret=LEGACY_SECRET, secret_type="legacy"))
        assert text.splitlines()[-1].startswith("tg://proxy?")
        assert validate_publication(_item(secret=LEGACY_SECRET, secret_type="legacy")).ok
