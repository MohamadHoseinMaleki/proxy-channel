"""Unit tests for TesterService orchestration and concurrency."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.identity import ProxySecret
from core.models import ErrorCategory, Proxy
from modules.tester.models import TesterResult, TransportType
from modules.tester.service import TesterService

# Prevent pytest from treating these as test suites
TesterService.__test__ = False  # type: ignore[attr-defined]
TesterResult.__test__ = False  # type: ignore[attr-defined]


class TestTesterServiceUnit:
    @pytest.mark.asyncio
    async def test_concurrency_bounded_by_semaphore(self) -> None:
        mock_db = MagicMock()
        service = TesterService(mock_db, concurrency=2)

        active = 0
        peak = 0

        async def fake_probe(*_args: object, **_kwargs: object) -> TesterResult:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            return TesterResult(
                proxy_id=1,
                transport_type=TransportType.RANDOMIZED_INTERMEDIATE,
                success=True,
            )

        proxy = Proxy(
            id=1,
            server="1.1.1.1",
            port=443,
            secret=ProxySecret("dd" + "11" * 16),
            fingerprint="f" * 64,
        )

        with patch("modules.tester.service.probe_proxy", side_effect=fake_probe):
            tasks = [service.test_proxy(proxy) for _ in range(6)]
            results = await asyncio.gather(*tasks)

        assert len(results) == 6
        assert peak <= 2

    @pytest.mark.asyncio
    async def test_run_batch_empty_when_no_proxies_claimed(self) -> None:
        mock_db = MagicMock()
        mock_session = AsyncMock()

        class DummyScope:
            async def __aenter__(self) -> AsyncMock:
                return mock_session

            async def __aexit__(self, *args: object) -> None:
                pass

        mock_db.session_scope.return_value = DummyScope()

        service = TesterService(mock_db)

        with patch("modules.tester.service.claim_due_proxies", return_value=[]):
            results = await service.run_batch()
            assert results == []

    @pytest.mark.asyncio
    async def test_run_batch_executes_probing_outside_transaction(self) -> None:
        mock_db = MagicMock()
        mock_session = AsyncMock()
        in_tx = False

        class DummyScope:
            async def __aenter__(self) -> AsyncMock:
                nonlocal in_tx
                in_tx = True
                return mock_session

            async def __aexit__(self, *args: object) -> None:
                nonlocal in_tx
                in_tx = False

        mock_db.session_scope.side_effect = lambda: DummyScope()

        service = TesterService(mock_db)

        proxy = Proxy(
            id=42,
            server="1.1.1.1",
            port=443,
            secret=ProxySecret("dd" + "11" * 16),
            fingerprint="f" * 64,
        )

        async def fake_probe(*_args: object, **_kwargs: object) -> TesterResult:
            # Architectural invariant: probe must NOT run inside DB transaction
            assert not in_tx, "probe_proxy was called while inside a database transaction!"
            return TesterResult(
                proxy_id=42,
                transport_type=TransportType.RANDOMIZED_INTERMEDIATE,
                success=True,
                tcp_connect_ms=10.0,
                mtproto_connect_ms=25.0,
                total_latency_ms=35.0,
            )

        with (
            patch("modules.tester.service.claim_due_proxies", return_value=[proxy]),
            patch("modules.tester.service.probe_proxy", side_effect=fake_probe),
        ):
            results = await service.run_batch()
            assert len(results) == 1
            assert results[0].success is True
            # Verify record_result executed queries in session 2
            assert mock_session.execute.await_count == 2  # obs insert + proxy update

    @pytest.mark.asyncio
    async def test_cancelled_batch_records_cancelled_and_reraises(self) -> None:
        mock_db = MagicMock()
        mock_session = AsyncMock()

        class DummyScope:
            async def __aenter__(self) -> AsyncMock:
                return mock_session

            async def __aexit__(self, *args: object) -> None:
                pass

        mock_db.session_scope.side_effect = lambda: DummyScope()
        service = TesterService(mock_db)
        recorded: list[TesterResult] = []

        async def capture(_session: object, result: TesterResult) -> None:
            recorded.append(result)

        proxy = Proxy(
            id=7,
            server="1.1.1.1",
            port=443,
            secret=ProxySecret("dd" + "11" * 16),
            fingerprint="a" * 64,
        )

        async def hang(*_args: object, **_kwargs: object) -> TesterResult:
            await asyncio.sleep(30)
            msg = "probe was not cancelled"
            raise AssertionError(msg)

        with (
            patch("modules.tester.service.claim_due_proxies", return_value=[proxy]),
            patch("modules.tester.service.probe_proxy", side_effect=hang),
            patch.object(service, "record_result", side_effect=capture),
        ):
            runner = asyncio.create_task(service.run_batch())
            await asyncio.sleep(0.02)
            runner.cancel()
            with pytest.raises(asyncio.CancelledError):
                await runner

        assert len(recorded) == 1
        assert recorded[0].success is False
        assert recorded[0].error_category == ErrorCategory.CANCELLED
        assert recorded[0].proxy_id == 7

    @pytest.mark.asyncio
    async def test_worker_tick_timeout_bounds_hanging_batch_and_next_tick_runs(self) -> None:
        """A finite tick timeout must stop a hung batch without poisoning tick N+1."""
        from core.lifecycle import WorkerLifecycle
        from tests.conftest import make_settings

        mock_db = MagicMock()
        mock_session = AsyncMock()

        class DummyScope:
            async def __aenter__(self) -> AsyncMock:
                return mock_session

            async def __aexit__(self, *args: object) -> None:
                pass

        mock_db.session_scope.side_effect = lambda: DummyScope()
        settings = make_settings(
            worker_poll_interval_seconds=0.001,
            heartbeat_interval_seconds=0.0,
            worker_error_backoff_seconds=0.0,
            worker_max_error_backoff_seconds=0.0,
            worker_tick_timeout_seconds=0.05,
        )
        service = TesterService(mock_db)
        recorded: list[TesterResult] = []
        still_running = True

        async def capture(_session: object, result: TesterResult) -> None:
            recorded.append(result)

        proxy = Proxy(
            id=9,
            server="1.1.1.1",
            port=443,
            secret=ProxySecret("dd" + "11" * 16),
            fingerprint="b" * 64,
        )

        async def hang(*_args: object, **_kwargs: object) -> TesterResult:
            nonlocal still_running
            try:
                await asyncio.sleep(30)
                msg = "probe was not cancelled by tick timeout"
                raise AssertionError(msg)
            finally:
                still_running = False

        with (
            patch("modules.tester.service.claim_due_proxies", return_value=[proxy]),
            patch("modules.tester.service.probe_proxy", side_effect=hang),
            patch.object(service, "record_result", side_effect=capture),
        ):

            async def tick(life: WorkerLifecycle) -> None:
                if life.tick_count >= 2:
                    life.request_shutdown("second-tick")
                    return
                await service.run_batch()

            life = WorkerLifecycle("tester-worker", settings=settings)
            await life.run(tick, interval=0.0)

        assert still_running is False
        assert recorded
        assert recorded[0].error_category == ErrorCategory.CANCELLED
        assert life.tick_count == 2
        assert life.failure_count >= 1
        assert life.shutdown_reason == "second-tick"
