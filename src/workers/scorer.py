"""``scoring-worker`` -- Process C.

Responsibility: aggregate observations into deterministic per-proxy scores.

.. warning::
   **Status: placeholder (Task 001).** The tick below only proves that this
   process starts, configures logging, handles signals and shuts down cleanly.
   It calculates *no* scores. The metrics engine lands in Task 008 and the
   scoring worker loop in Task 009.

Run with::

    uv run mtproto-scorer
    # or
    uv run python -m workers.scorer
"""

from __future__ import annotations

import time

from core.lifecycle import WorkerLifecycle, worker_main

__all__ = ["WORKER_NAME", "main", "tick"]

WORKER_NAME = "scoring-worker"

_IMPLEMENTATION_TASKS = ("task-008-scoring", "task-009-scorer-worker")


async def tick(life: WorkerLifecycle) -> None:
    """One scoring iteration. Currently a heartbeat only."""
    started = time.monotonic()

    # TODO(task-008/009): select proxies needing recalculation, aggregate
    # observations over 1h/6h/24h windows, upsert one ProxyScore row per proxy.
    life.logger.info(
        "scorer_tick",
        implemented=False,
        pending_tasks=list(_IMPLEMENTATION_TASKS),
        proxies_scored=0,
        scores_calculated=0,
        duration_ms=round((time.monotonic() - started) * 1000, 3),
    )


def main() -> None:
    """Console-script entrypoint for the scoring worker process."""
    worker_main(WORKER_NAME, tick)


if __name__ == "__main__":
    main()
