"""Unit tests for ``DISCOVERY_SOURCES`` parsing and channel-name validation."""

from __future__ import annotations

import pytest

from modules.discovery.catalog import (
    DiscoverySourceSpecError,
    parse_discovery_sources,
    validate_http_source_url,
    validate_telegram_channel,
)
from modules.discovery.sources.raw_http import RawHttpSource
from modules.discovery.sources.telegram_web import TelegramWebSource


class TestValidateTelegramChannel:
    def test_accepts_public_username(self) -> None:
        assert validate_telegram_channel("@ProxyList") == "ProxyList"

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "ab",
            "../etc",
            "foo/bar",
            "s/admin",
            "foo:bar",
            "foo\\bar",
            "12345",
            "chan nel",
        ],
    )
    def test_rejects_path_injection_and_short_names(self, bad: str) -> None:
        with pytest.raises(DiscoverySourceSpecError):
            validate_telegram_channel(bad)


class TestValidateHttpSourceUrl:
    def test_accepts_https(self) -> None:
        assert validate_http_source_url("https://example.com/list.txt").startswith("https://")

    def test_rejects_credentials(self) -> None:
        with pytest.raises(DiscoverySourceSpecError, match="credentials"):
            validate_http_source_url("https://user:pass@example.com/x")

    def test_rejects_non_http_scheme(self) -> None:
        with pytest.raises(DiscoverySourceSpecError):
            validate_http_source_url("file:///etc/passwd")


class TestParseDiscoverySources:
    def test_empty_is_idle(self) -> None:
        assert parse_discovery_sources("") == []
        assert parse_discovery_sources("  ; ; ") == []

    def test_parses_telegram_and_http(self) -> None:
        sources = parse_discovery_sources(
            "telegram:ProxyList; http:https://example.com/proxies.txt"
        )
        assert len(sources) == 2
        assert isinstance(sources[0], TelegramWebSource)
        assert sources[0].source_url == "https://t.me/s/ProxyList"
        assert isinstance(sources[1], RawHttpSource)
        assert sources[1].source_url == "https://example.com/proxies.txt"

    def test_accepts_bare_https_url(self) -> None:
        sources = parse_discovery_sources("https://example.com/list")
        assert isinstance(sources[0], RawHttpSource)

    def test_rejects_unknown_kind(self) -> None:
        with pytest.raises(DiscoverySourceSpecError, match="telegram"):
            parse_discovery_sources("ftp:example.com")
