"""Health API: liveness/readiness (unauthenticated) and scraper health (authenticated).
Owner: insights agent."""

from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import text

from app.companies.schemas import ScrapeRunOut
from app.core.auth import CurrentUser
from app.core.db import SessionDep
from app.core.errors import todo
from app.core.pagination import Page, PageDep
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
    raise todo()


@router.get("/runs", response_model=Page[ScrapeRunOut])
async def runs(
    session: SessionDep,
    user: CurrentUser,
    paging: PageDep,
    status: Annotated[list[RunStatus], Query()] = [],
    company_id: int | None = None,
):
    raise todo()


@router.get("/notifications", response_model=Page[NotificationOut])
async def notifications(session: SessionDep, user: CurrentUser, paging: PageDep, state: str | None = None):
    raise todo()


@router.get("/salary-providers", response_model=list[SalaryProviderOut])
async def salary_providers(session: SessionDep, user: CurrentUser):
    raise todo()
