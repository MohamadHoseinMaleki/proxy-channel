"""Base abstraction for proxy discovery sources."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from core.models import SourceType
from modules.discovery.models import DiscoveredProxyCandidate

if TYPE_CHECKING:
    from modules.discovery.http import SsrfSafeHttpClient

__all__ = ["BaseSource"]


class BaseSource(ABC):
    """Abstract base class for all proxy discovery sources.

    Each source defines its provenance metadata (type, name, optional URL)
    and an asynchronous ``fetch_candidates`` method.
    """

    source_type: SourceType | str
    source_name: str
    source_url: str | None

    @abstractmethod
    async def fetch_candidates(
        self,
        http_client: SsrfSafeHttpClient | None = None,
    ) -> list[DiscoveredProxyCandidate]:
        """Fetch and extract proxy candidates from the underlying source."""
