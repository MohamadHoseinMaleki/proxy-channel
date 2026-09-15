"""``discovery-worker`` -- Process A.

Responsibility: discover MTProto proxy candidates from configured sources and
persist them.

.. warning::
   **Status: placeholder (Task 001).** The tick below only proves that this
   process starts, configures logging, handles signals and shuts down cleanly.
   It performs *no* discovery. The real engine (source fetching, link
   extraction, normalisation, deduplication, upsert) lands in Task 004.

Run with::

    uv run mtproto-discovery
    # or
    uv run python -m workers.discovery
"""

from __future__ import annotations

import time

from core.lifecycle import WorkerLifecycle, worker_main

__all__ = ["WORKER_NAME", "main", "tick"]

WORKER_NAME = "discovery-worker"

#: What this process will eventually do; surfaced in logs so a placeholder is
#: never mistaken for working behaviour.
_IMPLEMENTATION_TASK = "task-004-discovery"


async def tick(life: WorkerLifecycle) -> None:
    """One discovery iteration. Currently a heartbeat only."""
    started = time.monotonic()

    # TODO(task-004): load active sources, fetch, parse, dedupe, upsert proxies.
    life.logger.info(
        "discovery_tick",
        implemented=False,
        pending_task=_IMPLEMENTATION_TASK,
        sources_processed=0,
        proxies_found=0,
        new_proxies=0,
        duplicates=0,
        duration_ms=round((time.monotonic() - started) * 1000, 3),
    )


def main() -> None:
    """Console-script entrypoint for the discovery worker process."""
    worker_main(WORKER_NAME, tick)


if __name__ == "__main__":
    main()
