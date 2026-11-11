"""Shared test factories (central). Build real rows through the ORM + lifecycle contract so every
module's tests start from the same, invariant-respecting state. Extend by adding new functions;
do not change existing signatures."""

from datetime import UTC, datetime
from itertools import count

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.applications.lifecycle import create_tracking_record
from app.models import Application, Company, Job, Posting, Source, Status
from app.models.enums import Availability, ScopeMatch, WorkArrangement

_seq = count(1)


async def make_company(session: AsyncSession, name: str | None = None, **kw) -> Company:
    n = next(_seq)
    name = name or f"Company {n}"
    company = Company(name=name, slug=kw.pop("slug", f"{name.lower().replace(' ', '-')}-{n}"), **kw)
    session.add(company)
    await session.flush()
    return company


async def source_id(session: AsyncSession, key: str = "career_site") -> int:
    return (await session.scalars(select(Source.id).where(Source.key == key))).one()


async def make_job(
    session: AsyncSession,
    company: Company | None = None,
    *,
    title: str = "Software Engineer",
    country_codes: list[str] | None = None,
    location_scope: ScopeMatch | None = None,
    source_key: str = "career_site",
    external_id: str | None = None,
    first_seen_at: datetime | None = None,
    availability: Availability = Availability.open,
    work_arrangement: WorkArrangement = WorkArrangement.unknown,
    **job_fields,
) -> Job:
    """Job + one posting + application (status 'new', unreviewed) + 'discovered' event."""
    company = company or await make_company(session)
    codes = ["IN"] if country_codes is None else country_codes
    scope = location_scope or (
        ScopeMatch.in_scope if "IN" in codes else ScopeMatch.unknown if not codes else ScopeMatch.out_of_scope
    )
    seen = first_seen_at or datetime.now(UTC)
    job = Job(
        company_id=company.id,
        title=title,
        title_normalized=title.lower(),
        country_codes=codes,
        location_scope=scope,
        location_text=job_fields.pop("location_text", "Bengaluru, India" if "IN" in codes else None),
        availability=availability,
        work_arrangement=work_arrangement,
        first_seen_at=seen,
        last_seen_at=seen,
        last_changed_at=seen,
        **job_fields,
    )
    session.add(job)
    await session.flush()
    n = next(_seq)
    posting = Posting(
        job_id=job.id,
        source_id=await source_id(session, source_key),
        company_id=company.id,
        external_id=external_id or f"EXT-{n}",
        source_url=f"https://example.com/jobs/{n}",
        apply_url=f"https://example.com/jobs/{n}/apply",
        availability=availability,
        first_seen_at=seen,
        last_seen_at=seen,
    )
    session.add(posting)
    await session.flush()
    job.primary_posting_id = posting.id
    await create_tracking_record(session, job.id, discovered_at=seen, summary=f"Discovered on {source_key}")
    await session.flush()
    return job


async def application_for(session: AsyncSession, job: Job) -> Application:
    return (await session.scalars(select(Application).where(Application.job_id == job.id))).one()


async def status_id(session: AsyncSession, key: str) -> int:
    return (await session.scalars(select(Status.id).where(Status.key == key))).one()
