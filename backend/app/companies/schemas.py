from datetime import datetime

from pydantic import Field, HttpUrl

from app.core.schema import APIModel
from app.discovery.configs import ExtractorConfig
from app.models.enums import ExtractorKind, HealthStatus, RunStatus, RunTrigger


class CompanyBase(APIModel):
    name: str = Field(min_length=1, max_length=200)
    website_url: HttpUrl | None = None
    careers_url: HttpUrl | None = None
    logo_url: HttpUrl | None = None
    description: str | None = None
    notes: str | None = None
    monitoring_enabled: bool = False
    check_interval_min: int = Field(default=30, ge=10, le=1440)


class CompanyCreate(CompanyBase):
    slug: str | None = Field(default=None, pattern=r"^[a-z0-9-]+$")  # derived from name when omitted
    extractor_config: ExtractorConfig | None = None  # None -> extractor 'none'
    aliases: list[str] = Field(default_factory=list)


class CompanyUpdate(APIModel):
    """PATCH semantics: only provided fields change."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    website_url: HttpUrl | None = None
    careers_url: HttpUrl | None = None
    logo_url: HttpUrl | None = None
    description: str | None = None
    notes: str | None = None
    monitoring_enabled: bool | None = None
    check_interval_min: int | None = Field(default=None, ge=10, le=1440)
    extractor_config: ExtractorConfig | None = None
    clear_extractor: bool = False
    aliases: list[str] | None = None


class CompanyOut(APIModel):
    id: int
    name: str
    slug: str
    website_url: str | None
    careers_url: str | None
    logo_url: str | None
    description: str | None
    notes: str | None
    monitoring_enabled: bool
    extractor: ExtractorKind
    extractor_config: dict
    check_interval_min: int
    next_check_at: datetime | None
    health: HealthStatus
    consecutive_failures: int
    last_checked_at: datetime | None
    last_success_at: datetime | None
    last_error: str | None
    open_jobs_count: int
    aliases: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None


class CompanyListItem(APIModel):
    id: int
    name: str
    slug: str
    logo_url: str | None
    careers_url: str | None
    monitoring_enabled: bool
    extractor: ExtractorKind
    health: HealthStatus
    open_jobs_count: int
    tracked_jobs_count: int = 0  # jobs with status beyond 'new'
    last_success_at: datetime | None
    last_error: str | None
    archived_at: datetime | None


class CsvImportRow(APIModel):
    row_number: int
    name: str | None
    careers_url: str | None
    website_url: str | None
    action: str  # 'create' | 'update' | 'skip' | 'error'
    detected_extractor: ExtractorKind = ExtractorKind.none
    message: str | None = None
    existing_company_id: int | None = None


class CsvImportPreview(APIModel):
    token: str  # pass to commit; preview is stored server-side for 30 min
    rows: list[CsvImportRow]
    counts: dict[str, int]


class CsvImportResult(APIModel):
    created: int
    updated: int
    skipped: int
    errors: int
    company_ids: list[int]


class ScrapeRunOut(APIModel):
    id: int
    company_id: int | None
    source_id: int | None
    trigger: RunTrigger
    status: RunStatus
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None
    complete: bool
    postings_seen: int
    postings_in_scope: int
    postings_new: int
    postings_updated: int
    postings_closed: int
    postings_reopened: int
    http_requests: int
    error_class: str | None
    error_message: str | None
    warnings: list[dict]


class DiscoveryHistoryPoint(APIModel):
    date: str  # YYYY-MM-DD (user timezone)
    discovered: int
    closed: int
