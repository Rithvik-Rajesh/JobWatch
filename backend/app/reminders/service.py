"""Reminders: CRUD, complete / dismiss / snooze, interview-linked reminders and the denormalized
`applications.next_follow_up_at` (earliest pending follow_up reminder). Services use flush(), never commit()."""

from collections.abc import Sequence
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.applications.lifecycle import record_event
from app.core.errors import Conflict, NotFound, ValidationFailed
from app.jobs.schemas import CompanyRef
from app.models import Application, Company, Interview, Job, Reminder
from app.models.enums import EventType, ReminderKind, ReminderState
from app.reminders.schemas import ReminderCreate, ReminderJobRef, ReminderOut, ReminderUpdate, SnoozeRequest
from app.settings.registry import ReminderDefaults, TimezoneSettings, get_setting


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(dt: datetime | None) -> datetime | None:
    """Naive datetimes are interpreted as UTC; aware ones are converted to UTC."""
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


# --------------------------------------------------------------------------- shared primitives


async def sync_next_follow_up(session: AsyncSession, application_id: int) -> datetime | None:
    """Recompute applications.next_follow_up_at = earliest pending follow_up reminder's due_at."""
    due = await session.scalar(
        select(func.min(Reminder.due_at)).where(
            Reminder.application_id == application_id,
            Reminder.kind == ReminderKind.follow_up,
            Reminder.state == ReminderState.pending,
        )
    )
    app = await session.get(Application, application_id)
    if app is not None and app.next_follow_up_at != due:
        app.next_follow_up_at = due
        await session.flush()
    return due


async def create_reminder_row(
    session: AsyncSession,
    *,
    application_id: int | None,
    kind: ReminderKind,
    title: str,
    due_at: datetime,
    notify_at: datetime | None,
    notes: str | None = None,
    interview_id: int | None = None,
) -> Reminder:
    reminder = Reminder(
        application_id=application_id,
        interview_id=interview_id,
        kind=kind,
        title=title,
        notes=notes,
        due_at=due_at,
        notify_at=notify_at,
        state=ReminderState.pending,
    )
    session.add(reminder)
    await session.flush()
    if application_id is not None and kind == ReminderKind.follow_up:
        await sync_next_follow_up(session, application_id)
    return reminder


async def reschedule_interview_reminders(session: AsyncSession, interview_id: int, scheduled_at: datetime) -> None:
    """Move the pending interview / prep reminders linked to an interview to a new time."""
    defaults = await get_setting(session, ReminderDefaults)
    lead = timedelta(minutes=defaults.notify_lead_minutes)
    reminders = (
        await session.scalars(
            select(Reminder).where(Reminder.interview_id == interview_id, Reminder.state == ReminderState.pending)
        )
    ).all()
    for r in reminders:
        if r.kind == ReminderKind.interview:
            due = scheduled_at
        elif r.kind == ReminderKind.interview_prep:
            due = scheduled_at - timedelta(hours=defaults.interview_prep_lead_hours)
        else:
            continue
        r.due_at = due
        r.notify_at = None if r.notify_at is None else due - lead
    await session.flush()


async def dismiss_interview_reminders(session: AsyncSession, interview_id: int) -> int:
    reminders = (
        await session.scalars(
            select(Reminder).where(Reminder.interview_id == interview_id, Reminder.state == ReminderState.pending)
        )
    ).all()
    for r in reminders:
        r.state = ReminderState.dismissed
    await session.flush()
    return len(reminders)


# --------------------------------------------------------------------------- read model


async def _query_outs(session: AsyncSession, *where: ColumnElement[bool], now: datetime) -> list[ReminderOut]:
    stmt = (
        select(Reminder, Job.id, Job.title, Company)
        .outerjoin(Application, Application.id == Reminder.application_id)
        .outerjoin(Job, Job.id == Application.job_id)
        .outerjoin(Company, Company.id == Job.company_id)
        .where(*where)
        .order_by(Reminder.due_at, Reminder.id)
        .execution_options(populate_existing=True)
    )
    out: list[ReminderOut] = []
    for r, job_id, job_title, company in (await session.execute(stmt)).all():
        job_ref = None
        if job_id is not None and r.application_id is not None:
            job_ref = ReminderJobRef(
                job_id=job_id,
                application_id=r.application_id,
                title=job_title,
                company=CompanyRef.model_validate(company),
            )
        out.append(
            ReminderOut(
                id=r.id,
                kind=r.kind,
                title=r.title,
                notes=r.notes,
                due_at=r.due_at,
                notify_at=r.notify_at,
                state=r.state,
                completed_at=r.completed_at,
                interview_id=r.interview_id,
                job=job_ref,
                overdue=r.state == ReminderState.pending and r.due_at < now,
                created_at=r.created_at,
            )
        )
    return out


