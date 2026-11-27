"""On-demand fetch (simple mode): scrape the selected companies' automatable career sites once, in the API process.

Each site is a normal pipeline run, so robots.txt, rate limits, the India gate and the "failed runs never close
jobs" rules all apply. Sites classified not_reliable/manual, blocked, archived or without an adapter are skipped.
"""

import asyncio
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import func, select

from app.core.auth import CurrentUser
from app.core.db import get_sessionmaker
from app.core.errors import Conflict, ValidationFailed
from app.core.logging import get_logger
from app.models import CareerSite, Company, Job
from app.models.enums import Availability, HealthStatus, SupportClass

log = get_logger(__name__)
router = APIRouter(tags=["fetch"])
PARALLEL_SITES = 6


def _fresh_state() -> dict[str, Any]:
    return {
        "running": False,
        "started_at": None,
        "finished_at": None,
        "total": 0,  # sites in this fetch
        "done": 0,
        "new_jobs": 0,
        "jobs_found": 0,
        "jobs_classified": 0,
        "ai_checked": 0,
        "rules_checked": 0,
        "stored_jobs": 0,
        "india_jobs": 0,
        "current": [],  # sites being scraped right now
        "pending": [],  # sites waiting
        "sites": [],  # finished: {site, company, status, in_scope, new, error}
        "activity": [],  # most recent per-job pipeline events for the live UI
    }


_state: dict[str, Any] = _fresh_state()
_task: asyncio.Task | None = None


def _record_progress(site: str, event: dict[str, Any]) -> None:
    stage = event.get("stage")
    if stage == "listed":
        _state["jobs_found"] += int(event.get("count") or 0)
    elif stage == "classified":
        _state["jobs_classified"] += 1
        provider = str(event.get("provider") or "")
        if provider.startswith("rules:"):
            _state["rules_checked"] += 1
        elif provider.startswith(("ollama:", "gemini:")):
            _state["ai_checked"] += 1
    elif stage == "stored":
        _state["stored_jobs"] += 1

    if stage in {"fetching_jd", "classifying", "classified", "stored"}:
        item = {"site": site, "at": datetime.now(UTC).isoformat(), **event}
        _state["activity"].append(item)
        _state["activity"] = _state["activity"][-40:]


async def _eligible(company_ids: list[int] | None = None) -> list[dict[str, Any]]:
    from app.sites.service import adapter_available

    conds = [
        CareerSite.archived_at.is_(None),
        Company.archived_at.is_(None),
        CareerSite.support_class.in_([SupportClass.auto, SupportClass.adapter]),
        CareerSite.health != HealthStatus.blocked,
    ]
    if company_ids:
        conds.append(Company.id.in_(company_ids))
    async with get_sessionmaker()() as s:
        rows = (
            await s.execute(
                select(CareerSite, Company.id, Company.name)
                .join(Company, Company.id == CareerSite.company_id)
                .where(*conds)
                .order_by(Company.name, CareerSite.label)
            )
        ).all()
    return [
        {
            "site_id": site.id,
            "company_id": cid,
            "company": name,
            "label": name if site.label == "main" else f"{name} · {site.label}",
            "platform": site.platform.value,
            "last_success_at": site.last_success_at,
            "last_error": site.last_error,
        }
        for site, cid, name in rows
        if adapter_available(site.platform, site.config)
    ]


async def _run(sites: list[dict[str, Any]]) -> None:
    from app.discovery.http import PoliteClient
    from app.discovery.pipeline import run_site

    sem = asyncio.Semaphore(PARALLEL_SITES)
    async with PoliteClient() as client:

        async def one(site: dict[str, Any]) -> None:
            async with sem:
                label = site["label"]
                _state["pending"].remove(label)
                _state["current"].append(label)
                try:
                    r = await run_site(
                        site["site_id"],
                        trigger="manual",
                        fetcher=client,
                        progress=lambda event: _record_progress(label, event),
                    )
                    entry = {
                        "status": r.status,
                        "in_scope": r.postings_in_scope,
                        "new": r.postings_new,
                        "error": r.error_message,
                    }
                except Exception as e:  # never let one site stop the batch
                    log.exception("fetch_site_failed", site_id=site["site_id"])
                    entry = {"status": "failed", "in_scope": 0, "new": 0, "error": str(e)[:200]}
                _state["current"].remove(label)
                _state["sites"].append({"site": label, "company": site["company"], **entry})
                _state["done"] += 1
                _state["new_jobs"] += entry["new"]
                _state["india_jobs"] += entry["in_scope"]

        await asyncio.gather(*(one(s) for s in sites))


async def _runner(sites: list[dict[str, Any]]) -> None:
    try:
        await _run(sites)
    finally:
        _state.update(running=False, current=[], pending=[], finished_at=datetime.now(UTC).isoformat())


class FetchRequest(BaseModel):
    company_ids: list[int] | None = None  # None/empty = all scrapable companies


@router.get("/fetch/companies")
async def scrapable_companies(user: CurrentUser) -> list[dict[str, Any]]:
    """Companies that can be scraped automatically, with open India job counts and last fetch info."""
    sites = await _eligible()
    async with get_sessionmaker()() as s:
        counts = dict(
            (
                await s.execute(
                    select(Job.company_id, func.count())
                    .where(Job.merged_into_job_id.is_(None), Job.availability == Availability.open)
                    .group_by(Job.company_id)
                )
            ).all()
        )
    out: dict[int, dict[str, Any]] = {}
    for site in sites:
        c = out.setdefault(
            site["company_id"],
            {
                "id": site["company_id"],
                "name": site["company"],
                "platform": site["platform"],
                "sites": 0,
                "jobs": counts.get(site["company_id"], 0),
                "last_success_at": None,
                "last_error": None,
            },
        )
        c["sites"] += 1
        if site["last_success_at"] and (not c["last_success_at"] or site["last_success_at"] > c["last_success_at"]):
            c["last_success_at"] = site["last_success_at"]
        c["last_error"] = c["last_error"] or site["last_error"]
    return sorted(out.values(), key=lambda c: c["name"].lower())


@router.post("/fetch-all", status_code=202)
async def start_fetch(user: CurrentUser, body: FetchRequest | None = None) -> dict[str, Any]:
    global _task, _state
    if _state["running"]:
        raise Conflict("A fetch is already running")
    sites = await _eligible((body.company_ids if body else None) or None)
    if not sites:
        raise ValidationFailed("No scrapable sites for the selected companies")
    _state = _fresh_state()
    _state.update(
        running=True, started_at=datetime.now(UTC).isoformat(), total=len(sites), pending=[s["label"] for s in sites]
    )
    _task = asyncio.create_task(_runner(sites))
    return _state


@router.get("/fetch-all")
async def fetch_status(user: CurrentUser) -> dict[str, Any]:
    return _state
