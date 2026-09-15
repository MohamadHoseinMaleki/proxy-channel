import asyncio
import pytest
from src.core.lifecycle import WorkerApp


async def dummy_main_immediate_exit(shutdown_event: asyncio.Event) -> None:
    """Simulates a worker that sets the shutdown event immediately."""
    shutdown_event.set()


@pytest.mark.asyncio
async def test_worker_lifecycle_clean_exit() -> None:
    """Tests that the WorkerApp starts and stops cleanly via shutdown_event."""
    app = WorkerApp("test-worker", dummy_main_immediate_exit)
    await app.run()
    assert app.shutdown_event.is_set()


@pytest.mark.asyncio
async def test_worker_lifecycle_crash() -> None:
    """Tests that exceptions in the main function bubble up correctly."""
    async def crashing_main(shutdown_event: asyncio.Event) -> None:
        raise ValueError("Simulated crash")

    app = WorkerApp("test-worker", crashing_main)
    with pytest.raises(ValueError, match="Simulated crash"):
        await app.run()