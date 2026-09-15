"""``tester-worker`` -- Process B.

Responsibility: claim proxies that need testing with ``FOR UPDATE SKIP LOCKED``,
validate them over MTProto with Telethon under bounded concurrency, and record
an observation for every attempt (successes *and* failures).

Run with::

    uv run mtproto-tester
    # or
    uv run python -m workers.tester
"""

from __future__ import annotations

import time

from core.database import Database
from core.lifecycle import WorkerLifecycle, worker_main
from modules.tester.service import TesterService

__all__ = ["WORKER_NAME", "main", "tick"]

WORKER_NAME = "tester-worker"


async def tick(life: WorkerLifecycle, *, db: Database | None = None) -> None:
    """One testing iteration: claim due proxies, probe them, record observations."""
    started = time.monotonic()
    dispose_db = False

    if db is None:
        db = Database.from_settings(life.settings)
        dispose_db = True

    try:
        if not await db.is_reachable():
            life.logger.warning(
                "tester_tick_db_unreachable",
                worker=WORKER_NAME,
                duration_ms=round((time.monotonic() - started) * 1000, 3),
            )
            return

        service = TesterService(
            db,
            api_id=life.settings.telegram_api_id,
            api_hash=(
                life.settings.telegram_api_hash.get_secret_value()
                if life.settings.telegram_api_hash
                else None
            ),
            concurrency=life.settings.tester_concurrency,
            tcp_timeout_seconds=life.settings.tester_tcp_timeout_seconds,
            mtproto_timeout_seconds=life.settings.tester_mtproto_timeout_seconds,
            total_timeout_seconds=life.settings.tester_total_timeout_seconds,
            batch_size=life.settings.tester_batch_size,
        )

        results = await service.run_batch()

        successes = sum(1 for r in results if r.success)
        failures = len(results) - successes
        timeouts = sum(
            1 for r in results if r.error_category in ("TCP_TIMEOUT", "MT_PROTO_TIMEOUT")
        )

        life.logger.info(
            "tester_tick",
            implemented=True,
            proxies_claimed=len(results),
            tests_started=len(results),
            tests_success=successes,
            tests_failed=failures,
            timeouts=timeouts,
            duration_ms=round((time.monotonic() - started) * 1000, 3),
        )
    finally:
        if dispose_db:
            await db.dispose()


def main() -> None:
    """Console-script entrypoint for the tester worker process."""
    worker_main(WORKER_NAME, tick)


if __name__ == "__main__":
    main()
