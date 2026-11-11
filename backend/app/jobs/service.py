"""Jobs service: detail, manual entry, edits, versions/diffs, inbox review and duplicate listings."""

import json
import re
import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.applications.lifecycle import record_event, status_by_key
from app.core.errors import Conflict, NotFound, NotImplementedYet, ValidationFailed
from app.core.pagination import Page, PageParams
from app.discovery.contracts import LocationInput, PostingInput
from app.discovery.ingest import ingest_service, refresh_job
from app.discovery.normalize import parse_locations
from app.jobs import queries
from app.jobs.schemas import (
    DuplicateSuggestionOut,
    FieldDiff,
    JobDetail,
    JobUpdate,
    ManualJobCreate,
    PostingOut,
    VersionDiff,
    VersionOut,
)
from app.models import (
    Application,
    Company,
    CompanyAlias,
    DuplicateSuggestion,
    Job,
    JobVersion,
    Posting,
    Source,
)
from app.models.enums import Actor, DatePrecision, DuplicateState, EventType

CONTENT_EDITABLE_SOURCES = ("manual", "capture")


# --------------------------------------------------------------------------------------------- helpers
async def _job(session: AsyncSession, job_id: int) -> Job:
    job = await session.get(Job, job_id)
    if job is None:
        raise NotFound(f"Job {job_id} not found", code="job_not_found")
    return job


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "company"


async def resolve_company(
    session: AsyncSession, *, company_id: int | None = None, company_name: str | None = None
) -> Company:
    """By id, else by name/alias (case-insensitive), else create an unmonitored company."""
    if company_id is not None:
        company = await session.get(Company, company_id)
        if company is None:
            raise NotFound(f"Company {company_id} not found", code="company_not_found")
        return company
    name = (company_name or "").strip()
    if not name:
        raise ValidationFailed("Provide company_id or company_name", code="company_required")
    company = await session.scalar(select(Company).where(func.lower(Company.name) == name.lower()).limit(1))
    if company is None:
        company = await session.scalar(
            select(Company)
            .join(CompanyAlias, CompanyAlias.company_id == Company.id)
            .where(func.lower(CompanyAlias.alias) == name.lower())
        )
    if company is not None:
        return company
    base = slugify(name)
    slug, n = base, 1
    while await session.scalar(select(Company.id).where(func.lower(Company.slug) == slug)):
        n += 1
        slug = f"{base}-{n}"
    company = Company(name=name, slug=slug, monitoring_enabled=False)
    session.add(company)
    await session.flush()
    return company


def locations_from(location_text: str | None, country_code: str | None) -> list[LocationInput] | None:
    """Parse location text; an explicit country code fills locations that have none."""
    cc = country_code.upper() if country_code else None
    parsed = parse_locations(location_text)
    if cc:
        if parsed:
            return [loc if loc.country_code else loc.model_copy(update={"country_code": cc}) for loc in parsed]
        return [LocationInput(raw=(location_text or cc).strip(), country_code=cc)]
    return parsed or None


async def change_initial_status(
    session: AsyncSession, job_id: int, status_key: str | None, *, now: datetime | None = None
) -> None:
    if not status_key or status_key == "new":
        return
    try:
        from app.applications.service import change_status
    except ImportError as exc:  # Applications agent's module not present yet
        raise NotImplementedYet("Status changes are not available yet", code="not_implemented") from exc
    await change_status(session, job_id, status_key, actor=Actor.user, now=now)


