import asyncio
from src.core.config import get_settings
from src.core.logger import setup_logging
from src.core.lifecycle import WorkerApp
import structlog

logger = structlog.get_logger()

async def tester_main(shutdown_event: asyncio.Event) -> None:
    while not shutdown_event.is_set():
        logger.debug("tester_heartbeat")
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass

if __name__ == "__main__":
    settings = get_settings()
    setup_logging(settings.env, settings.log_level, "tester-worker")
    app = WorkerApp("tester-worker", tester_main)
    asyncio.run(app.run())