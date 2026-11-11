"""Canonical jobs, per-source postings, versions, duplicate suggestions and salary records."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, IdMixin, TimestampMixin
from app.models.enums import (
    Availability,
    DatePrecision,
    DuplicateState,
    EmploymentType,
    MatchQuality,
    SalaryPeriod,
    SalaryProvenance,
    ScopeMatch,
    WorkArrangement,
)
from app.models.types import pg_enum

TEXT_ARRAY = ARRAY(Text)


class Job(IdMixin, TimestampMixin, Base):
    """The canonical opportunity. Fields below `primary_posting_id` are a denormalized *current view*
    maintained exclusively by the ingest service (or the manual-entry service for manual jobs)."""

    __tablename__ = "jobs"

    company_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("companies.id"))
    primary_posting_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("postings.id", use_alter=True, deferrable=True, initially="DEFERRED"),
    )

    title: Mapped[str] = mapped_column(Text)
    title_normalized: Mapped[str] = mapped_column(Text)
    department: Mapped[str | None] = mapped_column(Text)
    location_text: Mapped[str | None] = mapped_column(Text)
    locations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    country_codes: Mapped[list[str]] = mapped_column(TEXT_ARRAY, server_default=text("'{}'"))
    # Result of the single LocationScope policy (app.jobs.scope). The ONLY field UI filters,
    # inbox and notifications use to decide "is this an India job".
    location_scope: Mapped[ScopeMatch] = mapped_column(
        pg_enum(ScopeMatch, "scope_match"), server_default=ScopeMatch.unknown.value
    )
    scope_overridden: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    employment_type: Mapped[EmploymentType] = mapped_column(
        pg_enum(EmploymentType, "employment_type"), server_default=EmploymentType.unknown.value
    )
    work_arrangement: Mapped[WorkArrangement] = mapped_column(
        pg_enum(WorkArrangement, "work_arrangement"), server_default=WorkArrangement.unknown.value
    )
    experience_min_years: Mapped[Decimal | None] = mapped_column(Numeric(4, 1))
    experience_max_years: Mapped[Decimal | None] = mapped_column(Numeric(4, 1))
    skills: Mapped[list[str]] = mapped_column(TEXT_ARRAY, server_default=text("'{}'"))
    description_text: Mapped[str | None] = mapped_column(Text)  # current plain text, for search

    # Posting date as published by the source. Absolute dates are preserved and never overwritten
    # by an estimate; `first_seen_at` is independent (when JobWatch discovered it).
    posted_at: Mapped[datetime | None]
    posted_at_precision: Mapped[DatePrecision] = mapped_column(
        pg_enum(DatePrecision, "date_precision"), server_default=DatePrecision.unknown.value
    )
    posted_at_raw: Mapped[str | None] = mapped_column(Text)
    deadline_at: Mapped[datetime | None]
    apply_url: Mapped[str | None] = mapped_column(Text)
    has_employer_salary: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))

    availability: Mapped[Availability] = mapped_column(
        pg_enum(Availability, "availability"), server_default=Availability.open.value
    )
    first_seen_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    last_seen_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    closed_at: Mapped[datetime | None]
    last_changed_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    merged_into_job_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("jobs.id"))
    search_vector: Mapped[str | None] = mapped_column(TSVECTOR)  # maintained by DB trigger

    __table_args__ = (
        Index("ix_jobs_company_active", "company_id", postgresql_where=text("merged_into_job_id is null")),
        Index(
            "ix_jobs_first_seen_active", text("first_seen_at desc"), postgresql_where=text("merged_into_job_id is null")
        ),
        Index("ix_jobs_posted_at", text("posted_at desc nulls last")),
        Index("ix_jobs_availability", "availability"),
        Index("ix_jobs_location_scope", "location_scope"),
        Index("ix_jobs_country_codes", "country_codes", postgresql_using="gin"),
        Index("ix_jobs_skills", "skills", postgresql_using="gin"),
        Index("ix_jobs_search_vector", "search_vector", postgresql_using="gin"),
        Index(
            "ix_jobs_title_trgm",
            "title_normalized",
            postgresql_using="gin",
            postgresql_ops={"title_normalized": "gin_trgm_ops"},
        ),
    )


class Posting(IdMixin, TimestampMixin, Base):
    """One listing of a job on one source. Identity = (source, company, external_id)."""

    __tablename__ = "postings"

    job_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("jobs.id"))
    source_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("sources.id"))
    company_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("companies.id"))
    external_id: Mapped[str] = mapped_column(Text)
    # Employer requisition number (e.g. Workday "JR2029481") when known. Equal requisition IDs at the
    # same company are the ONLY automatic cross-source merge signal besides an identical apply URL.
    requisition_id: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(Text)
    apply_url: Mapped[str | None] = mapped_column(Text)
    canonical_apply_url: Mapped[str | None] = mapped_column(Text)  # normalized (no tracking params)
    listing_fingerprint: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(Text)
    current_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("job_versions.id", use_alter=True, deferrable=True, initially="DEFERRED"),
    )
    availability: Mapped[Availability] = mapped_column(
        pg_enum(Availability, "availability"), server_default=Availability.open.value
    )
    first_seen_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    last_seen_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    detail_fetched_at: Mapped[datetime | None]
    missing_since: Mapped[datetime | None]
    consecutive_misses: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    closed_at: Mapped[datetime | None]

    __table_args__ = (
        UniqueConstraint("source_id", "company_id", "external_id", name="uq_postings_identity"),
        Index("ix_postings_job", "job_id"),
        Index("ix_postings_company_open", "company_id", postgresql_where=text("availability = 'open'")),
        Index(
            "ix_postings_requisition",
            "company_id",
            "requisition_id",
            postgresql_where=text("requisition_id is not null"),
        ),
        Index(
            "ix_postings_canonical_apply_url",
            "canonical_apply_url",
            postgresql_where=text("canonical_apply_url is not null"),
        ),
    )


class JobVersion(IdMixin, Base):
    """Immutable snapshot of a posting's content; written only when the content hash changes."""

    __tablename__ = "job_versions"

    posting_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("postings.id", ondelete="CASCADE"))
    version_no: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(Text)
    captured_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    scrape_run_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("scrape_runs.id", ondelete="SET NULL"))

    title: Mapped[str] = mapped_column(Text)
    department: Mapped[str | None] = mapped_column(Text)
    location_text: Mapped[str | None] = mapped_column(Text)
    locations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    employment_type: Mapped[EmploymentType] = mapped_column(
        pg_enum(EmploymentType, "employment_type"), server_default=EmploymentType.unknown.value
    )
    work_arrangement: Mapped[WorkArrangement] = mapped_column(
        pg_enum(WorkArrangement, "work_arrangement"), server_default=WorkArrangement.unknown.value
    )
    experience_text: Mapped[str | None] = mapped_column(Text)
    experience_min_years: Mapped[Decimal | None] = mapped_column(Numeric(4, 1))
    experience_max_years: Mapped[Decimal | None] = mapped_column(Numeric(4, 1))
    skills: Mapped[list[str]] = mapped_column(TEXT_ARRAY, server_default=text("'{}'"))
    qualifications_required: Mapped[str | None] = mapped_column(Text)
    qualifications_preferred: Mapped[str | None] = mapped_column(Text)
    salary_text: Mapped[str | None] = mapped_column(Text)
    posted_at: Mapped[datetime | None]
    posted_at_precision: Mapped[DatePrecision] = mapped_column(
        pg_enum(DatePrecision, "date_precision"), server_default=DatePrecision.unknown.value
    )
    posted_at_raw: Mapped[str | None] = mapped_column(Text)
    deadline_at: Mapped[datetime | None]
    description_html: Mapped[str | None] = mapped_column(Text)  # sanitized original
    description_text: Mapped[str | None] = mapped_column(Text)
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    changed_fields: Mapped[list[str]] = mapped_column(TEXT_ARRAY, server_default=text("'{}'"))

    __table_args__ = (UniqueConstraint("posting_id", "version_no", name="uq_job_versions_posting_version"),)


