"""Process lifecycle: signal handling, graceful shutdown and the worker loop.

Each of the three workers (discovery, tester, scorer) is an *independent OS
process*. This module gives every one of them the same shutdown semantics
without letting them share any runtime state:

* ``SIGINT`` / ``SIGTERM`` / ``SIGHUP`` (POSIX) and ``SIGINT`` / ``SIGTERM`` /
  ``SIGBREAK`` (Windows) request a graceful stop.
* A second signal forces an immediate exit.
* ``loop.add_signal_handler`` is used where available, with a portable
  ``signal.signal`` fallback on Windows, whose event loops do not implement it.
* A failing tick never kills the process: errors are logged and backed off.

Nothing here holds a database connection, so importing this module is free and
the workers stay trivially testable.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from types import FrameType
from typing import Any

from core.config import Settings, get_settings
from core.logger import (
    bind_worker_context,
    configure_logging,
    get_logger,
    safe_error_message,
    unbind_worker_context,
)

__all__ = [
    "Tick",
    "WorkerLifecycle",
    "run_worker",
    "worker_main",
]

#: A unit of work executed once per loop iteration.
Tick = Callable[["WorkerLifecycle"], Awaitable[None]]

_IS_WINDOWS = sys.platform == "win32"

#: Exit code used when the operator insists on stopping (second signal).
FORCED_EXIT_CODE = 130


def _candidate_signals() -> list[int]:
    """Signals worth handling, filtered to what this platform defines."""
    names = ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK")
    available: list[int] = []
    for name in names:
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        if name == "SIGBREAK" and not _IS_WINDOWS:
            continue
        available.append(int(sig))
    # Preserve order, drop duplicates (SIGBREAK aliases SIGINT on some platforms).
    return list(dict.fromkeys(available))


class WorkerLifecycle:
    """Owns shutdown state for exactly one worker process.

    Use as an async context manager so signal handlers are always removed::

        async with WorkerLifecycle("tester") as life:
            await life.run(tick)
    """

    def __init__(
        self,
        name: str,
        *,
        settings: Settings | None = None,
        run_id: str | None = None,
    ) -> None:
        self.name = name
        self.settings = settings or get_settings()
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.started_at: float | None = None

        self._logger = get_logger(f"worker.{name}")
        self._shutdown_event = asyncio.Event()
        self._shutdown_reason: str | None = None
        self._signal_count = 0
        self._loop_signals: list[int] = []
        self._previous_handlers: list[tuple[int, Any]] = []
        self._installed = False
        self._last_heartbeat: float | None = None

        self.tick_count = 0
        self.failure_count = 0
        self.consecutive_failures = 0
        self._run_active = False
        self._tick_active = False

    # -- introspection ------------------------------------------------------

    @property
    def logger(self) -> Any:
        """The worker's bound logger (exposed so ticks can log consistently)."""
        return self._logger

    @property
    def shutdown_requested(self) -> bool:
        return self._shutdown_event.is_set()

    @property
    def shutdown_reason(self) -> str | None:
        return self._shutdown_reason

    @property
    def uptime_seconds(self) -> float:
        if self.started_at is None:
            return 0.0
        return max(0.0, time.monotonic() - self.started_at)

    # -- shutdown -----------------------------------------------------------

    def request_shutdown(self, reason: str = "requested") -> None:
        """Ask the loop to stop after the current tick. Idempotent."""
        if self._shutdown_event.is_set():
            return
        self._shutdown_reason = reason
        self._shutdown_event.set()
        self._logger.info("worker_shutdown_requested", reason=reason, run_id=self.run_id)

    async def wait_for_shutdown(self) -> str:
        """Block until shutdown is requested; returns the reason."""
        await self._shutdown_event.wait()
        return self._shutdown_reason or "requested"

    async def sleep(self, seconds: float) -> bool:
        """Sleep, waking immediately on shutdown.

        Returns ``True`` if the sleep completed, ``False`` if it was interrupted
        by a shutdown request.
        """
        if self.shutdown_requested:
            return False
        if seconds <= 0:
            return True
        try:
            await asyncio.wait_for(self._shutdown_event.wait(), timeout=seconds)
        except TimeoutError:
            return True
        return False

    # -- signal handling ----------------------------------------------------

    def install_signal_handlers(self) -> list[str]:
        """Register platform-appropriate signal handlers. Returns signal names."""
        if self._installed:
            return []
        loop = asyncio.get_running_loop()
        handled: list[str] = []

        for sig in _candidate_signals():
            sig_name = signal.Signals(sig).name
            try:
                loop.add_signal_handler(sig, self._on_loop_signal, sig_name)
                self._loop_signals.append(sig)
            except (NotImplementedError, RuntimeError, OSError, ValueError):
                # Windows event loops do not implement add_signal_handler.
                try:
                    previous = signal.signal(sig, self._on_fallback_signal)
                except (OSError, ValueError, RuntimeError):
                    self._logger.debug("worker_signal_unavailable", signal=sig_name)
                    continue
                self._previous_handlers.append((sig, previous))
            handled.append(sig_name)

        self._installed = True
        self._logger.debug("worker_signals_installed", signals=handled, platform=sys.platform)
        return handled

    def uninstall_signal_handlers(self) -> None:
        """Restore the previous signal disposition."""
        if not self._installed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and not loop.is_closed():
            for sig in self._loop_signals:
                with contextlib.suppress(NotImplementedError, RuntimeError, OSError, ValueError):
                    loop.remove_signal_handler(sig)
        for sig, previous in self._previous_handlers:
            with contextlib.suppress(OSError, ValueError, RuntimeError):
                signal.signal(sig, previous)
        self._loop_signals.clear()
        self._previous_handlers.clear()
        self._installed = False

    def _on_loop_signal(self, sig_name: str) -> None:
        self._handle_signal(sig_name)

    def _on_fallback_signal(self, signum: int, _frame: FrameType | None = None) -> None:
        try:
            sig_name = signal.Signals(signum).name
        except ValueError:
            sig_name = str(signum)
        self._handle_signal(sig_name)

    def _handle_signal(self, sig_name: str) -> None:
        self._signal_count += 1
        if self._signal_count == 1:
            self.request_shutdown(f"signal:{sig_name}")
            return
        # The operator insisted. Exit immediately without waiting for cleanup.
        self._logger.warning(
            "worker_forced_exit",
            signal=sig_name,
            exit_code=FORCED_EXIT_CODE,
            run_id=self.run_id,
        )
        _flush_logging()
        os._exit(FORCED_EXIT_CODE)

    # -- main loop ----------------------------------------------------------

    async def run(
        self,
        tick: Tick,
        *,
        interval: float | None = None,
        tick_timeout: float | None = None,
    ) -> None:
        """Run ``tick`` until shutdown is requested.

        ``tick`` exceptions are contained: one bad iteration logs
        ``worker_tick_failed`` and backs off instead of terminating the process.

        One ``run()`` at a time per instance. Ticks are strictly sequential:
        tick N+1 cannot start until tick N's ``finally`` has finished.
        """
        if self._run_active:
            msg = "WorkerLifecycle.run() is not re-entrant; ticks must not overlap"
            raise RuntimeError(msg)

        if interval is None:
            poll_interval = self.settings.worker_poll_interval_seconds
        else:
            # Direct callers may pass a test interval; never busy-loop on a
            # negative value. Settings already rejects ``<= 0`` via pydantic.
            poll_interval = max(0.0, float(interval))

        if tick_timeout is None:
            configured = self.settings.worker_tick_timeout_seconds
            tick_timeout = configured if configured > 0 else None
        elif tick_timeout <= 0:
            tick_timeout = None

        self._run_active = True
        self.started_at = time.monotonic()
        self._logger.info(
            "worker_started",
            run_id=self.run_id,
            pid=os.getpid(),
            platform=sys.platform,
            poll_interval_seconds=poll_interval,
        )

        try:
            while not self.shutdown_requested:
                tick_started = time.monotonic()
                self.tick_count += 1
                tick_no = self.tick_count
                self._logger.debug("worker_tick_started", tick=tick_no, run_id=self.run_id)
                try:
                    await self._run_one_tick(tick, tick_timeout)
                    self.consecutive_failures = 0
                except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                    # Cooperative cancellation must propagate to asyncio.run().
                    # Do not convert it into a successful tick.
                    self.request_shutdown("cancelled")
                    raise
                except TimeoutError:
                    self._record_failure(tick_no, "tick_timeout", TimeoutError(str(tick_timeout)))
                except Exception as exc:
                    # A worker must survive any single bad tick.
                    self._record_failure(tick_no, "tick_error", exc)

                self._maybe_heartbeat()

                elapsed = time.monotonic() - tick_started
                if not await self.sleep(self._next_delay(poll_interval, elapsed)):
                    break
        finally:
            self._run_active = False
            self._tick_active = False

        self._logger.info(
            "worker_stopped",
            run_id=self.run_id,
            reason=self._shutdown_reason or "unknown",
            uptime_seconds=round(self.uptime_seconds, 3),
            ticks=self.tick_count,
            failures=self.failure_count,
        )

    async def _run_one_tick(self, tick: Tick, tick_timeout: float | None) -> None:
        if self._tick_active:
            msg = "overlapping tick in a single worker process"
            raise RuntimeError(msg)
        self._tick_active = True
        try:
            if tick_timeout is not None:
                await asyncio.wait_for(tick(self), timeout=tick_timeout)
            else:
                await tick(self)
        finally:
            self._tick_active = False

    def _record_failure(self, tick_no: int, kind: str, exc: BaseException) -> None:
        self.failure_count += 1
        self.consecutive_failures += 1
        self._logger.error(
            "worker_tick_failed",
            tick=tick_no,
            kind=kind,
            error=safe_error_message(exc),
            exception_type=type(exc).__name__,
            consecutive_failures=self.consecutive_failures,
            exc_info=exc,
        )

    def _next_delay(self, poll_interval: float, elapsed: float) -> float:
        """Steady cadence on success, exponential backoff after failures."""
        if self.consecutive_failures == 0:
            return max(0.0, poll_interval - elapsed)
        base = max(self.settings.worker_error_backoff_seconds, poll_interval)
        ceiling = max(self.settings.worker_max_error_backoff_seconds, base)
        exponent = max(0, self.consecutive_failures - 1)
        backoff: float = base * (2.0**exponent)
        return min(ceiling, backoff)

    def _maybe_heartbeat(self) -> None:
        interval = self.settings.heartbeat_interval_seconds
        if interval <= 0:
            return
        now = time.monotonic()
        if self._last_heartbeat is None:
            self._last_heartbeat = now
            return
        if now - self._last_heartbeat < interval:
            return
        self._last_heartbeat = now
        self._logger.info(
            "worker_heartbeat",
            run_id=self.run_id,
            uptime_seconds=round(self.uptime_seconds, 3),
            ticks=self.tick_count,
            failures=self.failure_count,
        )

    # -- context manager ----------------------------------------------------

    async def __aenter__(self) -> WorkerLifecycle:
        bind_worker_context(self.name, run_id=self.run_id)
        self.install_signal_handlers()
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        self.uninstall_signal_handlers()
        unbind_worker_context()


