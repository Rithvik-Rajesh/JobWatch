"""Health API: liveness/readiness (unauthenticated) and scraper health (authenticated).
Owner: insights agent."""

from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import text

from app.companies.schemas import ScrapeRunOut
from app.core.auth import CurrentUser
from app.core.db import SessionDep
from app.core.pagination import Page, PageDep
from app.health import service
from app.health.schemas import HealthOverview, NotificationOut
from app.models.enums import RunStatus
from app.salary.schemas import SalaryProviderOut

probe_router = APIRouter(tags=["probes"])
router = APIRouter(prefix="/health", tags=["health"])


@probe_router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@probe_router.get("/readyz")
async def readyz(session: SessionDep) -> dict[str, str]:
    await session.execute(text("select 1"))
    return {"status": "ready"}


@router.get("/overview", response_model=HealthOverview)
async def overview(session: SessionDep, user: CurrentUser):
    return await service.overview(session)


@router.get("/runs", response_model=Page[ScrapeRunOut])
async def runs(
    session: SessionDep,
    user: CurrentUser,
    paging: PageDep,
    status: Annotated[list[RunStatus], Query()] = [],
    company_id: int | None = None,
):
    items, total = await service.list_runs(session, paging, statuses=status, company_id=company_id)
    return Page(items=items, page=paging.page, page_size=paging.page_size, total=total)


@router.get("/notifications", response_model=Page[NotificationOut])
async def notifications(session: SessionDep, user: CurrentUser, paging: PageDep, state: str | None = None):
    items, total = await service.list_notifications(session, paging, state=state)
    return Page(items=items, page=paging.page, page_size=paging.page_size, total=total)


@router.get("/salary-providers", response_model=list[SalaryProviderOut])
async def salary_providers(session: SessionDep, user: CurrentUser):
    return await service.salary_providers(session)
