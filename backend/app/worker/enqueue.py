"""Enqueue helper used by the sites API (manual runs). Signature is a cross-agent contract."""

from app.worker.app import app
from app.worker.tasks import defer_scrape_site


async def enqueue_site_run(site_id: int, scrape_run_id: int, trigger: str = "manual") -> None:
    """Defer `scrape_site` for an existing queued scrape_runs row. If a run is already queued for the
    site (queueing_lock) this is a silent no-op. Opens the queue pool if it is not open yet."""
    connector = app.connector
    opened_here = getattr(connector, "_async_pool", None) is None
    await app.open_async()
    try:
        await defer_scrape_site(site_id, trigger=trigger, scrape_run_id=scrape_run_id)
    finally:
        if opened_here:
            await app.close_async()
