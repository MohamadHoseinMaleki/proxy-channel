"""``tester-worker`` -- Process B.

Responsibility: claim proxies that need testing, validate them over MTProto with
Telethon, and record an observation for every attempt (successes *and* failures).

.. warning::
   **Status: placeholder (Task 001).** The tick below only proves that this
   process starts, configures logging, handles signals and shuts down cleanly.
   It performs *no* network I/O and writes no observations. The MTProto tester
   lands in Task 005, batch claiming with ``FOR UPDATE SKIP LOCKED`` in Task 006
   and observation storage in Task 007.

Run with::

    uv run mtproto-tester
    # or
    uv run python -m workers.tester
"""

from __future__ import annotations

import time

from core.lifecycle import WorkerLifecycle, worker_main

__all__ = ["WORKER_NAME", "main", "tick"]

WORKER_NAME = "tester-worker"

_IMPLEMENTATION_TASKS = ("task-005-mtproto-tester", "task-006-tester-worker")


async def tick(life: WorkerLifecycle) -> None:
    """One testing iteration. Currently a heartbeat only."""
    started = time.monotonic()

    # TODO(task-005/006): claim a locked batch of due proxies, test each with
    # bounded concurrency, store a ProxyObservation per attempt.
    life.logger.info(
        "tester_tick",
        implemented=False,
        pending_tasks=list(_IMPLEMENTATION_TASKS),
        proxies_claimed=0,
        tests_started=0,
        tests_success=0,
        tests_failed=0,
        timeouts=0,
        duration_ms=round((time.monotonic() - started) * 1000, 3),
    )


def main() -> None:
    """Console-script entrypoint for the tester worker process."""
    worker_main(WORKER_NAME, tick)


if __name__ == "__main__":
    main()
