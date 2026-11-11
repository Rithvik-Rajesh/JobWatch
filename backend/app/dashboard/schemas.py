from datetime import datetime
from typing import Literal

from app.core.schema import APIModel
from app.jobs.schemas import CompanyRef


class DashboardSummary(APIModel):
    """Counts honour the location scope (in-scope + unknown) unless noted."""

    total_discovered: int
    new_today: int
    unreviewed: int
    interested: int
    shortlisted: int
    applied: int  # ever applied (applied_at not null)
    in_process: int  # assessment + interviewing (current)
    offers: int
    rejected: int
    open_jobs: int
    companies_monitored: int
    companies_failing: int
    last_successful_scan_at: datetime | None


class TimeseriesPoint(APIModel):
    bucket: str  # ISO date of bucket start (user timezone)
    value: int


class Timeseries(APIModel):
    metric: Literal["discovered", "applied", "status_changes"]
    bucket: Literal["day", "week", "month"]
    points: list[TimeseriesPoint]


class FunnelStage(APIModel):
    key: str  # discovered | interested | shortlisted | applied | in_process | offer
    label: str
    count: int  # number of jobs that EVER reached this stage (from the timeline)
    conversion_from_previous: float | None


class BreakdownItem(APIModel):
    key: str
    label: str
    count: int
    color: str | None = None


class Breakdown(APIModel):
    by: Literal["status", "company", "source"]
    items: list[BreakdownItem]


class UpcomingItem(APIModel):
    kind: Literal["interview", "reminder", "deadline"]
    id: int
    title: str
    at: datetime
    overdue: bool
    job_id: int | None
    company: CompanyRef | None
