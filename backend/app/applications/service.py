"""Application lifecycle service: status changes, application edits, bulk status, tracker, timeline,
notes, contacts and interviews. Services use flush(), never commit(); routers stay thin.

`change_status` is a cross-module contract (the Jobs agent calls it) — keep its name and signature.
It never touches `jobs.availability`: availability and application status are independent.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, ScalarSelect, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.applications.lifecycle import record_event, status_by_key
from app.applications.schemas import (
    ApplicationOut,
    ApplicationUpdate,
    BulkStatusResult,
    ContactCreate,
    ContactUpdate,
    InterviewCreate,
    InterviewUpdate,
    ManualTimelineEntry,
    NoteCreate,
    NoteUpdate,
    TimelineEventOut,
    TrackerRow,
)
from app.core.errors import Conflict, NotFound, ValidationFailed
from app.core.pagination import Page, PageParams
from app.jobs.schemas import CompanyRef, StatusRef
from app.models import (
    Application,
    ApplicationEvent,
    Company,
    Contact,
    Interview,
    Job,
    Note,
    ResumeVersion,
    Status,
)
from app.models.enums import Actor, EventType, ReminderKind, StatusCategory
from app.reminders.service import (
    as_utc,
    create_reminder_row,
    dismiss_interview_reminders,
    reschedule_interview_reminders,
    sync_next_follow_up,
    utcnow,
)
from app.settings.registry import ReminderDefaults, get_setting

CLOSED_CATEGORIES = {StatusCategory.closed_negative, StatusCategory.closed_neutral, StatusCategory.archived}


@dataclass
class StatusChangeOutcome:
    application: Application
    changed: bool
    created_reminder_id: int | None = None


# --------------------------------------------------------------------------- lookups


async def _get_job(session: AsyncSession, job_id: int) -> Job:
    job = await session.get(Job, job_id)
    if job is None:
        raise NotFound(f"Job {job_id} not found", code="job_not_found")
    return job


async def _application_for_job(session: AsyncSession, job_id: int, *, lock: bool = False) -> Application:
    stmt = select(Application).where(Application.job_id == job_id).execution_options(populate_existing=True)
    if lock:
        stmt = stmt.with_for_update()
    app = await session.scalar(stmt)
    if app is None:
        raise NotFound(f"Job {job_id} has no application record", code="application_not_found")
    return app


async def _writable_application(session: AsyncSession, job_id: int) -> Application:
    """Application of a job for mutations: 404 if unknown, 409 if the job was merged away."""
    job = await _get_job(session, job_id)
    if job.merged_into_job_id is not None:
        raise Conflict("Job was merged into another job", code="merged")
    return await _application_for_job(session, job_id)


def _check_version(app: Application, expected: int | None) -> None:
    if expected is not None and expected != app.row_version:
        raise Conflict(
            "The application was changed by someone else; reload and retry",
            code="stale",
            details={"expected_row_version": expected, "current_row_version": app.row_version},
        )


# --------------------------------------------------------------------------- status changes


async def change_status(
    session: AsyncSession,
    job_id: int,
    status_key: str,
    *,
    occurred_at: datetime | None = None,
    reason: str | None = None,
    note: str | None = None,
    expected_row_version: int | None = None,
    actor: Actor = Actor.user,
    now: datetime | None = None,
) -> StatusChangeOutcome:
    now = as_utc(now) or utcnow()
    occurred = as_utc(occurred_at) or now

    job = await _get_job(session, job_id)
    status = await status_by_key(session, status_key)
    if job.merged_into_job_id is not None:
        raise Conflict("Job was merged into another job", code="merged")
    if status.archived_at is not None:
        raise ValidationFailed(f"Status '{status_key}' is archived", code="status_archived")

    app = await _application_for_job(session, job_id, lock=True)
    _check_version(app, expected_row_version)

    if app.status_id == status.id:
        # Idempotent: no event, but the user did act on the job, so it leaves the inbox.
        if app.reviewed_at is None:
            app.reviewed_at = now
        if note:
            await _add_note(session, app.id, note, now=now)
        await session.flush()
        return StatusChangeOutcome(app, changed=False)

    old_status = await session.get(Status, app.status_id)
    old_category = old_status.category if old_status else None
    category = status.category

    first_applied = category == StatusCategory.applied and app.applied_at is None

    # Milestones: first entry into a category only, never overwritten.
    if category == StatusCategory.considering:
        if status.key == "shortlisted":
            app.shortlisted_at = app.shortlisted_at or occurred
        else:
            app.interested_at = app.interested_at or occurred
    elif category == StatusCategory.applied:
        app.applied_at = app.applied_at or occurred
    if category in CLOSED_CATEGORIES:
        app.closed_at = app.closed_at or occurred
        if reason is not None:
            app.outcome_reason = reason
    elif old_category in CLOSED_CATEGORIES:
        app.closed_at = None

    app.status_id = status.id
    app.status_changed_at = occurred
    app.board_position = None  # position belonged to the old column
    if app.reviewed_at is None:
        app.reviewed_at = now
    app.row_version += 1
    await session.flush()

    payload: dict[str, Any] = {"from": old_status.key if old_status else None, "to": status.key}
    if reason:
        payload["reason"] = reason
    await record_event(
        session,
        app.id,
        EventType.status_changed,
        f"Marked {status.label}",
        actor=actor,
        occurred_at=occurred,
        from_status_id=old_status.id if old_status else None,
        to_status_id=status.id,
        payload=payload,
    )
    if note:
        await _add_note(session, app.id, note, now=now, actor=actor)

    created_reminder_id: int | None = None
    if first_applied:
        created_reminder_id = await _auto_follow_up(session, app, job, applied_at=app.applied_at or occurred, now=now)

    await session.flush()
    return StatusChangeOutcome(app, changed=True, created_reminder_id=created_reminder_id)


async def _auto_follow_up(
    session: AsyncSession, app: Application, job: Job, *, applied_at: datetime, now: datetime
) -> int | None:
    defaults = await get_setting(session, ReminderDefaults)
    days = defaults.follow_up_after_applied_days
    if days is None:
        return None
    company = await session.get(Company, job.company_id)
    due = applied_at + timedelta(days=days)
    title = f"Follow up on {job.title} at {company.name if company else 'the company'}"
    reminder = await create_reminder_row(
        session,
        application_id=app.id,
        kind=ReminderKind.follow_up,
        title=title,
        due_at=due,
        notify_at=due - timedelta(minutes=defaults.notify_lead_minutes),
    )
    await record_event(
        session,
        app.id,
        EventType.reminder_created,
        f"Follow-up reminder set for {due:%Y-%m-%d}",
        actor=Actor.system,
        occurred_at=now,
        payload={"reminder_id": reminder.id, "kind": ReminderKind.follow_up.value, "due_at": due.isoformat()},
    )
    return reminder.id


async def _add_note(
    session: AsyncSession,
    application_id: int,
    body: str,
    *,
    now: datetime,
    pinned: bool = False,
    actor: Actor = Actor.user,
) -> Note:
    note = Note(application_id=application_id, body=body, pinned=pinned)
    session.add(note)
    await session.flush()
    await record_event(
        session,
        application_id,
        EventType.note_added,
        "Added a note",
        actor=actor,
        occurred_at=now,
        payload={"note_id": note.id, "preview": body[:120]},
    )
    await session.refresh(note)
    return note


# --------------------------------------------------------------------------- application read / edit


async def application_out(session: AsyncSession, app: Application) -> ApplicationOut:
    await session.refresh(app)
    status = await session.get(Status, app.status_id)
    assert status is not None
    label = None
    if app.resume_version_id is not None:
        label = await session.scalar(select(ResumeVersion.label).where(ResumeVersion.id == app.resume_version_id))
    data = {k: getattr(app, k) for k in ApplicationOut.model_fields if k not in {"status", "resume_version_label"}}
    return ApplicationOut(status=StatusRef.model_validate(status), resume_version_label=label, **data)


async def get_application(session: AsyncSession, job_id: int) -> ApplicationOut:
    await _get_job(session, job_id)
    return await application_out(session, await _application_for_job(session, job_id))


def _json_value(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    return v


def _display(v: Any) -> str:
    if v is None:
        return "none"
    if isinstance(v, datetime):
        return f"{v:%Y-%m-%d}"
    if isinstance(v, dict):
        return f"{len(v)} field(s)"
    s = str(v)
    return s if len(s) <= 60 else s[:57] + "..."


_EDITABLE = [f for f in ApplicationUpdate.model_fields if f != "expected_row_version"]
_NOT_NULL_FIELDS = {"starred"}
_DATETIME_FIELDS = {"interested_at", "shortlisted_at", "applied_at"}


async def update_application(
    session: AsyncSession, job_id: int, data: ApplicationUpdate, *, now: datetime | None = None
) -> ApplicationOut:
    now = as_utc(now) or utcnow()
    await _writable_application(session, job_id)
    app = await _application_for_job(session, job_id, lock=True)
    _check_version(app, data.expected_row_version)

    changes: dict[str, tuple[Any, Any]] = {}
    for field in _EDITABLE:
        if field not in data.model_fields_set:
            continue
        value = getattr(data, field)
        if value is None and field in _NOT_NULL_FIELDS:
            continue
        if field == "application_url" and value is not None:
            value = str(value)
        if field in _DATETIME_FIELDS:
            value = as_utc(value)
        if field == "resume_version_id" and value is not None:
            exists = await session.scalar(select(ResumeVersion.id).where(ResumeVersion.id == value))
            if exists is None:
                raise ValidationFailed(f"Resume version {value} does not exist", code="resume_version_not_found")
        before = getattr(app, field)
        if before != value:
            changes[field] = (before, value)
            setattr(app, field, value)

    if changes:
        app.row_version += 1
        await session.flush()
        for field, (before, after) in changes.items():
            await record_event(
                session,
                app.id,
                EventType.field_changed,
                f"Changed {field.replace('_', ' ')}: {_display(before)} -> {_display(after)}",
                occurred_at=now,
                payload={"field": field, "before": _json_value(before), "after": _json_value(after)},
            )
    return await application_out(session, app)


# --------------------------------------------------------------------------- bulk


async def bulk_change_status(
    session: AsyncSession,
    job_ids: Sequence[int],
    status_key: str,
    *,
    reason: str | None = None,
    now: datetime | None = None,
) -> BulkStatusResult:
    """All or nothing: validated up front, and any failure aborts the request transaction."""
    ids = list(dict.fromkeys(job_ids))
    found = set((await session.scalars(select(Job.id).where(Job.id.in_(ids)))).all())
    missing = [i for i in ids if i not in found]
    if missing:
        raise NotFound(f"Jobs not found: {missing}", code="job_not_found", details={"job_ids": missing})
    await status_by_key(session, status_key)

    updated = unchanged = 0
    for job_id in ids:
        outcome = await change_status(session, job_id, status_key, reason=reason, now=now)
        if outcome.changed:
            updated += 1
        else:
            unchanged += 1
    return BulkStatusResult(updated=updated, unchanged=unchanged, job_ids=ids)


# --------------------------------------------------------------------------- tracker


def next_interview_at_expr(now: datetime) -> ScalarSelect[datetime | None]:
    return (
        select(func.min(Interview.scheduled_at))
        .where(
            Interview.application_id == Application.id,
            Interview.outcome == "pending",
            Interview.scheduled_at >= now,
        )
        .correlate(Application)
        .scalar_subquery()
    )


def search_conditions(company_ids: Sequence[int], q: str | None) -> list[ColumnElement[bool]]:
    conds: list[ColumnElement[bool]] = []
    if company_ids:
        conds.append(Job.company_id.in_(list(company_ids)))
    if q and q.strip():
        escaped = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        conds.append(or_(Job.title.ilike(pattern, escape="\\"), Company.name.ilike(pattern, escape="\\")))
    return conds


TRACKER_SORTS = ("latest_event_desc", "applied_desc", "follow_up_asc", "company_asc", "status")


async def list_tracker(
    session: AsyncSession,
    paging: PageParams,
    *,
    statuses: Sequence[str] = (),
    company_ids: Sequence[int] = (),
    q: str | None = None,
    sort: str = "latest_event_desc",
    now: datetime | None = None,
) -> Page[TrackerRow]:
    if sort not in TRACKER_SORTS:
        raise ValidationFailed(f"Unknown sort '{sort}'", code="invalid_sort", details={"allowed": list(TRACKER_SORTS)})
    now = as_utc(now) or utcnow()

    conds: list[ColumnElement[bool]] = [
        Status.category.not_in([StatusCategory.inbox, StatusCategory.archived]),
        Job.merged_into_job_id.is_(None),
        *search_conditions(company_ids, q),
    ]
    if statuses:
        conds.append(Status.key.in_(list(statuses)))

    total = await session.scalar(
        select(func.count())
        .select_from(Application)
        .join(Job, Job.id == Application.job_id)
        .join(Company, Company.id == Job.company_id)
        .join(Status, Status.id == Application.status_id)
        .where(*conds)
    )

    latest = (
        select(ApplicationEvent.occurred_at, ApplicationEvent.summary)
        .where(ApplicationEvent.application_id == Application.id)
        .order_by(ApplicationEvent.occurred_at.desc(), ApplicationEvent.id.desc())
        .limit(1)
        .correlate(Application)
        .lateral("latest")
    )
    order = {
        "latest_event_desc": [latest.c.occurred_at.desc().nulls_last()],
        "applied_desc": [Application.applied_at.desc().nulls_last(), latest.c.occurred_at.desc().nulls_last()],
        "follow_up_asc": [Application.next_follow_up_at.asc().nulls_last(), latest.c.occurred_at.desc().nulls_last()],
        "company_asc": [func.lower(Company.name).asc(), Job.title.asc()],
        "status": [Status.sort_order.asc(), latest.c.occurred_at.desc().nulls_last()],
    }[sort]

    stmt = (
        select(
            Application,
            Job,
            Company,
            Status,
            latest.c.occurred_at,
            latest.c.summary,
            next_interview_at_expr(now),
        )
        .select_from(Application)
        .join(Job, Job.id == Application.job_id)
        .join(Company, Company.id == Job.company_id)
        .join(Status, Status.id == Application.status_id)
        .outerjoin(latest, true())
        .where(*conds)
        .order_by(*order, Application.id)
        .limit(paging.page_size)
        .offset(paging.offset)
        .execution_options(populate_existing=True)
    )
    items = [
        TrackerRow(
            application_id=app.id,
            job_id=job.id,
            company=CompanyRef.model_validate(company),
            title=job.title,
            status=StatusRef.model_validate(status),
            availability=job.availability,
            applied_at=app.applied_at,
            next_action=app.next_action,
            next_follow_up_at=app.next_follow_up_at,
            next_interview_at=next_interview,
            latest_event_at=event_at or app.status_changed_at,
            latest_event_summary=summary or "",
            starred=app.starred,
            priority=app.priority,
            row_version=app.row_version,
        )
        for app, job, company, status, event_at, summary, next_interview in (await session.execute(stmt)).all()
    ]
    return Page[TrackerRow](items=items, page=paging.page, page_size=paging.page_size, total=total or 0)


# --------------------------------------------------------------------------- timeline


async def list_timeline(session: AsyncSession, job_id: int) -> list[TimelineEventOut]:
    await _get_job(session, job_id)
    app = await _application_for_job(session, job_id)
    events = (
        await session.scalars(
            select(ApplicationEvent)
            .where(ApplicationEvent.application_id == app.id)
            .order_by(ApplicationEvent.occurred_at.desc(), ApplicationEvent.id.desc())
            .execution_options(populate_existing=True)
        )
    ).all()
    return await _timeline_out(session, events)


async def _timeline_out(session: AsyncSession, events: Sequence[ApplicationEvent]) -> list[TimelineEventOut]:
    ids = {i for e in events for i in (e.from_status_id, e.to_status_id) if i is not None}
    statuses: dict[int, StatusRef] = {}
    if ids:
        rows = (await session.scalars(select(Status).where(Status.id.in_(ids)))).all()
        statuses = {s.id: StatusRef.model_validate(s) for s in rows}
    return [
        TimelineEventOut(
            id=e.id,
            event_type=e.event_type,
            actor=e.actor,
            summary=e.summary,
            from_status=statuses.get(e.from_status_id) if e.from_status_id is not None else None,
            to_status=statuses.get(e.to_status_id) if e.to_status_id is not None else None,
            payload=e.payload or {},
            occurred_at=e.occurred_at,
            created_at=e.created_at,
        )
        for e in events
    ]


async def add_timeline_entry(
    session: AsyncSession, job_id: int, data: ManualTimelineEntry, *, now: datetime | None = None
) -> TimelineEventOut:
    now = as_utc(now) or utcnow()
    app = await _writable_application(session, job_id)
    event = await record_event(
        session,
        app.id,
        EventType.manual,
        data.summary,
        actor=Actor.user,
        occurred_at=as_utc(data.occurred_at) or now,
        payload=data.payload,
    )
    await session.refresh(event)
    return (await _timeline_out(session, [event]))[0]


# --------------------------------------------------------------------------- notes


async def list_notes(session: AsyncSession, job_id: int) -> list[Note]:
    await _get_job(session, job_id)
    app = await _application_for_job(session, job_id)
    return list(
        (
            await session.scalars(
                select(Note)
                .where(Note.application_id == app.id)
                .order_by(Note.pinned.desc(), Note.created_at.desc(), Note.id.desc())
                .execution_options(populate_existing=True)
            )
        ).all()
    )


async def create_note(session: AsyncSession, job_id: int, data: NoteCreate, *, now: datetime | None = None) -> Note:
    now = as_utc(now) or utcnow()
    app = await _writable_application(session, job_id)
    return await _add_note(session, app.id, data.body, now=now, pinned=data.pinned)


async def _get_note(session: AsyncSession, note_id: int) -> Note:
    note = await session.get(Note, note_id, populate_existing=True)
    if note is None:
        raise NotFound(f"Note {note_id} not found")
    return note


async def update_note(session: AsyncSession, note_id: int, data: NoteUpdate) -> Note:
    note = await _get_note(session, note_id)
    if "body" in data.model_fields_set and data.body is not None:
        note.body = data.body
    if "pinned" in data.model_fields_set and data.pinned is not None:
        note.pinned = data.pinned
    await session.flush()
    await session.refresh(note)
    return note


async def delete_note(session: AsyncSession, note_id: int) -> None:
    note = await _get_note(session, note_id)
    await session.delete(note)
    await session.flush()


# --------------------------------------------------------------------------- contacts


async def list_contacts(session: AsyncSession, job_id: int) -> list[Contact]:
    await _get_job(session, job_id)
    app = await _application_for_job(session, job_id)
    return list(
        (
            await session.scalars(
                select(Contact)
                .where(Contact.application_id == app.id)
                .order_by(Contact.id)
                .execution_options(populate_existing=True)
            )
        ).all()
    )


def _contact_values(data: ContactCreate | ContactUpdate) -> dict[str, Any]:
    values = data.model_dump(exclude_unset=True)
    if values.get("profile_url") is not None:
        values["profile_url"] = str(values["profile_url"])
    return values


async def create_contact(session: AsyncSession, job_id: int, data: ContactCreate) -> Contact:
    app = await _writable_application(session, job_id)
    contact = Contact(application_id=app.id, **_contact_values(data))
    session.add(contact)
    await session.flush()
    await session.refresh(contact)
    return contact


async def _get_contact(session: AsyncSession, contact_id: int) -> Contact:
    contact = await session.get(Contact, contact_id, populate_existing=True)
    if contact is None:
        raise NotFound(f"Contact {contact_id} not found")
    return contact


async def update_contact(session: AsyncSession, contact_id: int, data: ContactUpdate) -> Contact:
    contact = await _get_contact(session, contact_id)
    for field, value in _contact_values(data).items():
        if value is None and field in {"role", "name"}:
            continue
        setattr(contact, field, value)
    await session.flush()
    await session.refresh(contact)
    return contact


async def delete_contact(session: AsyncSession, contact_id: int) -> None:
    await session.delete(await _get_contact(session, contact_id))
    await session.flush()


# --------------------------------------------------------------------------- interviews


async def list_interviews(session: AsyncSession, job_id: int) -> list[Interview]:
    await _get_job(session, job_id)
    app = await _application_for_job(session, job_id)
    return list(
        (
            await session.scalars(
                select(Interview)
                .where(Interview.application_id == app.id)
                .order_by(Interview.scheduled_at.asc().nulls_last(), Interview.id)
                .execution_options(populate_existing=True)
            )
        ).all()
    )


async def create_interview(
    session: AsyncSession, job_id: int, data: InterviewCreate, *, now: datetime | None = None
) -> Interview:
    now = as_utc(now) or utcnow()
    app = await _writable_application(session, job_id)
    job = await _get_job(session, job_id)
    scheduled_at = as_utc(data.scheduled_at)
    interview = Interview(
        application_id=app.id,
        round_name=data.round_name,
        kind=data.kind,
        scheduled_at=scheduled_at,
        duration_min=data.duration_min,
        location=data.location,
        notes=data.notes,
    )
    session.add(interview)
    await session.flush()

    when = f" on {scheduled_at:%Y-%m-%d %H:%M} UTC" if scheduled_at else ""
    await record_event(
        session,
        app.id,
        EventType.interview_scheduled,
        f"Scheduled {data.round_name}{when}",
        occurred_at=now,
        payload={
            "interview_id": interview.id,
            "round_name": data.round_name,
            "kind": data.kind,
            "scheduled_at": _json_value(scheduled_at),
        },
    )

    if data.create_reminders and scheduled_at is not None:
        defaults = await get_setting(session, ReminderDefaults)
        company = await session.get(Company, job.company_id)
        label = f"{data.round_name} - {job.title} at {company.name if company else 'the company'}"
        lead = timedelta(minutes=defaults.notify_lead_minutes)
        prep_due = scheduled_at - timedelta(hours=defaults.interview_prep_lead_hours)
        await create_reminder_row(
            session,
            application_id=app.id,
            interview_id=interview.id,
            kind=ReminderKind.interview,
            title=f"Interview: {label}",
            due_at=scheduled_at,
            notify_at=scheduled_at - lead,
        )
        await create_reminder_row(
            session,
            application_id=app.id,
            interview_id=interview.id,
            kind=ReminderKind.interview_prep,
            title=f"Prepare for: {label}",
            due_at=prep_due,
            notify_at=prep_due - lead,
        )
    await session.refresh(interview)
    return interview


async def _get_interview(session: AsyncSession, interview_id: int) -> Interview:
    interview = await session.get(Interview, interview_id, populate_existing=True)
    if interview is None:
        raise NotFound(f"Interview {interview_id} not found")
    return interview


async def update_interview(
    session: AsyncSession, interview_id: int, data: InterviewUpdate, *, now: datetime | None = None
) -> Interview:
    now = as_utc(now) or utcnow()
    interview = await _get_interview(session, interview_id)
    changes: dict[str, tuple[Any, Any]] = {}
    for field in ("round_name", "kind", "scheduled_at", "duration_min", "location", "outcome", "notes"):
        if field not in data.model_fields_set:
            continue
        value = getattr(data, field)
        if value is None and field in {"round_name", "kind", "outcome"}:
            continue
        if field == "scheduled_at":
            value = as_utc(value)
        before = getattr(interview, field)
        if before != value:
            changes[field] = (before, value)
            setattr(interview, field, value)
    if not changes:
        await session.refresh(interview)
        return interview
    await session.flush()

    if "scheduled_at" in changes:
        new_time = changes["scheduled_at"][1]
        if new_time is None:
            await dismiss_interview_reminders(session, interview.id)
        else:
            await reschedule_interview_reminders(session, interview.id, new_time)
    if interview.outcome == "cancelled" and "outcome" in changes:
        await dismiss_interview_reminders(session, interview.id)

    parts = []
    for field, (before, after) in changes.items():
        parts.append(f"{field.replace('_', ' ')} {_display(before)} -> {_display(after)}")
    await record_event(
        session,
        interview.application_id,
        EventType.interview_updated,
        f"Updated {interview.round_name}: " + "; ".join(parts),
        occurred_at=now,
        payload={
            "interview_id": interview.id,
            "changes": {f: {"before": _json_value(b), "after": _json_value(a)} for f, (b, a) in changes.items()},
        },
    )
    await session.refresh(interview)
    return interview


async def delete_interview(session: AsyncSession, interview_id: int, *, now: datetime | None = None) -> None:
    now = as_utc(now) or utcnow()
    interview = await _get_interview(session, interview_id)
    application_id, round_name = interview.application_id, interview.round_name
    await session.delete(interview)  # linked reminders are removed by ON DELETE CASCADE
    await session.flush()
    await record_event(
        session,
        application_id,
        EventType.interview_updated,
        f"Removed {round_name}",
        occurred_at=now,
        payload={"interview_id": interview_id, "deleted": True},
    )
    await sync_next_follow_up(session, application_id)
