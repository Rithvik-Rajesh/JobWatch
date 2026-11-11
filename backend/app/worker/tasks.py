"""Worker tasks: per-site scrape runs and the once-a-minute scheduler tick.

Locks: `scrape_site` is deferred with `lock=site:<id>` (never two concurrent runs of one site) and
`queueing_lock=site:<id>` (never two queued runs of one site). Both are per-deferral values, so jobs are
deferred through `defer_scrape_site`.
"""

import os
import random
import socket
from datetime import UTC, datetime, timedelta

from procrastinate.exceptions import AlreadyEnqueued
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_sessionmaker
from app.core.logging import get_logger
from app.discovery.http import PoliteClient
from app.models import CareerSite, WorkerHeartbeat
from app.models.enums import HealthStatus
from app.settings.registry import SchedulerSettings, get_setting
from app.worker.app import app

log = get_logger(__name__)


def worker_name() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


@app.task(name="scrape_site")
async def scrape_site(site_id: int, trigger: str = "schedule", scrape_run_id: int | None = None) -> None:
    from app.discovery import pipeline  # imported lazily: written by another module, patched in tests

    client = PoliteClient()
    try:
        summary = await pipeline.run_site(site_id, trigger=trigger, fetcher=client, scrape_run_id=scrape_run_id)
    finally:
        await client.aclose()
    log.info(
        "scrape_site_done",
        site_id=site_id,
        trigger=trigger,
        status=getattr(summary, "status", None),
        complete=getattr(summary, "complete", None),
        seen=getattr(summary, "postings_seen", None),
        new=getattr(summary, "postings_new", None),
        closed=getattr(summary, "postings_closed", None),
        error_class=getattr(summary, "error_class", None),
        http_requests=client.request_count,
    )


async def defer_scrape_site(site_id: int, trigger: str = "schedule", scrape_run_id: int | None = None) -> bool:
    """Defer a scrape with the per-site locks. Returns False if one is already queued for the site."""
    lock = f"site:{site_id}"
    try:
        await scrape_site.configure(lock=lock, queueing_lock=lock).defer_async(
            site_id=site_id, trigger=trigger, scrape_run_id=scrape_run_id
        )
    except AlreadyEnqueued:
        return False
    return True


async def select_and_schedule_due_sites(
    session: AsyncSession,
    now: datetime,
    settings: SchedulerSettings,
    *,
    rng: random.Random | None = None,
) -> list[int]:
    """Pick due sites (row-locked, SKIP LOCKED), advance their next_check_at, return their ids.
    Does nothing when the scheduler is disabled. Does not commit or defer."""
    if not settings.enabled:
        return []
    rng = rng or random.Random()
    stmt = (
        select(CareerSite)
        .where(
            CareerSite.monitoring_enabled.is_(True),
            CareerSite.archived_at.is_(None),
            CareerSite.health != HealthStatus.blocked,
            (CareerSite.next_check_at.is_(None)) | (CareerSite.next_check_at <= now),
        )
        .order_by(CareerSite.next_check_at.asc().nulls_first(), CareerSite.id)
        .limit(settings.max_concurrent_sites)
        .with_for_update(skip_locked=True)
    )
    sites = (await session.scalars(stmt)).all()
    for site in sites:
        interval = site.check_interval_min or settings.default_interval_min
        jitter = rng.uniform(-settings.jitter_min, settings.jitter_min) if settings.jitter_min else 0.0
        site.next_check_at = now + timedelta(minutes=max(1.0, interval + jitter))
    await session.flush()
    return [s.id for s in sites]


async def write_heartbeat(session: AsyncSession, now: datetime) -> None:
    stmt = insert(WorkerHeartbeat).values(name=worker_name(), beat_at=now, details={"pid": os.getpid()})
    stmt = stmt.on_conflict_do_update(
        index_elements=[WorkerHeartbeat.name], set_={"beat_at": stmt.excluded.beat_at, "details": stmt.excluded.details}
    )
    await session.execute(stmt)


@app.periodic(cron="* * * * *")
@app.task(name="scheduler_tick", queueing_lock="scheduler_tick")
async def scheduler_tick(timestamp: int | None = None) -> None:
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session:
        await write_heartbeat(session, now)
        settings = await get_setting(session, SchedulerSettings)
        site_ids = await select_and_schedule_due_sites(session, now, settings)
        await session.commit()
    for site_id in site_ids:
        await defer_scrape_site(site_id, trigger="schedule")
    if site_ids:
        log.info("scheduler_tick", scheduled=site_ids)