async def _one_out(session: AsyncSession, reminder_id: int, now: datetime) -> ReminderOut:
    outs = await _query_outs(session, Reminder.id == reminder_id, now=now)
    if not outs:
        raise NotFound(f"Reminder {reminder_id} not found")
    return outs[0]


async def _get(session: AsyncSession, reminder_id: int) -> Reminder:
    r = await session.get(Reminder, reminder_id, populate_existing=True)
    if r is None:
        raise NotFound(f"Reminder {reminder_id} not found")
    return r


async def list_reminders(
    session: AsyncSession,
    *,
    due_from: datetime | None = None,
    due_to: datetime | None = None,
    states: Sequence[ReminderState] = (ReminderState.pending,),
    kinds: Sequence[ReminderKind] = (),
    job_id: int | None = None,
    now: datetime | None = None,
) -> list[ReminderOut]:
    now = as_utc(now) or utcnow()
    conds: list[ColumnElement[bool]] = []
    if states:
        conds.append(Reminder.state.in_(list(states)))
    if kinds:
        conds.append(Reminder.kind.in_(list(kinds)))
    if due_from is not None:
        conds.append(Reminder.due_at >= as_utc(due_from))
    if due_to is not None:
        conds.append(Reminder.due_at <= as_utc(due_to))
    if job_id is not None:
        conds.append(Job.id == job_id)
    return await _query_outs(session, *conds, now=now)


# --------------------------------------------------------------------------- commands


async def create_reminder(session: AsyncSession, data: ReminderCreate, *, now: datetime | None = None) -> ReminderOut:
    now = as_utc(now) or utcnow()
    defaults = await get_setting(session, ReminderDefaults)
    due_at = as_utc(data.due_at)
    assert due_at is not None

    application_id: int | None = None
    if data.job_id is not None:
        job = await session.get(Job, data.job_id)
        if job is None:
            raise NotFound(f"Job {data.job_id} not found")
        if job.merged_into_job_id is not None:
            raise Conflict("Job was merged into another job", code="merged")
        application_id = await session.scalar(select(Application.id).where(Application.job_id == data.job_id))
        if application_id is None:
            raise NotFound(f"Job {data.job_id} has no application record")
    if data.interview_id is not None:
        interview = await session.get(Interview, data.interview_id)
        if interview is None:
            raise NotFound(f"Interview {data.interview_id} not found")
        if application_id is None:
            application_id = interview.application_id
        elif interview.application_id != application_id:
            raise ValidationFailed("Interview does not belong to this job", code="interview_mismatch")

    if not data.notify:
        notify_at = None
    elif data.notify_at is not None:
        notify_at = as_utc(data.notify_at)
    else:
        notify_at = due_at - timedelta(minutes=defaults.notify_lead_minutes)

    reminder = await create_reminder_row(
        session,
        application_id=application_id,
        interview_id=data.interview_id,
        kind=data.kind,
        title=data.title,
        notes=data.notes,
        due_at=due_at,
        notify_at=notify_at,
    )
    if application_id is not None:
        await record_event(
            session,
            application_id,
            EventType.reminder_created,
            f"Reminder set: {data.title}",
            occurred_at=now,
            payload={"reminder_id": reminder.id, "kind": data.kind.value, "due_at": due_at.isoformat()},
        )
    return await _one_out(session, reminder.id, now)


