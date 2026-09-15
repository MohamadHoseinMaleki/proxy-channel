import asyncio
from src.core.config import get_settings
from src.core.logger import setup_logging
from src.core.lifecycle import WorkerApp
import structlog

logger = structlog.get_logger()

async def scorer_main(shutdown_event: asyncio.Event) -> None:
    while not shutdown_event.is_set():
        logger.debug("scorer_heartbeat")
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass

if __name__ == "__main__":
    settings = get_settings()
    setup_logging(settings.env, settings.log_level, "scoring-worker")
    app = WorkerApp("scoring-worker", scorer_main)
    asyncio.run(app.run())