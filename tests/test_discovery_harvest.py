"""Unit tests for source isolation, cancellation, and harvest fail-safe."""

from __future__ import annotations

import asyncio

import pytest

from core.models import SourceType
from modules.discovery.http import SsrfSafeHttpClient
from modules.discovery.models import DiscoveredProxyCandidate
from modules.discovery.service import DiscoveryBatchResult, DiscoveryService
from modules.discovery.sources.base import BaseSource
from modules.discovery.sources.raw_http import RawTextSource


class _BoomSource(BaseSource):
    source_type = SourceType.RAW_TEXT
    source_name = "boom"
    source_url = None

    async def fetch_candidates(
        self, http_client: SsrfSafeHttpClient | None = None
    ) -> list[DiscoveredProxyCandidate]:
        del http_client
        msg = "source exploded"
        raise RuntimeError(msg)


class _HangSource(BaseSource):
    source_type = SourceType.RAW_TEXT
    source_name = "hang"
    source_url = None

    def __init__(self, started: asyncio.Event) -> None:
        self.started = started

    async def fetch_candidates(
        self, http_client: SsrfSafeHttpClient | None = None
    ) -> list[DiscoveredProxyCandidate]:
        del http_client
        self.started.set()
        await asyncio.sleep(30)
        return []


class TestHarvestIsolation:
    async def test_one_failing_source_does_not_drop_the_others(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        text = "tg://proxy?server=1.2.3.4&port=443&secret=000102030405060708090a0b0c0d0e0f"
        good = RawTextSource(text, source_name="good")
        saved: list[DiscoveredProxyCandidate] = []

        async def fake_save(
            self: DiscoveryService,
            candidates: list[DiscoveredProxyCandidate],
            *,
            now: object | None = None,
        ) -> DiscoveryBatchResult:
            del self, now
            saved.extend(candidates)
            return DiscoveryBatchResult(
                total_candidates=len(candidates),
                new_proxies=len(candidates),
                updated_proxies=0,
                discoveries_recorded=len(candidates),
            )

        monkeypatch.setattr(DiscoveryService, "save_candidates", fake_save)
        service = DiscoveryService(db=None)  # type: ignore[arg-type]
        result = await service.harvest(
            [_BoomSource(), good],
            http_client=SsrfSafeHttpClient(),
            concurrency=2,
        )
        assert result.source_failures == 1
        assert result.sources_attempted == 2
        assert result.total_candidates == 1
        assert len(saved) == 1
        assert saved[0].proxy.server == "1.2.3.4"

    async def test_cancel_propagates(self) -> None:
        started = asyncio.Event()
        service = DiscoveryService(db=None)  # type: ignore[arg-type]
        task = asyncio.create_task(
            service.harvest(
                [_HangSource(started)],
                http_client=SsrfSafeHttpClient(),
                concurrency=1,
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
