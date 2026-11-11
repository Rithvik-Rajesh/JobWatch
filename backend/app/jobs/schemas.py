from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field, HttpUrl

from app.core.schema import APIModel
from app.models.enums import (
    Availability,
    DatePrecision,
    DuplicateState,
    EmploymentType,
    ScopeMatch,
    StatusCategory,
    WorkArrangement,
)
from app.salary.schemas import SalarySummary

JobSort = Literal[
    "discovered_desc",
    "discovered_asc",
    "posted_desc",
    "posted_asc",
    "company_asc",
    "title_asc",
    "status",
    "relevance",
    "updated_desc",
]


class JobFilters(APIModel):
    """Query parameters of GET /jobs. All filters AND together; list values OR within a filter.

    Location scope: when `scope` is omitted the server applies the default visible scopes from
    LocationScopeSettings (India + unknown by default). Pass scope=all to disable.
    """

    q: str | None = None  # full-text (websearch syntax) over title, company, skills, department, description
    company_id: list[int] = Field(default_factory=list)
    status: list[str] = Field(default_factory=list)  # status keys
    status_category: list[StatusCategory] = Field(default_factory=list)
    source: list[str] = Field(default_factory=list)  # source keys
    country: list[str] = Field(default_factory=list)  # ISO alpha-2
    location: str | None = None  # substring match on location_text
    scope: list[ScopeMatch | Literal["all"]] = Field(default_factory=list)
    employment_type: list[EmploymentType] = Field(default_factory=list)
    work_arrangement: list[WorkArrangement] = Field(default_factory=list)
    availability: list[Availability] = Field(default_factory=list)  # default: open + unknown + closed
    has_salary: bool | None = None  # any salary record (employer or estimate)
    salary_min: Decimal | None = None  # annual INR, matched against employer-provided max or estimate max
    posted_after: datetime | None = None
    discovered_after: datetime | None = None
    is_new: bool | None = None  # discovered within the last 24 h
    inbox: bool | None = None  # unreviewed (see /inbox)
    starred: bool | None = None
    tag: list[str] = Field(default_factory=list)
    include_merged: bool = False
    # Experience filter (freshers: max_experience_years=1). Jobs whose requirement couldn't be parsed are kept
    # unless include_unknown_experience=false; titles like intern/graduate/fresher always match.
    max_experience_years: Decimal | None = None
    include_unknown_experience: bool = True
    sort: JobSort = "discovered_desc"


class StatusRef(APIModel):
    id: int
    key: str
    label: str
    category: StatusCategory
    color: str


class CompanyRef(APIModel):
    id: int
    name: str
    slug: str
    logo_url: str | None


class JobListItem(APIModel):
    id: int
    experience_min_years: Decimal | None = None
    experience_max_years: Decimal | None = None
    title: str
    company: CompanyRef
    location_text: str | None
    country_codes: list[str]
    location_scope: ScopeMatch
    employment_type: EmploymentType
    work_arrangement: WorkArrangement
    posted_at: datetime | None
    posted_at_precision: DatePrecision
    first_seen_at: datetime
    last_changed_at: datetime
    availability: Availability
    apply_url: str | None
    sources: list[str]  # source keys
    salary: SalarySummary | None
    status: StatusRef
    application_id: int
    row_version: int
    reviewed: bool
    starred: bool
    is_new: bool  # first_seen_at within 24 h
    is_updated: bool  # meaningful change within 72 h after discovery
    tags: list[str] = Field(default_factory=list)


class FacetValue(APIModel):
    value: str
    label: str
    count: int


class JobFacets(APIModel):
    company: list[FacetValue]
    status: list[FacetValue]
    source: list[FacetValue]
    country: list[FacetValue]
    employment_type: list[FacetValue]
    work_arrangement: list[FacetValue]
    availability: list[FacetValue]
    scope: list[FacetValue]


class PostingOut(APIModel):
    id: int
    source_key: str
    source_name: str
    external_id: str
    requisition_id: str | None
    source_url: str
    apply_url: str | None
    availability: Availability
    first_seen_at: datetime
    last_seen_at: datetime
    closed_at: datetime | None
    is_primary: bool
    version_count: int


