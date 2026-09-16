"""Unit tests for :mod:`modules.discovery.sources` -- Source adapters."""

from __future__ import annotations

import pytest

from core.models import SourceType
from modules.discovery.http import SsrfSafeHttpClient
from modules.discovery.sources.raw_http import RawHttpSource, RawTextSource
from modules.discovery.sources.telegram_web import TelegramWebSource


class _MockHttpClient(SsrfSafeHttpClient):
    def __init__(self, response_text: str) -> None:
        super().__init__()
        self.response_text = response_text
        self.requested_urls: list[str] = []

    async def get_text(self, url: str) -> str:
        self.requested_urls.append(url)
        return self.response_text


class _FailingHttpClient(SsrfSafeHttpClient):
    async def get_text(self, url: str) -> str:
        msg = f"Simulated network error fetching {url}"
        raise ConnectionError(msg)


class TestTelegramWebSource:
    async def test_fetches_and_extracts_candidates(self) -> None:
        html = """
        <div class="tgme_widget_message_text">
            Join our proxy:
            tg://proxy?server=1.2.3.4&port=443&secret=000102030405060708090a0b0c0d0e0f
        </div>
        """
        source = TelegramWebSource("free_proxies")
        assert source.source_name == "@free_proxies"
        assert source.source_type == SourceType.TELEGRAM_CHANNEL
        assert source.source_url == "https://t.me/s/free_proxies"

        mock_client = _MockHttpClient(html)
        candidates = await source.fetch_candidates(mock_client)

        assert len(candidates) == 1
        c = candidates[0]
        assert c.proxy.server == "1.2.3.4"
        assert c.source_name == "@free_proxies"
        assert c.source_type == SourceType.TELEGRAM_CHANNEL
        assert c.source_url == "https://t.me/s/free_proxies"
        assert mock_client.requested_urls == ["https://t.me/s/free_proxies"]

    async def test_handles_fetch_failure_gracefully(self) -> None:
        source = TelegramWebSource("dead_channel")
        failing_client = _FailingHttpClient()
        candidates = await source.fetch_candidates(failing_client)
        assert candidates == []

    def test_rejects_empty_channel_name(self) -> None:
        with pytest.raises(ValueError, match="channel name must not be empty"):
            TelegramWebSource("   ")

    @pytest.mark.parametrize("bad", ["../etc", "foo/bar", "s/admin", "ab"])
    def test_rejects_path_injection(self, bad: str) -> None:
        with pytest.raises(ValueError, match="Invalid Telegram channel"):
            TelegramWebSource(bad)


class TestRawHttpSource:
    async def test_fetches_and_extracts_from_url(self) -> None:
        text = "tg://proxy?server=8.8.8.8&port=443&secret=000102030405060708090a0b0c0d0e0f"
        source = RawHttpSource("https://example.com/proxies.txt", source_name="custom_list")
        mock_client = _MockHttpClient(text)

        candidates = await source.fetch_candidates(mock_client)
        assert len(candidates) == 1
        assert candidates[0].proxy.server == "8.8.8.8"
        assert candidates[0].source_name == "custom_list"
        assert candidates[0].source_type == SourceType.HTTP_PAGE


class TestRawTextSource:
    async def test_extracts_from_raw_string(self) -> None:
        text = """
        tg://proxy?server=1.2.3.4&port=443&secret=000102030405060708090a0b0c0d0e0f
        https://t.me/proxy?server=5.6.7.8&port=443&secret=000102030405060708090a0b0c0d0e0f
        """
        source = RawTextSource(text, source_name="manual_paste")
        candidates = await source.fetch_candidates()

        assert len(candidates) == 2
        assert candidates[0].source_type == SourceType.RAW_TEXT
        assert candidates[0].source_name == "manual_paste"
        assert candidates[0].source_url is None
