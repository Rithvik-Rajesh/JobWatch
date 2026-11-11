"""Dashboard aggregates (read-only). Every query is a single aggregate statement; no per-row loops.

Scope rules: job-derived counts use `jobs.location_scope IN visible_scopes(setting)` and exclude merged
jobs (`merged_into_job_id IS NULL`). "Upcoming" is about the user's own commitments, so it ignores the
location scope (a job you applied to never disappears from your calendar) but still ignores merged jobs.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import Select, and_, distinct, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.dashboard.schemas import (
    Breakdown,
    BreakdownItem,
    DashboardSummary,
    FunnelStage,
    Timeseries,
    TimeseriesPoint,
    UpcomingItem,
)
from app.jobs.schemas import CompanyRef
from app.jobs.scope import visible_scopes
from app.models import (
    Application,
    ApplicationEvent,
    Company,
    Interview,
    Job,
    Posting,
    Reminder,
    Source,
    Status,
)
from app.models.enums import (
    Availability,
    EventType,
    HealthStatus,
    ReminderState,
    StatusCategory,
)
from app.settings.registry import LocationScopeSettings, TimezoneSettings, get_setting


def _utcnow(now: datetime | None) -> datetime:
    return now.astimezone(UTC) if now else datetime.now(UTC)


async def _tz(session: AsyncSession) -> ZoneInfo:
    name = (await get_setting(session, TimezoneSettings)).name
    try:
        return ZoneInfo(name)
    except Exception:  # unknown tz name in settings: fall back rather than 500
        return ZoneInfo("Asia/Kolkata")


async def _scopes(session: AsyncSession) -> list[str]:
    cfg = await get_setting(session, LocationScopeSettings)
    return [s.value for s in visible_scopes(cfg)]


def _scoped_jobs(scopes: list[str]) -> list:
    return [Job.merged_into_job_id.is_(None), Job.location_scope.in_(scopes)]


async def summary(session: AsyncSession, *, now: datetime | None = None) -> DashboardSummary:
    now = _utcnow(now)
    tz = await _tz(session)
    scopes = await _scopes(session)
    local_midnight = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    today_start = local_midnight.astimezone(UTC)

    cat = Status.category
    row = (
        await session.execute(
            select(
                func.count(Job.id),
                func.count(Job.id).filter(Job.first_seen_at >= today_start),
                func.count(Job.id).filter(Application.reviewed_at.is_(None)),
                func.count(Job.id).filter(Status.key == "interested"),
                func.count(Job.id).filter(Status.key == "shortlisted"),
                func.count(Job.id).filter(Application.applied_at.is_not(None)),
                func.count(Job.id).filter(cat == StatusCategory.in_process),
                func.count(Job.id).filter(cat == StatusCategory.offer),
                func.count(Job.id).filter(cat == StatusCategory.closed_negative),
                func.count(Job.id).filter(Job.availability == Availability.open),
            )
            .select_from(Job)
            .join(Application, Application.job_id == Job.id)
            .join(Status, Status.id == Application.status_id)
            .where(*_scoped_jobs(scopes))
        )
    ).one()

    monitored, failing, last_success = (
        await session.execute(
            select(
                func.count(Company.id).filter(Company.monitoring_enabled),
                func.count(Company.id).filter(Company.health == HealthStatus.failing),
                func.max(Company.last_success_at),
            ).where(Company.archived_at.is_(None))
        )
    ).one()

    return DashboardSummary(
        total_discovered=row[0],
        new_today=row[1],
        unreviewed=row[2],
        interested=row[3],
        shortlisted=row[4],
        applied=row[5],
        in_process=row[6],
        offers=row[7],
        rejected=row[8],
        open_jobs=row[9],
        companies_monitored=monitored,
        companies_failing=failing,
        last_successful_scan_at=last_success,
    )


_STEP = {"day": "1 day", "week": "1 week", "month": "1 month"}


async def timeseries(
    session: AsyncSession, metric: str, bucket: str, days: int, *, now: datetime | None = None
) -> Timeseries:
    """Zero-filled series, oldest first, bucketed in the user's timezone.

    The first bucket may be partial (it starts at the bucket containing `now - days`).
    Metrics (all scoped, merged jobs excluded):
      discovered     jobs.first_seen_at
      applied        per application: earliest status_changed event into a status of category 'applied',
                     falling back to applications.applied_at when no such event exists
      status_changes every status_changed event
    """
    now = _utcnow(now)
    tz = await _tz(session)
    scopes = await _scopes(session)
    start = now - timedelta(days=days)

    if bucket not in _STEP:
        raise ValueError(bucket)
    if metric == "discovered":
        events_sql = "select j.first_seen_at as ts from scoped j"
    elif metric == "status_changes":
        events_sql = """
            select e.occurred_at as ts
            from application_events e
            join applications a on a.id = e.application_id
            join scoped j on j.id = a.job_id
            where e.event_type = 'status_changed'"""
    elif metric == "applied":
        events_sql = """
            select coalesce(min(e.occurred_at), a.applied_at) as ts
            from applications a
            join scoped j on j.id = a.job_id
            left join (
                select ev.application_id, ev.occurred_at
                from application_events ev
                join statuses s on s.id = ev.to_status_id and s.category = 'applied'
                where ev.event_type = 'status_changed'
            ) e on e.application_id = a.id
            group by a.id, a.applied_at"""
    else:
        raise ValueError(metric)

    sql = text(f"""
        with scoped as (
            select id, first_seen_at from jobs
            where merged_into_job_id is null and location_scope::text = any(cast(:scopes as text[]))
        ),
        evts as ({events_sql}),
        series as (
            select generate_series(
                date_trunc('{bucket}', cast(:start as timestamptz) at time zone :tz),
                date_trunc('{bucket}', cast(:now as timestamptz) at time zone :tz),
                cast('{_STEP[bucket]}' as interval)
            ) as bucket
        ),
        counts as (
            select date_trunc('{bucket}', ts at time zone :tz) as bucket, count(*) as c
            from evts where ts >= :start and ts <= :now group by 1
        )
        select cast(s.bucket as date) as bucket, coalesce(c.c, 0) as value
        from series s left join counts c on c.bucket = s.bucket
        order by s.bucket
    """)
    rows = (await session.execute(sql, {"scopes": scopes, "start": start, "now": now, "tz": tz.key})).all()
    return Timeseries(
        metric=metric,  # type: ignore[arg-type]
        bucket=bucket,  # type: ignore[arg-type]
        points=[TimeseriesPoint(bucket=r.bucket.isoformat(), value=r.value) for r in rows],
    )


_FUNNEL_LABELS = [
    ("discovered", "Discovered"),
    ("interested", "Interested"),
    ("shortlisted", "Shortlisted"),
    ("applied", "Applied"),
    ("in_process", "In process"),
    ("offer", "Offer"),
]


async def funnel(session: AsyncSession, days: int | None = None, *, now: datetime | None = None) -> list[FunnelStage]:
    """Funnel of jobs that EVER reached each stage, according to the timeline.

    * discovered: every scoped, non-merged job (optionally only those first seen in the last `days`).
    * Every other stage counts a job only if it has at least one `status_changed` event whose target
      status belongs to that stage. A job that jumped straight to 'applied' is NOT counted as
      interested/shortlisted: only stages actually reached are counted.
        interested  : status key 'interested' or any status of category 'considering'
        shortlisted : status key 'shortlisted'
        applied / in_process / offer : status category of the same name
    * Each job counts once per stage however often it entered it (events are de-duplicated per job).
    * conversion_from_previous = count / previous count (4 dp); null for the first stage or when the
      previous stage is empty. Because stages are not nested, a ratio can exceed 1.
    """
    now = _utcnow(now)
    scopes = await _scopes(session)
    conds = _scoped_jobs(scopes)
    if days is not None:
        conds.append(Job.first_seen_at >= now - timedelta(days=days))

    per_job = (
        select(
            Job.id.label("job_id"),
            func.coalesce(func.bool_or(or_(Status.key == "interested", Status.category == "considering")), False).label(
                "interested"
            ),
            func.coalesce(func.bool_or(Status.key == "shortlisted"), False).label("shortlisted"),
            func.coalesce(func.bool_or(Status.category == "applied"), False).label("applied"),
            func.coalesce(func.bool_or(Status.category == "in_process"), False).label("in_process"),
            func.coalesce(func.bool_or(Status.category == "offer"), False).label("offer"),
        )
        .select_from(Job)
        .join(Application, Application.job_id == Job.id)
        .outerjoin(
            ApplicationEvent,
            and_(
                ApplicationEvent.application_id == Application.id,
                ApplicationEvent.event_type == EventType.status_changed,
            ),
        )
        .outerjoin(Status, Status.id == ApplicationEvent.to_status_id)
        .where(*conds)
        .group_by(Job.id)
        .subquery()
    )
    row = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(per_job.c.interested),
                func.count().filter(per_job.c.shortlisted),
                func.count().filter(per_job.c.applied),
                func.count().filter(per_job.c.in_process),
                func.count().filter(per_job.c.offer),
            ).select_from(per_job)
        )
    ).one()

    out: list[FunnelStage] = []
    prev: int | None = None
    for (key, label), count in zip(_FUNNEL_LABELS, row, strict=True):
        conv = round(count / prev, 4) if prev else None
        out.append(FunnelStage(key=key, label=label, count=count, conversion_from_previous=conv))
        prev = count
    return out


async def breakdown(session: AsyncSession, by: str, limit: int = 15) -> Breakdown:
    scopes = await _scopes(session)
    scoped = and_(*_scoped_jobs(scopes))
    items: list[BreakdownItem]

    if by == "status":
        count = func.count(Job.id)
        rows = (
            await session.execute(
                select(Status.key, Status.label, Status.color, count)
                .select_from(Status)
                .outerjoin(Application, Application.status_id == Status.id)
                .outerjoin(Job, and_(Job.id == Application.job_id, scoped))
                .group_by(Status.id)
                .having(or_(Status.archived_at.is_(None), count > 0))
                .order_by(Status.sort_order, Status.id)
            )
        ).all()
        items = [BreakdownItem(key=r.key, label=r.label, color=r.color, count=r[3]) for r in rows]
    elif by == "company":
        count = func.count(Job.id)
        rows = (
            await session.execute(
                select(Company.slug, Company.name, count)
                .join(Job, Job.company_id == Company.id)
                .where(scoped)
                .group_by(Company.id)
                .order_by(count.desc(), Company.name, Company.id)
                .limit(limit)
            )
        ).all()
        items = [BreakdownItem(key=str(r.slug), label=str(r.name), count=r[2]) for r in rows]
    else:
        count = func.count(distinct(Posting.job_id))
        rows = (
            await session.execute(
                select(Source.key, Source.name, count)
                .select_from(Posting)
                .join(Source, Source.id == Posting.source_id)
                .join(Job, Job.id == Posting.job_id)
                .where(scoped)
                .group_by(Source.id)
                .order_by(count.desc(), Source.key)
            )
        ).all()
        items = [BreakdownItem(key=str(r.key), label=r.name, count=r[2]) for r in rows]
    return Breakdown(by=by, items=items)  # type: ignore[arg-type]


def _company_ref(row) -> CompanyRef | None:
    cid = getattr(row, "company_id", None)
    if cid is None:
        return None
    return CompanyRef(id=cid, name=str(row.company_name), slug=str(row.company_slug), logo_url=row.company_logo)


def _with_job(stmt: Select) -> Select:
    return stmt.add_columns(
        Job.id.label("job_id"),
        Job.title.label("job_title"),
        Company.id.label("company_id"),
        Company.name.label("company_name"),
        Company.slug.label("company_slug"),
        Company.logo_url.label("company_logo"),
    )


async def upcoming(session: AsyncSession, days: int = 14, *, now: datetime | None = None) -> list[UpcomingItem]:
    """Interviews (pending, within the window or overdue by at most a day), pending reminders (due within
    the window, or overdue at any age) and deadlines of tracked jobs, merged and sorted by `at`."""
    now = _utcnow(now)
    horizon = now + timedelta(days=days)
    items: list[UpcomingItem] = []

    interviews = await session.execute(
        _with_job(
            select(Interview.id, Interview.round_name, Interview.scheduled_at)
            .select_from(Interview)
            .join(Application, Application.id == Interview.application_id)
            .join(Job, Job.id == Application.job_id)
            .join(Company, Company.id == Job.company_id)
        ).where(
            Interview.outcome == "pending",
            Interview.scheduled_at.is_not(None),
            Interview.scheduled_at >= now - timedelta(days=1),
            Interview.scheduled_at <= horizon,
            Job.merged_into_job_id.is_(None),
        )
    )
    for r in interviews:
        items.append(
            UpcomingItem(
                kind="interview",
                id=r.id,
                title=f"{r.round_name} - {r.job_title}",
                at=r.scheduled_at,
                overdue=r.scheduled_at < now,
                job_id=r.job_id,
                company=_company_ref(r),
            )
        )

    reminder_stmt = (
        select(
            Reminder.id,
            Reminder.title,
            Reminder.due_at,
            Job.id.label("job_id"),
            Company.id.label("company_id"),
            Company.name.label("company_name"),
            Company.slug.label("company_slug"),
            Company.logo_url.label("company_logo"),
        )
        .select_from(Reminder)
        .outerjoin(Application, Application.id == Reminder.application_id)
        .outerjoin(Job, Job.id == Application.job_id)
        .outerjoin(Company, Company.id == Job.company_id)
        .where(Reminder.state == ReminderState.pending, Reminder.due_at <= horizon)
    )
    for r in await session.execute(reminder_stmt):
        items.append(
            UpcomingItem(
                kind="reminder",
                id=r.id,
                title=r.title,
                at=r.due_at,
                overdue=r.due_at < now,
                job_id=r.job_id,
                company=_company_ref(r),
            )
        )

    deadlines = await session.execute(
        _with_job(
            select(Job.id.label("id"), Job.deadline_at)
            .select_from(Job)
            .join(Application, Application.job_id == Job.id)
            .join(Status, Status.id == Application.status_id)
            .join(Company, Company.id == Job.company_id)
        ).where(
            Job.deadline_at.is_not(None),
            Job.deadline_at >= now,
            Job.deadline_at <= horizon,
            Job.merged_into_job_id.is_(None),
            Status.category.in_([StatusCategory.considering, StatusCategory.applied, StatusCategory.in_process]),
        )
    )
    for r in deadlines:
        items.append(
            UpcomingItem(
                kind="deadline",
                id=r.id,
                title=f"Deadline: {r.job_title}",
                at=r.deadline_at,
                overdue=False,
                job_id=r.job_id,
                company=_company_ref(r),
            )
        )

    items.sort(key=lambda i: (i.at, i.kind, i.id))
    return items
