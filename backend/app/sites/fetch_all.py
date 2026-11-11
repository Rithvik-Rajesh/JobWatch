"""On-demand "Fetch all" (simple mode): scrape every automatable career site once, in the API process.

Runs in the background (asyncio task) with a few sites in parallel; each site is a normal pipeline run, so
robots.txt, rate limits, the India gate and the "failed runs never close jobs" rules all still apply.
Sites classified not_reliable/manual, blocked, archived or without an adapter are skipped.
"""

import asyncio
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter
from sqlalchemy import select

from app.core.auth import CurrentUser
from app.core.db import get_sessionmaker
from app.core.errors import Conflict
from app.core.logging import get_logger
from app.models import CareerSite, Company
from app.models.enums import HealthStatus, SupportClass

log = get_logger(__name__)
router = APIRouter(tags=["fetch"])
PARALLEL_SITES = 6

_state: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "total": 0,
    "done": 0,
    "new_jobs": 0,
    "sites": [],
}
_task: asyncio.Task | None = None


async def _eligible_sites() -> list[tuple[int, str]]:
    from app.sites.service import adapter_available

    async with get_sessionmaker()() as s:
        rows = (
            await s.execute(
                select(CareerSite, Company.name)
                .join(Company, Company.id == CareerSite.company_id)
                .where(
                    CareerSite.archived_at.is_(None),
                    CareerSite.support_class.in_([SupportClass.auto, SupportClass.adapter]),
                    CareerSite.health != HealthStatus.blocked,
                )
                .order_by(Company.name)
            )
        ).all()
    return [(site.id, f"{name} ({site.label})") for site, name in rows if adapter_available(site.platform, site.config)]


async def _run_all() -> None:
    from app.discovery.http import PoliteClient
    from app.discovery.pipeline import run_site

    sites = await _eligible_sites()
    _state.update(total=len(sites), done=0, new_jobs=0, sites=[])
    sem = asyncio.Semaphore(PARALLEL_SITES)
    async with PoliteClient() as client:

        async def one(site_id: int, label: str) -> None:
            async with sem:
                try:
                    r = await run_site(site_id, trigger="manual", fetcher=client)
                    entry = {
                        "site": label,
                        "status": r.status,
                        "in_scope": r.postings_in_scope,
                        "new": r.postings_new,
                        "error": r.error_message,
                    }
                    _state["new_jobs"] += r.postings_new
                except Exception as e:  # never let one site stop the batch
                    log.exception("fetch_all_site_failed", site_id=site_id)
                    entry = {"site": label, "status": "failed", "in_scope": 0, "new": 0, "error": str(e)[:200]}
                _state["sites"].append(entry)
                _state["done"] += 1

        await asyncio.gather(*(one(i, label) for i, label in sites))


async def _runner() -> None:
    try:
        await _run_all()
    finally:
        _state.update(running=False, finished_at=datetime.now(UTC).isoformat())


@router.post("/fetch-all", status_code=202)
async def start_fetch_all(user: CurrentUser) -> dict[str, Any]:
    global _task
    if _state["running"]:
        raise Conflict("A fetch is already running")
    _state.update(running=True, started_at=datetime.now(UTC).isoformat(), finished_at=None)
    _task = asyncio.create_task(_runner())
    return _state


@router.get("/fetch-all")
async def fetch_all_status(user: CurrentUser) -> dict[str, Any]:
    return _state