async def update_reminder(
    session: AsyncSession, reminder_id: int, data: ReminderUpdate, *, now: datetime | None = None
) -> ReminderOut:
    now = as_utc(now) or utcnow()
    defaults = await get_setting(session, ReminderDefaults)
    r = await _get(session, reminder_id)
    fields = data.model_fields_set
    old_due, old_notify = r.due_at, r.notify_at

    if "kind" in fields and data.kind is not None:
        r.kind = data.kind
    if "title" in fields and data.title is not None:
        r.title = data.title
    if "notes" in fields:
        r.notes = data.notes
    if "due_at" in fields and data.due_at is not None:
        r.due_at = as_utc(data.due_at)  # type: ignore[assignment]
    if "notify_at" in fields:
        r.notify_at = as_utc(data.notify_at)
    elif "due_at" in fields and old_notify is not None and data.notify is not False:
        r.notify_at = r.due_at - (old_due - old_notify)  # keep the same lead time
    if "notify" in fields and data.notify is not None:
        if data.notify is False:
            r.notify_at = None
        elif r.notify_at is None:
            r.notify_at = r.due_at - timedelta(minutes=defaults.notify_lead_minutes)
    await session.flush()
    if r.application_id is not None:
        await sync_next_follow_up(session, r.application_id)
    return await _one_out(session, r.id, now)


async def complete_reminder(session: AsyncSession, reminder_id: int, *, now: datetime | None = None) -> ReminderOut:
    now = as_utc(now) or utcnow()
    r = await _get(session, reminder_id)
    if r.state != ReminderState.done:
        r.state = ReminderState.done
        r.completed_at = now
        await session.flush()
        if r.application_id is not None:
            await record_event(
                session,
                r.application_id,
                EventType.reminder_completed,
                f"Completed: {r.title}",
                occurred_at=now,
                payload={"reminder_id": r.id, "kind": r.kind.value},
            )
            await sync_next_follow_up(session, r.application_id)
    return await _one_out(session, r.id, now)


async def dismiss_reminder(session: AsyncSession, reminder_id: int, *, now: datetime | None = None) -> ReminderOut:
    now = as_utc(now) or utcnow()
    r = await _get(session, reminder_id)
    if r.state != ReminderState.dismissed:
        r.state = ReminderState.dismissed
        r.completed_at = None
        await session.flush()
        if r.application_id is not None:
            await sync_next_follow_up(session, r.application_id)
    return await _one_out(session, r.id, now)


def _local_nine(tz: ZoneInfo, day) -> datetime:
    return datetime.combine(day, time(9, 0), tzinfo=tz).astimezone(UTC)


def snooze_target(preset: str, now: datetime, tz: ZoneInfo) -> datetime:
    local = now.astimezone(tz)
    if preset == "1h":
        return now + timedelta(hours=1)
    if preset == "3h":
        return now + timedelta(hours=3)
    if preset == "tomorrow":
        return _local_nine(tz, local.date() + timedelta(days=1))
    if preset == "next_week":
        days = (7 - local.weekday()) % 7 or 7  # next Monday, strictly after today
        return _local_nine(tz, local.date() + timedelta(days=days))
    raise ValidationFailed(f"Unknown snooze preset '{preset}'")


async def snooze_reminder(
    session: AsyncSession, reminder_id: int, data: SnoozeRequest, *, now: datetime | None = None
) -> ReminderOut:
    now = as_utc(now) or utcnow()
    if data.until is None and data.preset is None:
        raise ValidationFailed("Provide 'until' or 'preset'", code="snooze_target_required")
    r = await _get(session, reminder_id)
    if r.state != ReminderState.pending:
        raise Conflict("Only pending reminders can be snoozed", code="not_pending")
    if data.until is not None:
        new_due = as_utc(data.until)
        assert new_due is not None
    else:
        assert data.preset is not None
        tz = ZoneInfo((await get_setting(session, TimezoneSettings)).name)
        new_due = snooze_target(data.preset, now, tz)
    if r.notify_at is not None:
        r.notify_at = new_due - (r.due_at - r.notify_at)
    r.due_at = new_due
    await session.flush()
    if r.application_id is not None:
        await sync_next_follow_up(session, r.application_id)
    return await _one_out(session, r.id, now)


async def delete_reminder(session: AsyncSession, reminder_id: int) -> None:
    r = await _get(session, reminder_id)
    application_id = r.application_id
    await session.execute(delete(Reminder).where(Reminder.id == reminder_id))
    session.expunge(r)
    if application_id is not None:
        await sync_next_follow_up(session, application_id)
