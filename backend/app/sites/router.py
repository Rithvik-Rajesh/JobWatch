"""Career sites API (Phase 3). Owner: pipeline agent. Paths/models are the frozen contract.

Scraping actions require a registered adapter. `scrape` enqueues a single manual run of ONE site; the
global scheduler (settings.scheduler.enabled) stays off until the user approves the rollout.
"""

from typing import Annotated

from fastapi import APIRouter, Query

from app.companies.schemas import ScrapeRunOut
from app.core.auth import CurrentUser
from app.core.db import SessionDep
from app.core.pagination import Page, PageDep
from app.models.enums import ExtractorKind, HealthStatus, IndiaCoverage, SupportClass
from app.sites import service
from app.sites.schemas import CareerSiteCreate, CareerSiteOut, CareerSiteUpdate, DryRunResult, SchedulerStatus

router = APIRouter(tags=["sites"])


@router.get("/sites", response_model=Page[CareerSiteOut])
async def list_sites(
    session: SessionDep,
    user: CurrentUser,
    paging: PageDep,
    company_id: int | None = None,
    platform: Annotated[list[ExtractorKind], Query()] = [],
    health: Annotated[list[HealthStatus], Query()] = [],
    support_class: Annotated[list[SupportClass], Query()] = [],
    india_coverage: Annotated[list[IndiaCoverage], Query()] = [],
    monitoring: bool | None = None,
    include_archived: bool = False,
):
    return await service.list_sites(
        session,
        paging,
        company_id=company_id,
        platform=platform,
        health=health,
        support_class=support_class,
        india_coverage=india_coverage,
        monitoring=monitoring,
        include_archived=include_archived,
    )


@router.get("/companies/{company_id}/sites", response_model=list[CareerSiteOut])
async def company_sites(company_id: int, session: SessionDep, user: CurrentUser):
    return await service.company_sites(session, company_id)


@router.post("/companies/{company_id}/sites", response_model=CareerSiteOut, status_code=201)
async def create_site(company_id: int, body: CareerSiteCreate, session: SessionDep, user: CurrentUser):
    return await service.create_site(session, company_id, body)


@router.get("/sites/{site_id}", response_model=CareerSiteOut)
async def get_site(site_id: int, session: SessionDep, user: CurrentUser):
    return await service.get_site(session, site_id)


@router.patch("/sites/{site_id}", response_model=CareerSiteOut)
async def update_site(site_id: int, body: CareerSiteUpdate, session: SessionDep, user: CurrentUser):
    return await service.update_site(session, site_id, body)


@router.post("/sites/{site_id}/archive", response_model=CareerSiteOut)
async def archive_site(site_id: int, session: SessionDep, user: CurrentUser):
    return await service.archive_site(session, site_id)


@router.post("/sites/{site_id}/resume", response_model=CareerSiteOut)
async def resume_site(site_id: int, session: SessionDep, user: CurrentUser):
    """Clear an automatic pause (blocked/robots) after the user has reviewed it. Robots is re-checked on
    the next run; if still disallowed the site is paused again."""
    return await service.resume_site(session, site_id)


@router.post("/sites/{site_id}/test", response_model=DryRunResult)
async def test_site(site_id: int, session: SessionDep, user: CurrentUser):
    """Dry run: first page(s) only, India gate + title filters, nothing written. Respects robots/blocks."""
    return await service.test_site(session, site_id)


@router.post("/sites/{site_id}/scrape", response_model=ScrapeRunOut, status_code=202)
async def scrape_site(site_id: int, session: SessionDep, user: CurrentUser):
    """Enqueue one manual run. Idempotent while a run is queued/running. 409 if blocked/archived/no adapter."""
    return await service.scrape_site(session, site_id)


@router.get("/sites/{site_id}/runs", response_model=Page[ScrapeRunOut])
async def site_runs(site_id: int, session: SessionDep, user: CurrentUser, paging: PageDep):
    return await service.site_runs(session, site_id, paging)


@router.get("/scheduler", response_model=SchedulerStatus)
async def scheduler_status(session: SessionDep, user: CurrentUser):
    return await service.scheduler_status(session)