# --------------------------------------------------------------------------------------------- detail
async def get_job_detail(session: AsyncSession, job_id: int, *, now: datetime | None = None) -> JobDetail:
    now = now or datetime.now(UTC)
    items = await queries.items_for_job_ids(session, [job_id], now=now)
    item = items.get(job_id)
    if item is None:
        raise NotFound(f"Job {job_id} not found", code="job_not_found")
    job = await _job(session, job_id)
    primary_version: JobVersion | None = None
    if job.primary_posting_id:
        primary = await session.get(Posting, job.primary_posting_id)
        if primary and primary.current_version_id:
            primary_version = await session.get(JobVersion, primary.current_version_id)

    version_counts = dict(
        (
            await session.execute(
                select(JobVersion.posting_id, func.count())
                .join(Posting, Posting.id == JobVersion.posting_id)
                .where(Posting.job_id == job_id)
                .group_by(JobVersion.posting_id)
            )
        ).all()
    )
    posting_rows = (
        await session.execute(
            select(Posting, Source).join(Source, Source.id == Posting.source_id).where(Posting.job_id == job_id)
            .order_by(Posting.id)
        )
    ).all()  # fmt: skip
    postings = [
        PostingOut(
            id=p.id,
            source_key=s.key,
            source_name=s.name,
            external_id=p.external_id,
            requisition_id=p.requisition_id,
            source_url=p.source_url,
            apply_url=p.apply_url,
            availability=p.availability,
            first_seen_at=p.first_seen_at,
            last_seen_at=p.last_seen_at,
            closed_at=p.closed_at,
            is_primary=p.id == job.primary_posting_id,
            version_count=version_counts.get(p.id, 0),
        )
        for p, s in posting_rows
    ]
    pending = await session.scalar(
        select(func.count())
        .select_from(DuplicateSuggestion)
        .where(
            DuplicateSuggestion.state == DuplicateState.pending,
            or_(DuplicateSuggestion.job_a_id == job_id, DuplicateSuggestion.job_b_id == job_id),
        )
    )
    v = primary_version
    return JobDetail(
        **item.model_dump(exclude={"experience_min_years", "experience_max_years"}),
        department=job.department,
        locations=list(job.locations or []),
        experience_text=v.experience_text if v else None,
        experience_min_years=job.experience_min_years,
        experience_max_years=job.experience_max_years,
        skills=list(job.skills or []),
        qualifications_required=v.qualifications_required if v else None,
        qualifications_preferred=v.qualifications_preferred if v else None,
        salary_text=v.salary_text if v else None,
        posted_at_raw=job.posted_at_raw,
        deadline_at=job.deadline_at,
        description_html=v.description_html if v else None,
        description_text=job.description_text,
        last_seen_at=job.last_seen_at,
        closed_at=job.closed_at,
        scope_overridden=job.scope_overridden,
        merged_into_job_id=job.merged_into_job_id,
        postings=postings,
        pending_duplicate_count=pending or 0,
    )


# ------------------------------------------------------------------------------------------ manual entry
async def create_manual_job(session: AsyncSession, body: ManualJobCreate, *, now: datetime | None = None) -> JobDetail:
    now = now or datetime.now(UTC)
    if body.initial_status and body.initial_status != "new":
        await status_by_key(session, body.initial_status)  # validate early -> 404 unknown_status
    company = await resolve_company(session, company_id=body.company_id, company_name=body.company_name)
    token = uuid.uuid4()
    posted_at = body.posted_at
    data = PostingInput(
        source_key="manual",
        company_id=company.id,
        external_id=f"manual-{token}",
        source_url=body.source_url or f"https://jobwatch.local/manual/{token}",
        apply_url=body.apply_url,
        title=body.title,
        department=body.department,
        location_text=body.location_text,
        locations=locations_from(body.location_text, body.country_code),
        employment_type=body.employment_type if body.employment_type.value != "unknown" else None,
        work_arrangement=body.work_arrangement if body.work_arrangement.value != "unknown" else None,
        salary_text=body.salary_text,
        posted_at=posted_at,
        posted_at_precision=DatePrecision.day if posted_at else DatePrecision.unknown,
        deadline_at=body.deadline_at,
        description_text=body.description,
        raw_payload={"origin_platform": body.origin_platform, "manual": True},
    )
    outcome = await ingest_service.ingest(session, data, seen_at=now)
    await change_initial_status(session, outcome.job_id, body.initial_status, now=now)
    return await get_job_detail(session, outcome.job_id, now=now)


# ---------------------------------------------------------------------------------------------- update
_CONTENT_FIELDS = {
    "title", "location_text", "country_code", "employment_type", "work_arrangement",
    "department", "description", "apply_url", "deadline_at",
}  # fmt: skip


