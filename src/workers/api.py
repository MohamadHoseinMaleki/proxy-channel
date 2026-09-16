"""``mtproto-api`` -- independent read-only ranking HTTP process.

Not a discovery/tester/scorer worker. Uvicorn owns SIGINT/SIGTERM; this
module does not install :class:`~core.lifecycle.WorkerLifecycle` handlers
(D-006 would race uvicorn's shutdown). Database lifetime is the FastAPI
lifespan in :func:`modules.api.create_app`.

Run with::

    uv run mtproto-api
    # or
    uv run python -m workers.api
"""

from __future__ import annotations

import uvicorn

from core.config import get_settings
from core.logger import bind_worker_context, configure_logging
from modules.api.app import PROCESS_NAME, create_app

__all__ = ["PROCESS_NAME", "main"]


def main() -> None:
    """Console-script entrypoint for the ranking API process."""
    settings = get_settings()
    configure_logging(settings)
    bind_worker_context(PROCESS_NAME)
    app = create_app(settings=settings)
    uvicorn.run(
        app,
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,
        access_log=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()
