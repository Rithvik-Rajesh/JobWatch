"""Dashboard API (read-only aggregates). Owner: insights agent."""

from typing import Literal

from fastapi import APIRouter, Query

from app.core.auth import CurrentUser
from app.core.db import SessionDep
from app.core.errors import todo
from app.dashboard.schemas import Breakdown, DashboardSummary, FunnelStage, Timeseries, UpcomingItem

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/summary", response_model=DashboardSummary)
async def summary(session: SessionDep, user: CurrentUser):
    raise todo()


@router.get("/timeseries", response_model=Timeseries)
async def timeseries(
    session: SessionDep,
    user: CurrentUser,
    metric: Literal["discovered", "applied", "status_changes"] = "applied",
    bucket: Literal["day", "week", "month"] = "week",
    days: int = Query(default=90, ge=7, le=730),
):
    raise todo()


@router.get("/funnel", response_model=list[FunnelStage])
async def funnel(session: SessionDep, user: CurrentUser, days: int | None = Query(default=None, ge=1, le=730)):
    raise todo()


@router.get("/breakdown", response_model=Breakdown)
async def breakdown(
    session: SessionDep,
    user: CurrentUser,
    by: Literal["status", "company", "source"] = "status",
    limit: int = Query(default=15, ge=1, le=100),
):
    raise todo()


@router.get("/upcoming", response_model=list[UpcomingItem])
async def upcoming(session: SessionDep, user: CurrentUser, days: int = Query(default=14, ge=1, le=90)):
    """Interviews, pending reminders and application deadlines (tracked jobs), incl. overdue."""
    raise todo()