def _flush_logging() -> None:
    """Best-effort flush before a hard exit; failure here must not mask the exit."""
    with contextlib.suppress(Exception):
        logging.shutdown()


async def run_worker(
    name: str,
    tick: Tick,
    *,
    settings: Settings | None = None,
    interval: float | None = None,
    tick_timeout: float | None = None,
) -> int:
    """Standard entrypoint body for a worker process.

    Configures logging, binds worker context, installs signal handlers and runs
    ``tick`` until shutdown. Returns a process exit code.
    """
    cfg = settings or get_settings()
    configure_logging(cfg)
    lifecycle = WorkerLifecycle(name, settings=cfg)
    try:
        async with lifecycle:
            await lifecycle.run(tick, interval=interval, tick_timeout=tick_timeout)
    except (KeyboardInterrupt, asyncio.CancelledError):
        return FORCED_EXIT_CODE
    except Exception as exc:
        # Top-level guard: report, then let systemd/Task Scheduler restart us.
        lifecycle.logger.critical(
            "worker_crashed",
            error=safe_error_message(exc),
            exception_type=type(exc).__name__,
            exc_info=exc,
        )
        return 1
    return 0


def worker_main(name: str, tick: Tick) -> None:
    """Synchronous ``main()`` used by the console-script entrypoints."""
    raise SystemExit(asyncio.run(run_worker(name, tick)))
