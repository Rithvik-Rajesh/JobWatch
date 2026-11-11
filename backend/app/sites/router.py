"""Career sites API (Phase 3). Owner: pipeline agent. Paths/models are the frozen contract.

Scraping actions require a registered adapter. `scrape` enqueues a single manual run of ONE site; the
global scheduler (settings.scheduler.enabled) stays off until the user approves the rollout.
"""

from typing import Annotated

from fastapi import APIRouter, Query

from app.companies.schemas import ScrapeRunOut
from app.core.auth import CurrentUser
from app.core.db import SessionDep
from app.core.errors import todo
from app.core.pagination import Page, PageDep
from app.models.enums import ExtractorKind, HealthStatus, IndiaCoverage, SupportClass
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
    raise todo()


@router.get("/companies/{company_id}/sites", response_model=list[CareerSiteOut])
async def company_sites(company_id: int, session: SessionDep, user: CurrentUser):
    raise todo()


@router.post("/companies/{company_id}/sites", response_model=CareerSiteOut, status_code=201)
async def create_site(company_id: int, body: CareerSiteCreate, session: SessionDep, user: CurrentUser):
    raise todo()


@router.get("/sites/{site_id}", response_model=CareerSiteOut)
async def get_site(site_id: int, session: SessionDep, user: CurrentUser):
    raise todo()


@router.patch("/sites/{site_id}", response_model=CareerSiteOut)
async def update_site(site_id: int, body: CareerSiteUpdate, session: SessionDep, user: CurrentUser):
    raise todo()


@router.post("/sites/{site_id}/archive", response_model=CareerSiteOut)
async def archive_site(site_id: int, session: SessionDep, user: CurrentUser):
    raise todo()


@router.post("/sites/{site_id}/resume", response_model=CareerSiteOut)
async def resume_site(site_id: int, session: SessionDep, user: CurrentUser):
    """Clear an automatic pause (blocked/robots) after the user has reviewed it. Robots is re-checked on
    the next run; if still disallowed the site is paused again."""
    raise todo()


@router.post("/sites/{site_id}/test", response_model=DryRunResult)
async def test_site(site_id: int, session: SessionDep, user: CurrentUser):
    """Dry run: first page(s) only, India gate + title filters, nothing written. Respects robots/blocks."""
    raise todo()


@router.post("/sites/{site_id}/scrape", response_model=ScrapeRunOut, status_code=202)
async def scrape_site(site_id: int, session: SessionDep, user: CurrentUser):
    """Enqueue one manual run. Idempotent while a run is queued/running. 409 if blocked/archived/no adapter."""
    raise todo()


@router.get("/sites/{site_id}/runs", response_model=Page[ScrapeRunOut])
async def site_runs(site_id: int, session: SessionDep, user: CurrentUser, paging: PageDep):
    raise todo()


@router.get("/scheduler", response_model=SchedulerStatus)
async def scheduler_status(session: SessionDep, user: CurrentUser):
    raise todo()
