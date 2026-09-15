"""Public Telegram channel web preview scraper.

Fetches public channel web views via ``https://t.me/s/{channel_name}`` without
requiring Telegram user credentials, API IDs, phone numbers, or sessions.
"""

from __future__ import annotations

from core.logger import get_logger
from core.models import SourceType
from modules.discovery.http import SsrfSafeHttpClient
from modules.discovery.models import DiscoveredProxyCandidate
from modules.discovery.parser import parse_proxy_text
from modules.discovery.sources.base import BaseSource

__all__ = ["TelegramWebSource"]

_logger = get_logger("modules.discovery.sources.telegram_web")


class TelegramWebSource(BaseSource):
    """Scrapes candidate proxies from public Telegram channel web previews (t.me/s/...)."""

    def __init__(self, channel_name: str) -> None:
        clean_name = channel_name.strip().lstrip("@")
        if not clean_name:
            msg = "Telegram channel name must not be empty"
            raise ValueError(msg)

        self.source_name = f"@{clean_name}"
        self.source_type = SourceType.TELEGRAM_CHANNEL
        self.source_url = f"https://t.me/s/{clean_name}"

    async def fetch_candidates(
        self,
        http_client: SsrfSafeHttpClient | None = None,
    ) -> list[DiscoveredProxyCandidate]:
        """Fetch the public channel page and extract MTProto proxy candidates."""
        client = http_client or SsrfSafeHttpClient()
        _logger.info("telegram_web_fetch_started", channel=self.source_name, url=self.source_url)

        try:
            assert self.source_url is not None
            html_content = await client.get_text(self.source_url)
        except Exception as exc:
            _logger.error(
                "telegram_web_fetch_failed",
                channel=self.source_name,
                url=self.source_url,
                error=str(exc),
            )
            return []

        proxies = parse_proxy_text(html_content)
        _logger.info(
            "telegram_web_fetch_completed",
            channel=self.source_name,
            extracted_count=len(proxies),
        )

        candidates: list[DiscoveredProxyCandidate] = []
        for p in proxies:
            candidates.append(
                DiscoveredProxyCandidate(
                    proxy=p,
                    source_type=self.source_type,
                    source_name=self.source_name,
                    source_url=self.source_url,
                    raw_reference=str(p),
                )
            )

        return candidates
