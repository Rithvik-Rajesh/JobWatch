"""Merging jobs (user-confirmed duplicates). The other job becomes a tombstone (`merged_into_job_id`);
postings and application children move to the survivor, history is kept."""

from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.applications.lifecycle import record_event
from app.core.errors import Conflict, NotFound
from app.discovery.ingest import refresh_job, update_company_open_count
from app.models import (
    Application,
    ApplicationEvent,
    Contact,
    DuplicateSuggestion,
    Interview,
    Job,
    JobTag,
    Note,
    Posting,
    Reminder,
    SalaryRecord,
)
from app.models.enums import Actor, DuplicateState, EventType


async def _job(session: AsyncSession, job_id: int) -> Job:
    job = await session.get(Job, job_id)
    if job is None:
        raise NotFound(f"Job {job_id} not found", code="job_not_found")
    return job


async def _application(session: AsyncSession, job_id: int) -> Application:
    app = await session.scalar(select(Application).where(Application.job_id == job_id))
    if app is None:
        raise NotFound(f"Job {job_id} has no application record")
    return app


async def merge_jobs(
    session: AsyncSession,
    survivor_id: int,
    other_id: int,
    *,
    take_status_from_other: bool = False,
    now: datetime | None = None,
) -> Job:
    """Merge `other` INTO `survivor`. Returns the survivor."""
    now = now or datetime.now(UTC)
    if survivor_id == other_id:
        raise Conflict("A job cannot be merged into itself", code="merge_self")
    survivor = await _job(session, survivor_id)
    other = await _job(session, other_id)
    if survivor.merged_into_job_id is not None or other.merged_into_job_id is not None:
        raise Conflict("Cannot merge into or from a job that was already merged", code="merge_target_merged")

    s_app = await _application(session, survivor.id)
    o_app = await _application(session, other.id)

    moved = await session.scalar(select(func.count()).select_from(Posting).where(Posting.job_id == other.id)) or 0
    await session.execute(update(Posting).where(Posting.job_id == other.id).values(job_id=survivor.id))

    # application children (notes, contacts, interviews, reminders) move to the survivor's application
    for model in (Note, Contact, Interview, Reminder):
        await session.execute(update(model).where(model.application_id == o_app.id).values(application_id=s_app.id))
    # timeline: application_events is append-only (UPDATE is blocked by a trigger), so the history is copied
    # onto the survivor's timeline (original timestamps kept) and the original rows stay on the tombstone.
    events = (
        await session.scalars(
            select(ApplicationEvent).where(ApplicationEvent.application_id == o_app.id).order_by(ApplicationEvent.id)
        )
    ).all()
    for ev in events:
        session.add(
            ApplicationEvent(
                application_id=s_app.id,
                event_type=ev.event_type,
                from_status_id=ev.from_status_id,
                to_status_id=ev.to_status_id,
                actor=ev.actor,
                summary=ev.summary,
                payload={**(ev.payload or {}), "merged_from_job_id": other.id},
                occurred_at=ev.occurred_at,
                created_at=ev.created_at,
            )
        )
    follow_ups = [t for t in (s_app.next_follow_up_at, o_app.next_follow_up_at) if t is not None]
    s_app.next_follow_up_at = min(follow_ups) if follow_ups else None
    o_app.next_follow_up_at = None

    # salary records: keep at most one current record per provider on the survivor
    survivor_providers = select(SalaryRecord.provider).where(
        SalaryRecord.job_id == survivor.id, SalaryRecord.is_current.is_(True)
    )
    await session.execute(
        update(SalaryRecord)
        .where(
            SalaryRecord.job_id == other.id,
            SalaryRecord.is_current.is_(True),
            SalaryRecord.provider.in_(survivor_providers),
        )
        .values(is_current=False)
    )
    await session.execute(update(SalaryRecord).where(SalaryRecord.job_id == other.id).values(job_id=survivor.id))

    # tags
    await session.execute(
        pg_insert(JobTag)
        .from_select(["job_id", "tag_id"], select(survivor.id, JobTag.tag_id).where(JobTag.job_id == other.id))
        .on_conflict_do_nothing()
    )
    await session.execute(JobTag.__table__.delete().where(JobTag.job_id == other.id))

    if take_status_from_other:
        old_status = s_app.status_id
        s_app.status_id = o_app.status_id
        s_app.status_changed_at = o_app.status_changed_at
        s_app.interested_at = o_app.interested_at
        s_app.shortlisted_at = o_app.shortlisted_at
        s_app.applied_at = o_app.applied_at
        s_app.closed_at = o_app.closed_at
        s_app.outcome_reason = o_app.outcome_reason
        s_app.reviewed_at = s_app.reviewed_at or o_app.reviewed_at or now
        s_app.row_version += 1
        if old_status != o_app.status_id:
            await record_event(
                session,
                s_app.id,
                EventType.status_changed,
                "Status taken over from merged duplicate",
                actor=Actor.user,
                occurred_at=now,
                from_status_id=old_status,
                to_status_id=o_app.status_id,
                payload={"merged_from_job_id": other.id},
            )

    other.merged_into_job_id = survivor.id
    await session.flush()

    a, b = sorted((survivor.id, other.id))
    await session.execute(
        update(DuplicateSuggestion)
        .where(
            DuplicateSuggestion.job_a_id == a,
            DuplicateSuggestion.job_b_id == b,
            DuplicateSuggestion.state == DuplicateState.pending,
        )
        .values(state=DuplicateState.confirmed, resolved_at=now)
    )

    await session.flush()
    await refresh_job(session, survivor, now=now)
    await record_event(
        session,
        s_app.id,
        EventType.job_merged,
        f"Merged duplicate job #{other.id} ({other.title})",
        actor=Actor.user,
        occurred_at=now,
        payload={
            "merged_job_id": other.id,
            "merged_title": other.title,
            "postings_moved": moved,
            "take_status_from_other": take_status_from_other,
        },
    )
    for company_id in {survivor.company_id, other.company_id}:
        await update_company_open_count(session, company_id)
    return survivor


async def _suggestion(session: AsyncSession, suggestion_id: int) -> DuplicateSuggestion:
    s = await session.get(DuplicateSuggestion, suggestion_id)
    if s is None:
        raise NotFound(f"Duplicate suggestion {suggestion_id} not found", code="suggestion_not_found")
    return s


async def confirm_suggestion(session: AsyncSession, suggestion_id: int, *, now: datetime | None = None) -> Job:
    """Merge job_b into job_a (the older job survives)."""
    s = await _suggestion(session, suggestion_id)
    if s.state != DuplicateState.pending:
        raise Conflict(f"Suggestion is already {s.state.value}", code="suggestion_resolved")
    return await merge_jobs(session, s.job_a_id, s.job_b_id, now=now)


async def dismiss_suggestion(session: AsyncSession, suggestion_id: int, *, now: datetime | None = None) -> None:
    s = await _suggestion(session, suggestion_id)
    if s.state == DuplicateState.confirmed:
        raise Conflict("Suggestion was already merged", code="suggestion_resolved")
    if s.state == DuplicateState.pending:
        s.state = DuplicateState.dismissed
        s.resolved_at = now or datetime.now(UTC)
        await session.flush()
