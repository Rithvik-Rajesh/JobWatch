from datetime import datetime
from typing import Any

from pydantic import Field, HttpUrl

from app.core.schema import APIModel
from app.models.enums import ExtractorKind, HealthStatus, IndiaCoverage, RunStatus, SupportClass


class CareerSiteCreate(APIModel):
    label: str = Field(default="main", pattern=r"^[A-Za-z0-9_. -]{1,60}$")
    platform: ExtractorKind
    config: dict[str, Any]  # validated against app.discovery.configs.CONFIG_MODELS[platform]
    careers_url: HttpUrl | None = None
    monitoring_enabled: bool = False
    check_interval_min: int | None = Field(default=None, ge=10, le=1440)  # None -> scheduler default


class CareerSiteUpdate(APIModel):
    label: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_. -]{1,60}$")
    config: dict[str, Any] | None = None
    careers_url: HttpUrl | None = None
    monitoring_enabled: bool | None = None  # enabling a 'blocked' site requires POST /resume first (409)
    check_interval_min: int | None = Field(default=None, ge=10, le=1440)
    clear_check_interval: bool = False


class CareerSiteOut(APIModel):
    id: int
    company_id: int
    company_name: str
    label: str
    platform: ExtractorKind
    config: dict[str, Any]
    careers_url: str | None
    monitoring_enabled: bool
    check_interval_min: int | None
    effective_interval_min: int  # site override or scheduler default
    next_check_at: datetime | None
    health: HealthStatus
    consecutive_failures: int
    last_checked_at: datetime | None
    last_success_at: datetime | None
    last_error: str | None
    paused_reason: str | None
    open_jobs_count: int
    robots_status: str | None
    robots_checked_at: datetime | None
    support_class: SupportClass
    india_coverage: IndiaCoverage
    verification_status: str | None
    verified_at: datetime | None
    evidence: dict[str, Any]
    adapter_available: bool  # an adapter is registered and importable for this platform/module
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None


class DryRunSample(APIModel):
    title: str
    location: str | None
    scope: str
    reason: str
    filtered: bool  # removed by title filters
    url: str


class DryRunResult(APIModel):
    """POST /sites/{id}/test — first page(s) only, nothing written."""

    career_site_id: int
    ok: bool
    complete: bool
    postings_seen: int
    postings_in_scope: int
    postings_unknown_scope: int
    postings_out_of_scope: int
    postings_filtered: int
    error_class: str | None
    error_message: str | None
    robots_status: str | None
    samples: list[DryRunSample]
    diagnostics: dict = Field(default_factory=dict)


class SchedulerStatus(APIModel):
    enabled: bool  # settings.scheduler.enabled (global switch; OFF until rollout approval)
    default_interval_min: int
    sites_total: int
    sites_monitored: int
    sites_due: int
    sites_blocked: int
    runs_queued_or_running: int
    worker_heartbeat_at: datetime | None


class RunRef(APIModel):
    id: int
    status: RunStatus
    queued_at: datetime
