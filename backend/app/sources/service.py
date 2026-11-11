"""Sources service: list with usage stats, enable/disable and configure."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound
from app.models import Job, Posting, ScrapeRun, Source
from app.sources.schemas import SourceOut, SourceUpdate

ALWAYS_ENABLED = {"career_site", "manual", "capture"}


def _query():
    jobs_count = (
        select(func.count(func.distinct(Job.id)))
        .select_from(Posting)
        .join(Job, Job.id == Posting.job_id)
        .where(Posting.source_id == Source.id, Job.merged_into_job_id.is_(None))
        .correlate(Source)
        .scalar_subquery()
    )
    last_run = (
        select(func.max(ScrapeRun.queued_at))
        .where(ScrapeRun.source_id == Source.id)
        .correlate(Source)
        .scalar_subquery()
    )
    return select(Source, jobs_count.label("jobs_count"), last_run.label("last_run_at"))


def _out(source: Source, jobs_count: int | None, last_run_at) -> SourceOut:
    out = SourceOut.model_validate(source)
    out.jobs_count = jobs_count or 0
    out.last_run_at = last_run_at
    return out


async def list_sources(session: AsyncSession) -> list[SourceOut]:
    rows = await session.execute(_query().order_by(Source.id))
    return [_out(s, n, t) for s, n, t in rows.all()]


async def update_source(session: AsyncSession, source_id: int, body: SourceUpdate) -> SourceOut:
    source = await session.get(Source, source_id)
    if source is None:
        raise NotFound("Source not found")
    if body.enabled is False and source.key.lower() in ALWAYS_ENABLED:
        raise Conflict(f"Source '{source.key}' cannot be disabled")
    if body.enabled is not None:
        source.enabled = body.enabled
    if body.config is not None:
        source.config = body.config
    await session.flush()
    row = (await session.execute(_query().where(Source.id == source_id))).one()
    await session.refresh(source)
    return _out(*row)
