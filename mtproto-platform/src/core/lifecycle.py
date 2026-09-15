import asyncio
import signal
from typing import Awaitable, Callable

import structlog

logger = structlog.get_logger()


class WorkerApp:
    """
    Minimal abstraction to handle application lifecycle and graceful shutdown 
    via OS signals (SIGINT, SIGTERM).
    """

    def __init__(self, name: str, main_func: Callable[[asyncio.Event], Awaitable[None]]):
        self.name = name
        self.main_func = main_func
        self.shutdown_event = asyncio.Event()

    def _handle_signal(self) -> None:
        logger.info("received_signal", action="initiating_shutdown")
        self.shutdown_event.set()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        
        # Register graceful shutdown signals
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._handle_signal)
            except NotImplementedError:
                # Fallback for platforms like Windows during local dev
                pass

        logger.info("worker_starting")
        try:
            await self.main_func(self.shutdown_event)
        except asyncio.CancelledError:
            logger.info("worker_cancelled")
        except Exception as e:
            logger.exception("worker_crashed", exc_info=e)
            raise
        finally:
            logger.info("worker_stopped")
            