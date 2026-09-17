"""Tests for :mod:`core.lifecycle` -- shutdown, signals and the worker loop.

These prove the properties the three independent processes rely on:

* a stop signal ends the loop promptly instead of hanging;
* a raising tick never terminates the worker;
* Linux signals are handled via the event loop and Windows via ``signal.signal``;
* a repeated signal forces an immediate exit.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from typing import Any

import pytest

import core.lifecycle as lifecycle_module
from core.config import Settings
from core.lifecycle import (
    FORCED_EXIT_CODE,
    WorkerLifecycle,
    _candidate_signals,
    run_worker,
    worker_main,
)

from .conftest import make_settings


@pytest.fixture
def fast_settings() -> Settings:
    """Settings tuned so loops iterate in milliseconds and never heartbeat."""
    return make_settings(
        worker_poll_interval_seconds=0.001,
        heartbeat_interval_seconds=0.0,
        worker_error_backoff_seconds=0.0,
        worker_max_error_backoff_seconds=0.0,
    )


@pytest.fixture
def backoff_settings() -> Settings:
    return make_settings(
        worker_poll_interval_seconds=0.001,
        heartbeat_interval_seconds=0.0,
        worker_error_backoff_seconds=0.01,
        worker_max_error_backoff_seconds=0.08,
    )


class TestSleepAndShutdownState:
    async def test_sleep_completes_when_no_shutdown(self, fast_settings: Settings) -> None:
        life = WorkerLifecycle("tester", settings=fast_settings)
        assert await life.sleep(0.01) is True
        assert life.shutdown_requested is False

    async def test_sleep_returns_false_once_shutdown_requested(
        self, fast_settings: Settings
    ) -> None:
        life = WorkerLifecycle("tester", settings=fast_settings)
        life.request_shutdown("done")
        assert await life.sleep(5.0) is False

    async def test_non_positive_sleep_is_not_an_error(self, fast_settings: Settings) -> None:
        life = WorkerLifecycle("tester", settings=fast_settings)
        assert await life.sleep(0) is True
        assert await life.sleep(-1) is True

    async def test_sleep_wakes_early_on_shutdown(self, fast_settings: Settings) -> None:
        """A stop signal must not wait out the full poll interval."""
        life = WorkerLifecycle("tester", settings=fast_settings)

        async def stop_soon() -> None:
            await asyncio.sleep(0.02)
            life.request_shutdown("operator")

        stopper = asyncio.create_task(stop_soon())
        loop = asyncio.get_running_loop()
        started = loop.time()
        completed = await life.sleep(30.0)
        elapsed = loop.time() - started
        await stopper

        assert completed is False
        assert elapsed < 1.0
        assert life.shutdown_reason == "operator"

    async def test_request_shutdown_is_idempotent(self, fast_settings: Settings) -> None:
        life = WorkerLifecycle("tester", settings=fast_settings)
        life.request_shutdown("first")
        life.request_shutdown("second")
        assert life.shutdown_reason == "first"

    async def test_wait_for_shutdown_returns_reason(self, fast_settings: Settings) -> None:
        life = WorkerLifecycle("tester", settings=fast_settings)
        life.request_shutdown("term")
        assert await life.wait_for_shutdown() == "term"

    async def test_uptime_is_zero_before_start(self, fast_settings: Settings) -> None:
        assert WorkerLifecycle("tester", settings=fast_settings).uptime_seconds == 0.0


class TestRunLoop:
    async def test_runs_ticks_until_shutdown(self, fast_settings: Settings) -> None:
        seen: list[int] = []

        async def tick(life: WorkerLifecycle) -> None:
            seen.append(life.tick_count)
            if len(seen) >= 3:
                life.request_shutdown("enough")

        life = WorkerLifecycle("discovery", settings=fast_settings)
        await life.run(tick, interval=0.0)

        assert seen == [1, 2, 3]
        assert life.failure_count == 0
        assert life.shutdown_reason == "enough"
        # ``>= 0.0``, not ``> 0.0``. With ``interval=0.0`` three ticks complete
        # faster than the monotonic clock advances on some platforms, so the
        # measured uptime is legitimately zero: on Windows ``time.monotonic()``
        # moves in ~15.6 ms quanta. ``uptime_seconds`` already documents this by
        # clamping with ``max(0.0, ...)``, so a strict ``>`` was asserting a
        # property the code never promised. See
        # ``test_survives_a_coarse_monotonic_clock`` for the guarded version.
        assert life.uptime_seconds >= 0.0

    async def test_survives_a_coarse_monotonic_clock(
        self, fast_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The loop must stay correct when the clock cannot measure it.

        Regression guard for a Windows-only failure. ``time.monotonic()`` there
        advances in ~15.6 ms quanta, so a loop running with ``interval=0.0``
        completes several ticks without the clock moving at all and the measured
        uptime is exactly ``0.0``. An earlier ``> 0.0`` assertion failed on
        Windows while passing on every other platform -- the worst kind of bug,
        because the machine that trips over it is not the machine running CI.

        Quantising the clock reproduces that platform here, on any host, so the
        guard runs everywhere instead of only where it was discovered.
        """
        real_monotonic = time.monotonic
        quantum = 0.015625  # 1/64 s -- the classic Windows system timer tick

        def coarse() -> float:
            return int(real_monotonic() / quantum) * quantum

        monkeypatch.setattr(time, "monotonic", coarse)

        seen: list[int] = []

        async def tick(life: WorkerLifecycle) -> None:
            seen.append(life.tick_count)
            if len(seen) >= 3:
                life.request_shutdown("enough")

        life = WorkerLifecycle("discovery", settings=fast_settings)
        await life.run(tick, interval=0.0)

        # The clock stood still; the loop's own bookkeeping must not have.
        assert seen == [1, 2, 3]
        assert life.tick_count == 3
        assert life.failure_count == 0
        assert life.shutdown_reason == "enough"
        assert life.uptime_seconds >= 0.0

    async def test_stops_immediately_if_shutdown_already_requested(
        self, fast_settings: Settings
    ) -> None:
        calls = 0

        async def tick(_: WorkerLifecycle) -> None:
            nonlocal calls
            calls += 1

        life = WorkerLifecycle("scorer", settings=fast_settings)
        life.request_shutdown("pre-stopped")
        await life.run(tick, interval=0.0)
        assert calls == 0

    async def test_raising_tick_does_not_kill_the_worker(self, fast_settings: Settings) -> None:
        """The single most important resilience property of the loop."""
        calls = 0

        async def tick(life: WorkerLifecycle) -> None:
            nonlocal calls
            calls += 1
            if calls < 3:
                msg = "boom"
                raise RuntimeError(msg)
            life.request_shutdown("recovered")

        life = WorkerLifecycle("tester", settings=fast_settings)
        await life.run(tick, interval=0.0)

        assert calls == 3
        assert life.tick_count == 3
        assert life.failure_count == 2
        assert life.consecutive_failures == 0
        assert life.shutdown_reason == "recovered"

    async def test_consecutive_failures_reset_after_success(self, fast_settings: Settings) -> None:
        async def tick(life: WorkerLifecycle) -> None:
            if life.tick_count == 1:
                msg = "transient"
                raise ValueError(msg)
            if life.tick_count >= 2:
                life.request_shutdown("done")

        life = WorkerLifecycle("tester", settings=fast_settings)
        await life.run(tick, interval=0.0)
        assert life.failure_count == 1
        assert life.consecutive_failures == 0

    async def test_backoff_grows_with_consecutive_failures(
        self, backoff_settings: Settings
    ) -> None:
        life = WorkerLifecycle("tester", settings=backoff_settings)
        life.consecutive_failures = 1
        assert life._next_delay(0.0, 0.0) == pytest.approx(0.01)
        life.consecutive_failures = 2
        assert life._next_delay(0.0, 0.0) == pytest.approx(0.02)
        life.consecutive_failures = 3
        assert life._next_delay(0.0, 0.0) == pytest.approx(0.04)
        # ... and is capped.
        life.consecutive_failures = 20
        assert life._next_delay(0.0, 0.0) == pytest.approx(0.08)

    async def test_success_delay_respects_poll_interval(self, fast_settings: Settings) -> None:
        life = WorkerLifecycle("tester", settings=fast_settings)
        assert life._next_delay(1.0, 0.25) == pytest.approx(0.75)
        assert life._next_delay(1.0, 5.0) == 0.0

    async def test_settings_tick_timeout_is_applied(self) -> None:
        settings = make_settings(
            worker_poll_interval_seconds=0.001,
            heartbeat_interval_seconds=0.0,
            worker_error_backoff_seconds=0.0,
            worker_max_error_backoff_seconds=0.0,
            worker_tick_timeout_seconds=0.05,
        )

        async def tick(life: WorkerLifecycle) -> None:
            if life.tick_count >= 2:
                life.request_shutdown("done")
            await asyncio.sleep(30)

        life = WorkerLifecycle("tester", settings=settings)
        await life.run(tick, interval=0.0)
        assert life.failure_count == 2
        assert life.tick_count == 2

    async def test_tick_timeout_is_enforced(self, fast_settings: Settings) -> None:
        """A hung tick must not hang the worker forever."""

        async def tick(life: WorkerLifecycle) -> None:
            if life.tick_count >= 2:
                life.request_shutdown("done")
            await asyncio.sleep(30)

        life = WorkerLifecycle("tester", settings=fast_settings)
        await life.run(tick, interval=0.0, tick_timeout=0.05)

        assert life.failure_count == 2
        assert life.tick_count == 2

    async def test_cancellation_propagates(self, fast_settings: Settings) -> None:
        async def tick(_: WorkerLifecycle) -> None:
            raise asyncio.CancelledError

        life = WorkerLifecycle("tester", settings=fast_settings)
        with pytest.raises(asyncio.CancelledError):
            await life.run(tick, interval=0.0)
        assert life.shutdown_reason == "cancelled"

    async def test_heartbeat_is_emitted_when_enabled(self) -> None:
        settings = make_settings(
            worker_poll_interval_seconds=0.001,
            heartbeat_interval_seconds=0.01,
        )
        life = WorkerLifecycle("scorer", settings=settings)
        life._last_heartbeat = 0.0  # force the first heartbeat to be due
        life._maybe_heartbeat()
        assert life._last_heartbeat is not None and life._last_heartbeat > 0.0

    async def test_heartbeat_disabled_at_zero(self, fast_settings: Settings) -> None:
        life = WorkerLifecycle("scorer", settings=fast_settings)
        life._maybe_heartbeat()
        assert life._last_heartbeat is None

    async def test_ticks_never_overlap(self, fast_settings: Settings) -> None:
        concurrent = 0
        peak = 0

        async def tick(life: WorkerLifecycle) -> None:
            nonlocal concurrent, peak
            concurrent += 1
            peak = max(peak, concurrent)
            await asyncio.sleep(0.01)
            concurrent -= 1
            if life.tick_count >= 3:
                life.request_shutdown("enough")

        life = WorkerLifecycle("tester", settings=fast_settings)
        await life.run(tick, interval=0.0)
        assert peak == 1
        assert concurrent == 0

    async def test_run_is_not_reentrant(self, fast_settings: Settings) -> None:
        started = asyncio.Event()

        async def tick(life: WorkerLifecycle) -> None:
            started.set()
            await asyncio.sleep(0.2)
            life.request_shutdown("done")

        life = WorkerLifecycle("tester", settings=fast_settings)
        runner = asyncio.create_task(life.run(tick, interval=0.0))
        await started.wait()
        with pytest.raises(RuntimeError, match="not re-entrant"):
            await life.run(tick, interval=0.0)
        life.request_shutdown("done")
        await runner

    async def test_negative_interval_does_not_busy_loop(self, fast_settings: Settings) -> None:
        async def tick(life: WorkerLifecycle) -> None:
            if life.tick_count >= 2:
                life.request_shutdown("done")

        life = WorkerLifecycle("tester", settings=fast_settings)
        await life.run(tick, interval=-1.0)
        assert life.tick_count == 2
        assert life.failure_count == 0

    async def test_cancellation_during_tick_runs_cleanup(self, fast_settings: Settings) -> None:
        cleaned: list[bool] = []
        started = asyncio.Event()

        async def tick(_: WorkerLifecycle) -> None:
            started.set()
            try:
                await asyncio.sleep(30)
            finally:
                cleaned.append(True)

        life = WorkerLifecycle("tester", settings=fast_settings)
        runner = asyncio.create_task(life.run(tick, interval=0.0))
        await started.wait()
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner
        assert cleaned == [True]
        assert life.shutdown_reason == "cancelled"

    async def test_tick_timeout_still_runs_tick_cleanup(self, fast_settings: Settings) -> None:
        cleaned: list[int] = []

        async def tick(life: WorkerLifecycle) -> None:
            try:
                if life.tick_count >= 2:
                    life.request_shutdown("done")
                    return
                await asyncio.sleep(30)
            finally:
                cleaned.append(life.tick_count)

        life = WorkerLifecycle("tester", settings=fast_settings)
        await life.run(tick, interval=0.0, tick_timeout=0.05)
        assert cleaned
        assert life.failure_count >= 1

    async def test_tick_exception_cleanup_then_continue(self, fast_settings: Settings) -> None:
        cleaned: list[str] = []

        async def tick(life: WorkerLifecycle) -> None:
            try:
                if life.tick_count == 1:
                    msg = "boom"
                    raise RuntimeError(msg)
                life.request_shutdown("recovered")
            finally:
                cleaned.append("finally")

        life = WorkerLifecycle("tester", settings=fast_settings)
        await life.run(tick, interval=0.0)
        assert cleaned == ["finally", "finally"]
        assert life.failure_count == 1
        assert life.shutdown_reason == "recovered"

    async def test_failed_tick_does_not_log_proxy_secret(
        self, fast_settings: Settings, json_logs: pytest.CaptureFixture[str]
    ) -> None:
        secret = "000102030405060708090a0b0c0d0e0f"

        async def tick(life: WorkerLifecycle) -> None:
            if life.tick_count >= 2:
                life.request_shutdown("done")
                return
            msg = f"connect failed secret={secret}"
            raise RuntimeError(msg)

        life = WorkerLifecycle("tester", settings=fast_settings)
        await life.run(tick, interval=0.0)
        output = json_logs.readouterr().out
        assert secret not in output
        assert "***REDACTED***" in output


