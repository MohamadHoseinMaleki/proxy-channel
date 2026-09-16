"""``discovery-worker`` -- Process A.

Responsibility: fetch configured sources, parse MTProto proxy links, and upsert
canonical ``Proxy`` rows plus append-only ``ProxyDiscovery`` provenance.

HTTP runs **outside** any database transaction. Rediscovery never resets tester
scheduling columns. One bad source cannot kill the loop.

Run with::

    uv run mtproto-discovery
    # or
    uv run python -m workers.discovery
"""

from __future__ import annotations

import time

from core.database import Database
from core.lifecycle import WorkerLifecycle, worker_main
from core.logger import safe_error_message
from modules.discovery.catalog import DiscoverySourceSpecError, parse_discovery_sources
from modules.discovery.http import SsrfSafeHttpClient
from modules.discovery.service import DiscoveryService
from modules.discovery.sources.base import BaseSource

__all__ = ["WORKER_NAME", "main", "tick"]

WORKER_NAME = "discovery-worker"


def _load_sources(raw: str, life: WorkerLifecycle) -> list[BaseSource]:
    """Parse ``DISCOVERY_SOURCES``; skip invalid entries instead of aborting."""
    sources: list[BaseSource] = []
    for piece in (raw or "").split(";"):
        entry = piece.strip()
        if not entry:
            continue
        try:
            sources.extend(parse_discovery_sources(entry))
        except DiscoverySourceSpecError as exc:
            life.logger.error(
                "discovery_source_invalid",
                error=safe_error_message(exc),
            )
    return sources


async def tick(life: WorkerLifecycle, *, db: Database | None = None) -> None:
    """One discovery iteration: fetch sources, persist candidates."""
    started = time.monotonic()
    dispose_db = False
    http: SsrfSafeHttpClient | None = None

    if db is None:
        db = Database.from_settings(life.settings)
        dispose_db = True

    try:
        if not await db.is_reachable():
            life.logger.warning(
                "discovery_tick_db_unreachable",
                worker=WORKER_NAME,
                duration_ms=round((time.monotonic() - started) * 1000, 3),
            )
            return

        sources = _load_sources(life.settings.discovery_sources, life)
        if not sources:
            life.logger.info(
                "discovery_tick",
                implemented=True,
                sources_processed=0,
                source_failures=0,
                proxies_found=0,
                new_proxies=0,
                duplicates=0,
                discoveries=0,
                duration_ms=round((time.monotonic() - started) * 1000, 3),
            )
            return

        http = SsrfSafeHttpClient(
            timeout_seconds=life.settings.discovery_timeout_seconds,
            connect_timeout_seconds=life.settings.discovery_connect_timeout_seconds,
            max_response_bytes=life.settings.discovery_max_response_bytes,
            max_redirects=life.settings.discovery_max_redirects,
        )
        async with http:
            result = await DiscoveryService(db).harvest(
                sources,
                http_client=http,
                concurrency=life.settings.discovery_concurrency,
            )

        life.logger.info(
            "discovery_tick",
            implemented=True,
            sources_processed=result.sources_attempted,
            source_failures=result.source_failures,
            proxies_found=result.total_candidates,
            new_proxies=result.new_proxies,
            duplicates=result.updated_proxies,
            discoveries=result.discoveries_recorded,
            duration_ms=round((time.monotonic() - started) * 1000, 3),
        )
    finally:
        if http is not None:
            await http.aclose()
        if dispose_db:
            await db.dispose()


def main() -> None:
    """Console-script entrypoint for the discovery worker process."""
    worker_main(WORKER_NAME, tick)


if __name__ == "__main__":
    main()
