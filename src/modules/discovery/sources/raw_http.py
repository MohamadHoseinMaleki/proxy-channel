"""Raw HTTP page and raw text proxy sources."""

from __future__ import annotations

from core.logger import get_logger, safe_error_message, scrub_secrets
from core.models import SourceType
from modules.discovery.http import SsrfSafeHttpClient
from modules.discovery.models import DiscoveredProxyCandidate
from modules.discovery.parser import parse_proxy_text
from modules.discovery.sources.base import BaseSource

__all__ = ["RawHttpSource", "RawTextSource"]

_logger = get_logger("modules.discovery.sources.raw_http")


class RawHttpSource(BaseSource):
    """Fetches candidate proxies from an arbitrary public HTTP/HTTPS page or paste."""

    def __init__(self, url: str, *, source_name: str | None = None) -> None:
        self.source_url = url.strip()
        self.source_name = source_name or self.source_url
        self.source_type = SourceType.HTTP_PAGE

    async def fetch_candidates(
        self,
        http_client: SsrfSafeHttpClient | None = None,
    ) -> list[DiscoveredProxyCandidate]:
        client = http_client or SsrfSafeHttpClient()
        source_url = self.source_url or ""
        _logger.info(
            "raw_http_fetch_started",
            source_name=self.source_name,
            url=scrub_secrets(source_url),
        )

        try:
            assert self.source_url is not None
            content = await client.get_text(self.source_url)
        except Exception as exc:
            _logger.error(
                "raw_http_fetch_failed",
                source_name=self.source_name,
                error=safe_error_message(exc),
            )
            return []

        proxies = parse_proxy_text(content)
        _logger.info(
            "raw_http_fetch_completed",
            source_name=self.source_name,
            extracted_count=len(proxies),
        )

        return [
            DiscoveredProxyCandidate(
                proxy=p,
                source_type=self.source_type,
                source_name=self.source_name,
                source_url=self.source_url,
                raw_reference=str(p),
            )
            for p in proxies
        ]


class RawTextSource(BaseSource):
    """Parses candidate proxies from an in-memory string (e.g. manual import)."""

    def __init__(self, text: str, *, source_name: str = "manual_import") -> None:
        self.text = text
        self.source_name = source_name
        self.source_type = SourceType.RAW_TEXT
        self.source_url = None

    async def fetch_candidates(
        self,
        http_client: SsrfSafeHttpClient | None = None,
    ) -> list[DiscoveredProxyCandidate]:
        del http_client  # No HTTP required for in-memory text
        proxies = parse_proxy_text(self.text)
        return [
            DiscoveredProxyCandidate(
                proxy=p,
                source_type=self.source_type,
                source_name=self.source_name,
                source_url=None,
                raw_reference=str(p),
            )
            for p in proxies
        ]