class TestSignalHandling:
    async def test_skips_real_os_kill_on_windows(
        self, fast_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On Windows, os.kill(pid, SIGTERM) calls TerminateProcess and kills the runner."""
        monkeypatch.setattr(sys, "platform", "win32")
        with pytest.raises(pytest.skip.Exception):
            await self.test_real_sigterm_requests_graceful_shutdown(fast_settings)
        with pytest.raises(pytest.skip.Exception):
            await self.test_running_loop_stops_on_real_signal(fast_settings)

    async def test_candidate_signals_are_platform_appropriate(self) -> None:
        names = {signal.Signals(s).name for s in _candidate_signals()}
        assert "SIGINT" in names
        assert "SIGTERM" in names
        if sys.platform == "win32":
            assert "SIGHUP" not in names
        else:
            assert "SIGBREAK" not in names

    async def test_install_and_uninstall_are_symmetrical(self, fast_settings: Settings) -> None:
        life = WorkerLifecycle("tester", settings=fast_settings)
        handled = life.install_signal_handlers()
        try:
            assert handled
            # Re-installing is a no-op rather than a double registration.
            assert life.install_signal_handlers() == []
        finally:
            life.uninstall_signal_handlers()
        assert life._installed is False

    async def test_real_sigterm_requests_graceful_shutdown(self, fast_settings: Settings) -> None:
        """End-to-end Linux signal handling through the running event loop."""
        if sys.platform == "win32":
            pytest.skip("Windows os.kill(pid, SIGTERM) calls TerminateProcess and cannot be caught")
        life = WorkerLifecycle("tester", settings=fast_settings)
        handled = life.install_signal_handlers()
        if "SIGTERM" not in handled:  # pragma: no cover - platform guard
            life.uninstall_signal_handlers()
            pytest.skip("SIGTERM handler could not be installed on this platform")
        try:
            os.kill(os.getpid(), signal.SIGTERM)
            # Give the event loop a chance to run the signal callback.
            for _ in range(50):
                if life.shutdown_requested:
                    break
                await asyncio.sleep(0.01)
            assert life.shutdown_requested is True
            assert life.shutdown_reason == "signal:SIGTERM"
        finally:
            life.uninstall_signal_handlers()

    async def test_running_loop_stops_on_real_signal(self, fast_settings: Settings) -> None:
        """``run()`` must return promptly after SIGINT instead of hanging."""
        if sys.platform == "win32":
            pytest.skip("Windows os.kill does not deliver catchable signals to a running loop")
        life = WorkerLifecycle("discovery", settings=fast_settings)
        handled = life.install_signal_handlers()
        if "SIGINT" not in handled:  # pragma: no cover - platform guard
            life.uninstall_signal_handlers()
            pytest.skip("SIGINT handler could not be installed on this platform")

        async def tick(_: WorkerLifecycle) -> None:
            await asyncio.sleep(0.001)

        runner = asyncio.create_task(life.run(tick, interval=0.05))
        try:
            await asyncio.sleep(0.05)
            os.kill(os.getpid(), signal.SIGINT)
            await asyncio.wait_for(runner, timeout=2.0)
        except KeyboardInterrupt:  # pragma: no cover - would mean the handler failed
            runner.cancel()
            pytest.fail("SIGINT was not intercepted by the lifecycle handler")
        finally:
            life.uninstall_signal_handlers()

        assert runner.done()
        assert life.shutdown_reason == "signal:SIGINT"

    async def test_second_signal_forces_immediate_exit(
        self, fast_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        exits: list[int] = []
        flushed: list[bool] = []
        monkeypatch.setattr(lifecycle_module.os, "_exit", lambda code: exits.append(code))
        monkeypatch.setattr(lifecycle_module, "_flush_logging", lambda: flushed.append(True))

        life = WorkerLifecycle("tester", settings=fast_settings)
        life._handle_signal("SIGINT")
        assert life.shutdown_requested is True
        assert exits == []

        life._handle_signal("SIGINT")
        assert exits == [FORCED_EXIT_CODE]
        assert flushed == [True]

    async def test_fallback_handler_used_when_loop_cannot(
        self, fast_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Windows event loops raise ``NotImplementedError`` from add_signal_handler."""
        loop = asyncio.get_running_loop()

        def unsupported(*_args: Any, **_kwargs: Any) -> None:
            raise NotImplementedError

        monkeypatch.setattr(loop, "add_signal_handler", unsupported, raising=False)

        registered: list[tuple[int, Any]] = []

        def fake_signal(signum: int, handler: Any) -> Any:
            registered.append((signum, handler))
            return signal.SIG_DFL

        monkeypatch.setattr(lifecycle_module.signal, "signal", fake_signal)

        life = WorkerLifecycle("tester", settings=fast_settings)
        handled = life.install_signal_handlers()
        try:
            assert handled
            assert registered
            assert life._loop_signals == []

            # Simulate the OS delivering the signal to the fallback handler.
            signum, handler = registered[0]
            handler(signum, None)
            assert life.shutdown_requested is True
            assert life.shutdown_reason == f"signal:{signal.Signals(signum).name}"
        finally:
            life.uninstall_signal_handlers()

        assert life._previous_handlers == []

    async def test_uninstall_without_install_is_safe(self, fast_settings: Settings) -> None:
        WorkerLifecycle("tester", settings=fast_settings).uninstall_signal_handlers()


class TestContextManager:
    async def test_binds_and_unbinds_worker_context(self, fast_settings: Settings) -> None:
        import structlog

        async with WorkerLifecycle("scoring-worker", settings=fast_settings) as life:
            bound = structlog.contextvars.get_contextvars()
            assert bound["worker"] == "scoring-worker"
            assert bound["run_id"] == life.run_id
        assert structlog.contextvars.get_contextvars() == {}

    async def test_run_id_defaults_to_a_short_unique_value(self, fast_settings: Settings) -> None:
        a = WorkerLifecycle("tester", settings=fast_settings)
        b = WorkerLifecycle("tester", settings=fast_settings)
        assert len(a.run_id) == 12
        assert a.run_id != b.run_id

    async def test_explicit_run_id_is_honoured(self, fast_settings: Settings) -> None:
        assert WorkerLifecycle("tester", settings=fast_settings, run_id="abc").run_id == "abc"


class TestRunWorker:
    async def test_returns_zero_on_clean_shutdown(self, fast_settings: Settings) -> None:
        async def tick(life: WorkerLifecycle) -> None:
            life.request_shutdown("immediate")

        assert await run_worker("tester", tick, settings=fast_settings, interval=0.0) == 0

    async def test_returns_zero_even_when_ticks_fail(self, fast_settings: Settings) -> None:
        calls = 0

        async def tick(life: WorkerLifecycle) -> None:
            nonlocal calls
            calls += 1
            if calls >= 2:
                life.request_shutdown("done")
            else:
                msg = "temporary"
                raise OSError(msg)

        assert await run_worker("tester", tick, settings=fast_settings, interval=0.0) == 0
        assert calls == 2

    async def test_returns_one_when_startup_fails(
        self, fast_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A worker that cannot even start must exit non-zero for the supervisor."""

        def explode(*_args: Any) -> list[str]:
            msg = "cannot install handlers"
            raise RuntimeError(msg)

        monkeypatch.setattr(WorkerLifecycle, "install_signal_handlers", explode)
        assert await run_worker("tester", _noop_tick, settings=fast_settings) == 1

    async def test_returns_forced_exit_code_on_cancellation(self, fast_settings: Settings) -> None:
        async def cancel(_life: WorkerLifecycle) -> None:
            raise asyncio.CancelledError

        # run_worker's `async with` must not swallow cancellation into a crash.
        code = await run_worker("tester", cancel, settings=fast_settings, interval=0.0)
        assert code == FORCED_EXIT_CODE

    def test_worker_main_raises_system_exit(
        self, fast_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(lifecycle_module, "get_settings", lambda: fast_settings)

        async def tick(life: WorkerLifecycle) -> None:
            life.request_shutdown("done")

        with pytest.raises(SystemExit) as excinfo:
            worker_main("tester", tick)
        assert excinfo.value.code == 0


async def _noop_tick(_: WorkerLifecycle) -> None:
    return None
