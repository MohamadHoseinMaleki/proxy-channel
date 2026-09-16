"""Read-only HTTP transport over :class:`~modules.ranking.service.RankingService`.

This package is an adapter. It must not discover, test, score, or mutate
proxies. See ``docs/API.md`` and D-041.
"""

from __future__ import annotations

from modules.api.app import create_app

__all__ = ["create_app"]