async def update_job(session: AsyncSession, job_id: int, body: JobUpdate, *, now: datetime | None = None) -> JobDetail:
    now = now or datetime.now(UTC)
    job = await _job(session, job_id)
    sent = body.model_fields_set
    content_sent = sent & _CONTENT_FIELDS
    if content_sent:
        primary = await session.get(Posting, job.primary_posting_id) if job.primary_posting_id else None
        source = await session.get(Source, primary.source_id) if primary else None
        if primary is None or source is None or source.key not in CONTENT_EDITABLE_SOURCES:
            raise Conflict(
                "Content of jobs discovered from a source cannot be edited; only manual and captured jobs can",
                code="content_locked",
                details={"fields": sorted(content_sent)},
            )
        version = await session.get(JobVersion, primary.current_version_id)
        assert version is not None
        title = body.title if "title" in sent and body.title else version.title
        location_text = body.location_text if "location_text" in sent else version.location_text
        description_changed = "description" in sent
        locations = [LocationInput(**loc) for loc in version.locations]
        if "location_text" in sent or "country_code" in sent:
            locations = locations_from(location_text, body.country_code) or []
            if not locations:
                location_text = location_text if "location_text" in sent else version.location_text
        data = PostingInput(
            source_key=source.key,
            company_id=job.company_id,
            external_id=primary.external_id,
            requisition_id=primary.requisition_id,
            source_url=primary.source_url,
            apply_url=str(body.apply_url)
            if "apply_url" in sent and body.apply_url
            else (None if "apply_url" in sent else primary.apply_url),
            title=title,
            department=body.department if "department" in sent else version.department,
            location_text=location_text,
            locations=locations or None,
            employment_type=(body.employment_type if "employment_type" in sent else version.employment_type),
            work_arrangement=(body.work_arrangement if "work_arrangement" in sent else version.work_arrangement),
            experience_text=version.experience_text,
            experience_min_years=version.experience_min_years,
            experience_max_years=version.experience_max_years,
            skills=None if description_changed or "title" in sent else version.skills,
            qualifications_required=version.qualifications_required,
            qualifications_preferred=version.qualifications_preferred,
            salary_text=version.salary_text,
            posted_at=version.posted_at,
            posted_at_precision=version.posted_at_precision,
            posted_at_raw=version.posted_at_raw,
            deadline_at=body.deadline_at if "deadline_at" in sent else version.deadline_at,
            description_html=None if description_changed else version.description_html,
            description_text=body.description if description_changed else version.description_text,
            raw_payload=version.raw_payload,
        )
        await ingest_service.ingest(session, data, seen_at=now)
        job = await _job(session, job_id)
    if body.clear_scope_override:
        job.scope_overridden = False
        await refresh_job(session, job, now=now, scope_only=True)
    elif body.location_scope_override is not None:
        job.location_scope = body.location_scope_override
        job.scope_overridden = True
        await session.flush()
    return await get_job_detail(session, job_id, now=now)


# ------------------------------------------------------------------------------------ versions & diffs
def _stringify(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, list | dict):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def _version_out(v: JobVersion) -> VersionOut:
    return VersionOut(
        id=v.id,
        posting_id=v.posting_id,
        version_no=v.version_no,
        captured_at=v.captured_at,
        title=v.title,
        changed_fields=list(v.changed_fields or []),
    )


async def list_versions(session: AsyncSession, job_id: int) -> list[VersionOut]:
    await _job(session, job_id)
    rows = (
        await session.scalars(
            select(JobVersion)
            .join(Posting, Posting.id == JobVersion.posting_id)
            .where(Posting.job_id == job_id)
            .order_by(JobVersion.captured_at.desc(), JobVersion.id.desc())
        )
    ).all()
    return [_version_out(v) for v in rows]


