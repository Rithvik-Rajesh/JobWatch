"""`uv run python -m app.worker` — run the Procrastinate worker.

SIGTERM/SIGINT: Procrastinate stops fetching new jobs and waits for running jobs to finish
(shutdown_graceful_timeout=None); each `scrape_site` closes its PoliteClient in a `finally`.
"""

import asyncio
import os

from app.core.logging import configure_logging, get_logger
from app.worker.app import app


async def main() -> None:
    configure_logging()
    log = get_logger("app.worker")
    concurrency = int(os.environ.get("JOBWATCH_WORKER_CONCURRENCY", "4"))
    log.info("worker_starting", concurrency=concurrency)
    async with app.open_async():
        await app.run_worker_async(concurrency=concurrency, wait=True, install_signal_handlers=True)
    log.info("worker_stopped")


if __name__ == "__main__":
    asyncio.run(main())