class JobTag(Base):
    __tablename__ = "job_tags"

    job_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    tag_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True)


class DuplicateSuggestion(IdMixin, Base):
    """Possible cross-source duplicate. Ambiguous matches are NEVER merged automatically; the user
    confirms (merge) or dismisses. Pair is stored ordered (job_a_id < job_b_id)."""

    __tablename__ = "duplicate_suggestions"

    job_a_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("jobs.id", ondelete="CASCADE"))
    job_b_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("jobs.id", ondelete="CASCADE"))
    score: Mapped[float] = mapped_column(Float)
    reasons: Mapped[list[str]] = mapped_column(TEXT_ARRAY, server_default=text("'{}'"))
    state: Mapped[DuplicateState] = mapped_column(
        pg_enum(DuplicateState, "duplicate_state"), server_default=DuplicateState.pending.value
    )
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    resolved_at: Mapped[datetime | None]

    __table_args__ = (
        UniqueConstraint("job_a_id", "job_b_id", name="uq_duplicate_suggestions_pair"),
        CheckConstraint("job_a_id < job_b_id", name="ordered_pair"),
        Index("ix_duplicate_suggestions_pending", "state", postgresql_where=text("state = 'pending'")),
    )


class SalaryRecord(IdMixin, Base):
    """Every compensation figure, with provenance. Never stored on `jobs`."""

    __tablename__ = "salary_records"

    job_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("jobs.id", ondelete="CASCADE"))
    provenance: Mapped[SalaryProvenance] = mapped_column(pg_enum(SalaryProvenance, "salary_provenance"))
    provider: Mapped[str] = mapped_column(Text)
    is_current: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    currency: Mapped[str | None] = mapped_column(String(3))
    period: Mapped[SalaryPeriod] = mapped_column(
        pg_enum(SalaryPeriod, "salary_period"), server_default=SalaryPeriod.unknown.value
    )
    amount_min: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    amount_max: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    amount_avg: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    amount_typical: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    raw_text: Mapped[str | None] = mapped_column(Text)
    role_queried: Mapped[str | None] = mapped_column(Text)
    role_matched: Mapped[str | None] = mapped_column(Text)
    role_match: Mapped[MatchQuality] = mapped_column(
        pg_enum(MatchQuality, "match_quality"), server_default=MatchQuality.unknown.value
    )
    experience_band: Mapped[str | None] = mapped_column(Text)
    experience_match: Mapped[MatchQuality] = mapped_column(
        pg_enum(MatchQuality, "match_quality"), server_default=MatchQuality.unknown.value
    )
    location: Mapped[str | None] = mapped_column(Text)
    sample_size: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    source_updated_at: Mapped[date | None] = mapped_column(Date)
    retrieved_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    notes: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (
        CheckConstraint(
            "provenance <> 'employer_provided' or provider = 'job_posting'", name="employer_salary_from_posting"
        ),
        CheckConstraint("amount_min is null or amount_max is null or amount_min <= amount_max", name="amount_order"),
        CheckConstraint("confidence is null or confidence in ('high','medium','low')", name="confidence_valid"),
        Index(
            "uq_salary_one_current_per_provider", "job_id", "provider", unique=True, postgresql_where=text("is_current")
        ),
    )