async def version_diff(session: AsyncSession, job_id: int, version_id: int) -> VersionDiff:
    await _job(session, job_id)
    version = await session.scalar(
        select(JobVersion)
        .join(Posting, Posting.id == JobVersion.posting_id)
        .where(JobVersion.id == version_id, Posting.job_id == job_id)
    )
    if version is None:
        raise NotFound(f"Version {version_id} not found for job {job_id}", code="version_not_found")
    previous = await session.scalar(
        select(JobVersion).where(
            JobVersion.posting_id == version.posting_id, JobVersion.version_no == version.version_no - 1
        )
    )
    diffs: list[FieldDiff] = []
    if previous is not None:
        for name in version.changed_fields or []:
            diffs.append(
                FieldDiff(
                    field=name,
                    before=_stringify(getattr(previous, name, None)),
                    after=_stringify(getattr(version, name, None)),
                )
            )
    return VersionDiff(
        version=_version_out(version), previous_version_id=previous.id if previous else None, diffs=diffs
    )


# ------------------------------------------------------------------------------------------------ review
async def mark_reviewed(session: AsyncSession, job_ids: list[int], *, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    apps = (
        await session.scalars(
            select(Application).where(Application.job_id.in_(set(job_ids)), Application.reviewed_at.is_(None))
        )
    ).all()
    for app in apps:
        app.reviewed_at = now
        await record_event(session, app.id, EventType.reviewed, "Marked as reviewed", actor=Actor.user, occurred_at=now)
    await session.flush()


async def mark_unreviewed(session: AsyncSession, job_ids: list[int]) -> None:
    await session.execute(
        update(Application)
        .where(Application.job_id.in_(set(job_ids)), Application.reviewed_at.is_not(None))
        .values(reviewed_at=None)
    )


# ------------------------------------------------------------------------------------------- duplicates
async def _suggestion_outs(
    session: AsyncSession, suggestions: list[DuplicateSuggestion], *, now: datetime
) -> list[DuplicateSuggestionOut]:
    ids = [j for s in suggestions for j in (s.job_a_id, s.job_b_id)]
    items = await queries.items_for_job_ids(session, ids, now=now)
    return [
        DuplicateSuggestionOut(
            id=s.id,
            job_a=items[s.job_a_id],
            job_b=items[s.job_b_id],
            score=s.score,
            reasons=list(s.reasons or []),
            state=s.state,
            created_at=s.created_at,
        )
        for s in suggestions
    ]


async def job_duplicates(
    session: AsyncSession, job_id: int, *, now: datetime | None = None
) -> list[DuplicateSuggestionOut]:
    now = now or datetime.now(UTC)
    await _job(session, job_id)
    suggestions = (
        await session.scalars(
            select(DuplicateSuggestion)
            .where(
                DuplicateSuggestion.state == DuplicateState.pending,
                or_(DuplicateSuggestion.job_a_id == job_id, DuplicateSuggestion.job_b_id == job_id),
            )
            .order_by(DuplicateSuggestion.score.desc(), DuplicateSuggestion.id)
        )
    ).all()
    return await _suggestion_outs(session, list(suggestions), now=now)


async def list_pending_duplicates(
    session: AsyncSession, paging: PageParams, *, now: datetime | None = None
) -> Page[DuplicateSuggestionOut]:
    from sqlalchemy.orm import aliased

    now = now or datetime.now(UTC)
    ja, jb = aliased(Job), aliased(Job)
    base = (
        select(DuplicateSuggestion)
        .join(ja, ja.id == DuplicateSuggestion.job_a_id)
        .join(jb, jb.id == DuplicateSuggestion.job_b_id)
        .where(
            DuplicateSuggestion.state == DuplicateState.pending,
            ja.merged_into_job_id.is_(None),
            jb.merged_into_job_id.is_(None),
        )
    )
    total = await session.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = (
        await session.scalars(
            base.order_by(DuplicateSuggestion.score.desc(), DuplicateSuggestion.id)
            .limit(paging.page_size)
            .offset(paging.offset)
        )
    ).all()
    items = await _suggestion_outs(session, list(rows), now=now)
    return Page[DuplicateSuggestionOut](items=items, page=paging.page, page_size=paging.page_size, total=total)