class JobDetail(JobListItem):
    department: str | None
    locations: list[dict]
    experience_text: str | None
    experience_min_years: Decimal | None
    experience_max_years: Decimal | None
    skills: list[str]
    qualifications_required: str | None
    qualifications_preferred: str | None
    salary_text: str | None
    posted_at_raw: str | None
    deadline_at: datetime | None
    description_html: str | None  # sanitized
    description_text: str | None
    last_seen_at: datetime
    closed_at: datetime | None
    scope_overridden: bool
    merged_into_job_id: int | None
    postings: list[PostingOut]
    pending_duplicate_count: int


class VersionOut(APIModel):
    id: int
    posting_id: int
    version_no: int
    captured_at: datetime
    title: str
    changed_fields: list[str]


class FieldDiff(APIModel):
    field: str
    before: str | None
    after: str | None


class VersionDiff(APIModel):
    version: VersionOut
    previous_version_id: int | None
    diffs: list[FieldDiff]


class ManualJobCreate(APIModel):
    """Manual entry. Company is matched by id, or by name/alias (created unmonitored if unknown)."""

    company_id: int | None = None
    company_name: str | None = Field(default=None, min_length=1)
    title: str = Field(min_length=1, max_length=300)
    source_url: HttpUrl | None = None  # original posting URL if any
    apply_url: HttpUrl | None = None
    location_text: str | None = None
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    employment_type: EmploymentType = EmploymentType.unknown
    work_arrangement: WorkArrangement = WorkArrangement.unknown
    department: str | None = None
    description: str | None = None  # plain text or markdown
    salary_text: str | None = None  # employer-provided text only
    posted_at: datetime | None = None
    deadline_at: datetime | None = None
    origin_platform: str | None = None  # free text, e.g. "LinkedIn", "Naukri", "referral"
    initial_status: str | None = None  # status key; default 'new'


class CaptureRequest(APIModel):
    """Payload sent by the "Save to JobWatch" bookmarklet from the page the user is viewing."""

    page_url: HttpUrl
    page_title: str | None = None
    json_ld: list[dict] | None = None  # every <script type="application/ld+json"> block, parsed
    selected_text: str | None = None
    meta: dict[str, str] = Field(default_factory=dict)  # og:title, og:description, etc.
    initial_status: str | None = None


class CaptureResult(APIModel):
    job_id: int
    created: bool  # False when the URL was already captured
    title: str
    company_name: str
    needs_review: list[str]  # fields that could not be extracted, e.g. ['company', 'location']
    app_url: str  # deep link into JobWatch


class JobUpdate(APIModel):
    """Editable fields. Only manual/capture jobs allow content edits; scope override applies to all."""

    title: str | None = Field(default=None, min_length=1, max_length=300)
    location_text: str | None = None
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    employment_type: EmploymentType | None = None
    work_arrangement: WorkArrangement | None = None
    department: str | None = None
    description: str | None = None
    apply_url: HttpUrl | None = None
    deadline_at: datetime | None = None
    location_scope_override: ScopeMatch | None = None  # set to force in/out of scope
    clear_scope_override: bool = False


class DuplicateSuggestionOut(APIModel):
    id: int
    job_a: JobListItem
    job_b: JobListItem
    score: float
    reasons: list[str]
    state: DuplicateState
    created_at: datetime


class MergeRequest(APIModel):
    """Merge `other_job_id` INTO the path job. Postings move; the other job becomes a tombstone.
    Application data: the survivor keeps its status unless `take_status_from_other` is true; notes,
    contacts, interviews, reminders and timeline events of the other job are re-parented."""

    other_job_id: int
    take_status_from_other: bool = False


class ReviewRequest(APIModel):
    job_ids: list[int] = Field(min_length=1, max_length=500)


class InboxSummary(APIModel):
    unreviewed: int
    unreviewed_today: int
    by_company: list[FacetValue]
