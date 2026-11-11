"""Ingest contract — CENTRAL. Every source (career-site extractors, Adzuna, email alerts, bookmarklet
capture, manual entry) produces `PostingInput` and calls the ingest service. No other code path writes
postings / jobs / job_versions.

Implementation lives in app/discovery/ingest.py (Phase 2 agent) and must satisfy the invariants
documented in docs/phase1/02-database-schema.md §3 (as amended in docs/phase2/decisions.md).
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import DatePrecision, EmploymentType, ScopeMatch, WorkArrangement


class LocationInput(BaseModel):
    raw: str
    city: str | None = None
    region: str | None = None
    country_code: str | None = Field(default=None, min_length=2, max_length=2)  # ISO alpha-2


class PostingInput(BaseModel):
    """A single listing as observed on a source, already mapped to JobWatch fields.

    Sources fill what they know; the ingest service runs the normalizers for anything left empty
    (e.g. parses `locations` from `location_text`, infers work arrangement from text).
    """

    source_key: str  # sources.key, e.g. 'career_site', 'manual', 'capture', 'adzuna'
    company_id: int
    career_site_id: int | None = None  # REQUIRED for source 'career_site' (misses are evaluated per portal)
    external_id: str = Field(min_length=1)  # stable id on that source
    requisition_id: str | None = None  # employer req id, e.g. Workday 'JR2029481'
    source_url: HttpUrl
    apply_url: HttpUrl | None = None

    title: str = Field(min_length=1)
    department: str | None = None
    location_text: str | None = None
    locations: list[LocationInput] | None = None
    employment_type: EmploymentType | None = None
    work_arrangement: WorkArrangement | None = None
    experience_text: str | None = None
    experience_min_years: Decimal | None = None
    experience_max_years: Decimal | None = None
    skills: list[str] | None = None
    qualifications_required: str | None = None
    qualifications_preferred: str | None = None
    salary_text: str | None = None  # verbatim employer-provided compensation text
    posted_at: datetime | None = None
    posted_at_precision: DatePrecision = DatePrecision.unknown
    posted_at_raw: str | None = None  # e.g. "Posted 3 Days Ago"
    deadline_at: datetime | None = None
    description_html: str | None = None  # untrusted; ingest sanitizes
    description_text: str | None = None
    listing_fingerprint: str | None = None
    raw_payload: dict[str, Any] | None = None
    # India-gate decision computed by the pipeline from structured evidence. When given, ingest stores it
    # (jobs.location_scope / scope_reason) instead of re-deriving scope from location text.
    scope: ScopeMatch | None = None
    scope_reason: str | None = None


@dataclass
class IngestOutcome:
    job_id: int
    posting_id: int
    created_job: bool
    created_posting: bool
    new_version: bool
    changed_fields: list[str] = field(default_factory=list)
    reopened: bool = False
    attached_to_existing_job: bool = False  # strong-identity cross-source match (req id / apply URL)
    duplicate_suggestion_ids: list[int] = field(default_factory=list)


@dataclass
class MissesOutcome:
    closed_posting_ids: list[int] = field(default_factory=list)
    closed_job_ids: list[int] = field(default_factory=list)
    misses_recorded: int = 0


class IngestService(Protocol):
    async def ingest(
        self,
        session: AsyncSession,
        data: PostingInput,
        *,
        seen_at: datetime | None = None,
        scrape_run_id: int | None = None,
    ) -> IngestOutcome:
        """Idempotent upsert of one posting (see invariants)."""
        ...

    async def apply_misses(
        self,
        session: AsyncSession,
        *,
        source_key: str,
        company_id: int,
        seen_external_ids: set[str],
        career_site_id: int | None = None,  # REQUIRED for 'career_site': only that portal's postings count
        run_complete: bool,
        run_trusted: bool,
        now: datetime | None = None,
    ) -> MissesOutcome:
        """Record absences after a run. No-op unless run_complete AND run_trusted (succeeded, not
        quarantined). Closes a posting after >= 2 recorded misses spanning >= 6 hours."""
        ...


# ───────────────────────────── Phase 3: India gate & pipeline contracts ─────────────────────────────


@dataclass(frozen=True)
class ScopeDecision:
    """Result of the India gate (app/discovery/india.py :: evaluate_listing)."""

    scope: ScopeMatch
    reason: str  # human-readable evidence, e.g. "country IN (structured)", "city: Pune", "remote: India listed"


@dataclass
class RunSummary:
    """Returned by app/discovery/pipeline.py :: run_site(site_id, *, trigger, fetcher, now=None, dry_run=False).

    dry_run=True: list() first page(s) only, India gate + title filters applied, NOTHING written (no run row,
    no postings); used by POST /sites/{id}/test.
    """

    career_site_id: int
    scrape_run_id: int | None
    status: str  # RunStatus value
    complete: bool
    postings_seen: int = 0
    postings_in_scope: int = 0
    postings_unknown_scope: int = 0
    postings_out_of_scope: int = 0
    postings_filtered: int = 0
    postings_new: int = 0
    postings_updated: int = 0
    postings_closed: int = 0
    postings_reopened: int = 0
    details_fetched: int = 0
    error_class: str | None = None
    error_message: str | None = None
    samples: list[dict[str, Any]] = field(default_factory=list)  # dry-run: [{title, location, scope, reason}]
