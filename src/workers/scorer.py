"""``scoring-worker`` -- Process C.

Responsibility: turn persisted ``ProxyObservation`` history into append-only
``ProxyScore`` snapshots. No network I/O. No mutation of observations.

Run with::

    uv run mtproto-scorer
    # or
    uv run python -m workers.scorer
"""

from __future__ import annotations

import time

from core.database import Database
from core.lifecycle import WorkerLifecycle, worker_main
from modules.scoring.service import ScoringService

__all__ = ["WORKER_NAME", "main", "tick"]

WORKER_NAME = "scoring-worker"


async def tick(life: WorkerLifecycle, *, db: Database | None = None) -> None:
    """One scoring iteration: claim due proxies, score, persist snapshots."""
    started = time.monotonic()
    dispose_db = False

    if db is None:
        db = Database.from_settings(life.settings)
        dispose_db = True

    try:
        if not await db.is_reachable():
            life.logger.warning(
                "scorer_tick_db_unreachable",
                worker=WORKER_NAME,
                duration_ms=round((time.monotonic() - started) * 1000, 3),
            )
            return

        service = ScoringService(db, batch_size=life.settings.scorer_batch_size)
        results = await service.run_batch()

        life.logger.info(
            "scorer_tick",
            implemented=True,
            proxies_scored=len(results),
            scores_calculated=len(results),
            empty_windows=sum(1 for item in results if item.observation_count == 0),
            duration_ms=round((time.monotonic() - started) * 1000, 3),
        )
    finally:
        if dispose_db:
            await db.dispose()


def main() -> None:
    """Console-script entrypoint for the scoring worker process."""
    worker_main(WORKER_NAME, tick)


if __name__ == "__main__":
    main()
