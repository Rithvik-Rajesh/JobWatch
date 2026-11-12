"""Read side of the jobs API: filtered lists, facets, inbox summary and list-item assembly (batch loaded)."""

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, exists, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import Page, PageParams
from app.jobs import scope as scope_policy
from app.jobs.schemas import (
    CompanyRef,
    FacetValue,
    InboxSummary,
    JobFacets,
    JobFilters,
    JobListItem,
    StatusRef,
)
from app.models import (
    Application,
    Company,
    Job,
    JobTag,
    Posting,
    SalaryRecord,
    Source,
    Status,
    Tag,
)
from app.models.enums import SalaryProvenance, StatusCategory
from app.salary.schemas import SalarySummary
from app.settings.registry import LocationScopeSettings, TimezoneSettings, get_setting

NEW_WINDOW = timedelta(hours=24)
UPDATED_MIN_AFTER_DISCOVERY = timedelta(hours=1)
UPDATED_WINDOW = timedelta(hours=72)
_PROVENANCE_RANK = {
    SalaryProvenance.employer_provided: 0,
    SalaryProvenance.external_estimate: 1,
    SalaryProvenance.user_entered: 2,
}


def _salary_max_annual():
    best = func.coalesce(
        SalaryRecord.amount_max, SalaryRecord.amount_avg, SalaryRecord.amount_typical, SalaryRecord.amount_min
    )
    return best * case((SalaryRecord.period == "monthly", 12), else_=1)


def job_conditions(
    filters: JobFilters,
    cfg: LocationScopeSettings,
    *,
    now: datetime,
    skip: str | None = None,
) -> list:
    """WHERE clauses for a Job joined with Application, Company and Status. `skip` drops one facet's filter."""
    conds: list = []
    if not filters.include_merged:
        conds.append(Job.merged_into_job_id.is_(None))

    if skip != "scope":
        if not filters.scope:
            conds.append(Job.location_scope.in_(scope_policy.visible_scopes(cfg)))
        elif "all" not in filters.scope:
            conds.append(Job.location_scope.in_([s for s in filters.scope if s != "all"]))
    if filters.q and filters.q.strip():
        conds.append(Job.search_vector.op("@@")(func.websearch_to_tsquery("simple", filters.q.strip())))
    if filters.company_id and skip != "company":
        conds.append(Job.company_id.in_(filters.company_id))
    if filters.status and skip != "status":
        conds.append(Status.key.in_(filters.status))
    if filters.status_category:
        conds.append(Status.category.in_(filters.status_category))
    if filters.source and skip != "source":
        conds.append(
            exists().where(Posting.job_id == Job.id, Posting.source_id == Source.id, Source.key.in_(filters.source))
        )
    if filters.country and skip != "country":
        conds.append(Job.country_codes.overlap([c.upper() for c in filters.country]))
    if filters.location:
        conds.append(Job.location_text.ilike(f"%{filters.location}%"))
    if filters.max_experience_years is not None:
        x = filters.max_experience_years
        # "0-1 yrs" means a fresher can apply: min is 0, or the whole range is within x (1-3, 1-6, 1+ excluded)
        exp_ok = or_(
            Job.experience_min_years == 0,
            Job.experience_max_years <= x,
            and_(Job.experience_min_years <= x, Job.experience_max_years.is_(None), Job.experience_min_years < 1),
        )
        if filters.include_unknown_experience:
            exp_ok = or_(exp_ok, Job.experience_min_years.is_(None))
        conds.append(or_(exp_ok, Job.title.op("~*")(FRESHER_TITLE)))
        if x <= 2:  # junior search: drop clearly senior titles
            conds.append(~Job.title.op("~*")(SENIOR_TITLE))
    if filters.role:
        conds.append(or_(*[Job.title.ilike(f"%{r.strip()}%") for r in filters.role if r.strip()]))
    if filters.internships_only:
        conds.append(Job.title.op("~*")(INTERN_TITLE))
    if filters.tech_only:
        desc = func.coalesce(Job.description_text, "")
        conds.append(
            and_(
                ~Job.title.op("~*")(NON_TECH_TITLE),
                or_(Job.title.op("~*")(TECH_TITLE), and_(~Job.title.op("~*")(HARDWARE_TITLE), desc.op("~*")(TECH_JD))),
                ~and_(Job.title.op("~*")(HARDWARE_TITLE), ~Job.title.op("~*")(TECH_TITLE)),
            )
        )
    if filters.tab and filters.tab != "all":
        cats = {
            "not_applied": [StatusCategory.inbox, StatusCategory.considering],
            "applied": [
                StatusCategory.applied,
                StatusCategory.in_process,
                StatusCategory.offer,
                StatusCategory.closed_negative,
                StatusCategory.closed_neutral,
            ],
            "not_interested": [StatusCategory.archived],
        }
        if filters.tab == "interested":
            conds.append(Status.key.in_(["interested", "shortlisted"]))
        else:
            conds.append(Status.category.in_(cats[filters.tab]))
    if filters.employment_type and skip != "employment_type":
        conds.append(Job.employment_type.in_(filters.employment_type))
    if filters.work_arrangement and skip != "work_arrangement":
        conds.append(Job.work_arrangement.in_(filters.work_arrangement))
    if filters.availability and skip != "availability":
        conds.append(Job.availability.in_(filters.availability))
    if filters.has_salary is not None:
        has = exists().where(SalaryRecord.job_id == Job.id, SalaryRecord.is_current.is_(True))
        conds.append(has if filters.has_salary else not_(has))
    if filters.salary_min is not None:
        conds.append(
            exists().where(
                SalaryRecord.job_id == Job.id,
                SalaryRecord.is_current.is_(True),
                SalaryRecord.currency == "INR",
                _salary_max_annual() >= filters.salary_min,
            )
        )
    if filters.posted_after:
        conds.append(Job.posted_at >= filters.posted_after)
    if filters.discovered_after:
        conds.append(Job.first_seen_at >= filters.discovered_after)
    if filters.is_new is not None:
        threshold = now - NEW_WINDOW
        conds.append(Job.first_seen_at >= threshold if filters.is_new else Job.first_seen_at < threshold)
    if filters.inbox is not None:
        conds.append(Application.reviewed_at.is_(None) if filters.inbox else Application.reviewed_at.is_not(None))
    if filters.starred is not None:
        conds.append(Application.starred.is_(filters.starred))
    if filters.tag:
        conds.append(
            exists().where(JobTag.job_id == Job.id, JobTag.tag_id == Tag.id, Tag.name.in_(filters.tag)),
        )
    return conds


FRESHER_TITLE = r"\m(intern|internship|graduate|grad|fresher|entry[- ]level|trainee|apprentice|new grad)\M"
INTERN_TITLE = r"\m(intern|internship|interns)\M"
SENIOR_TITLE = r"\m(senior|sr|lead|principal|staff|manager|director|head|vp|vice president|architect|avp|pmts|smts|lmts|distinguished|fellow)\M"
SOFTWARE_TITLE = r"(software|developer|\msde\M|\mswe\M|programmer|full[ -]?stack|back[ -]?end|front[ -]?end|devops|\msre\M|cloud|mobile|android|\mios\M|web)"
TECH_TITLE = (
    SOFTWARE_TITLE[:-1]
    + r"|data (engineer|scien)|big data|analytics engineer|machine learning|\mml\M|\mai\M|artificial intelligence"
    r"|deep learning|\mnlp\M|computer vision|\mllm|genai|applied scien|research (engineer|scien)|platform engineer"
    r"|site reliability|security engineer|security research|qa engineer|test engineer|sdet|automation engineer|member of technical staff)"
)
HARDWARE_TITLE = r"(silicon|asic|\msoc\M|\mrtl\M|vlsi|analog|\mpcb\M|mechanical|electrical|hardware|layout|package design|physical design|verification|validation|circuit|chip|semiconductor|manufacturing|process engineer|aero|structur|thermal|stress|propulsion|avionic|aerospace|civil|chemical|materials|mechatronic|cabin|flight|airworthiness|maintenance|technical data|rf |wireless|antenna|optical|photonic|power electronics|embedded hardware)"
NON_TECH_TITLE = r"\m(sales|account executive|account manager|marketing|recruit|recruiter|talent|sourcer|\mhr\M|human resources|finance|accountant|legal|counsel|content|writer|editor|customer|support specialist|business development|operations|procurement|payroll|audit|compliance|relationship manager|banker|teller|graphic|designer|product manag|product design|program manag|project manag|patent|business analyst|consultant|scrum)\M"
TECH_JD = r"\m(python|java|golang|c\+\+|typescript|javascript|kubernetes|docker|microservices|pytorch|tensorflow|spark|sql|react|node\.?js|distributed systems|data pipelines|machine learning)\M"


def _order_by(filters: JobFilters):
    sort = filters.sort
    tiebreak = Job.id.desc()
    if sort == "internship_first":
        return [case((Job.title.op("~*")(INTERN_TITLE), 0), else_=1), Job.first_seen_at.desc(), Job.id.desc()]
    if sort == "discovered_asc":
        return [Job.first_seen_at.asc(), Job.id.asc()]
    if sort == "posted_desc":
        return [Job.posted_at.desc().nulls_last(), tiebreak]
    if sort == "posted_asc":
        return [Job.posted_at.asc().nulls_last(), Job.id.asc()]
    if sort == "company_asc":
        return [func.lower(Company.name), Job.id.asc()]
    if sort == "title_asc":
        return [func.lower(Job.title), Job.id.asc()]
    if sort == "status":
        return [Status.sort_order.asc(), Job.first_seen_at.desc(), tiebreak]
    if sort == "updated_desc":
        return [Job.last_changed_at.desc(), tiebreak]
    if sort == "relevance" and filters.q and filters.q.strip():
        rank = func.ts_rank(Job.search_vector, func.websearch_to_tsquery("simple", filters.q.strip()))
        return [rank.desc(), Job.first_seen_at.desc(), tiebreak]
    return [Job.first_seen_at.desc(), tiebreak]


def _base():
    return (
        select(Job, Application, Company, Status)
        .join(Application, Application.job_id == Job.id)
        .join(Company, Company.id == Job.company_id)
        .join(Status, Status.id == Application.status_id)
    )


def _base_count(*cols):
    return (
        select(*cols)
        .select_from(Job)
        .join(Application, Application.job_id == Job.id)
        .join(Company, Company.id == Job.company_id)
        .join(Status, Status.id == Application.status_id)
    )


async def build_items(
    session: AsyncSession,
    rows: Sequence[tuple[Job, Application, Company, Status]],
    *,
    now: datetime,
) -> list[JobListItem]:
    """Assemble list items for already-loaded rows with three batched queries (sources, salary, tags)."""
    if not rows:
        return []
    ids = [r[0].id for r in rows]
    sources: dict[int, list[str]] = defaultdict(list)
    for job_id, key in (
        await session.execute(
            select(Posting.job_id, Source.key)
            .join(Source, Source.id == Posting.source_id)
            .where(Posting.job_id.in_(ids))
            .order_by(Posting.id)
        )
    ).all():
        if key not in sources[job_id]:
            sources[job_id].append(key)
    salaries: dict[int, SalaryRecord] = {}
    records = (
        await session.scalars(
            select(SalaryRecord)
            .where(SalaryRecord.job_id.in_(ids), SalaryRecord.is_current.is_(True))
            .order_by(SalaryRecord.id.desc())
        )
    ).all()
    for rec in sorted(records, key=lambda r: _PROVENANCE_RANK[r.provenance], reverse=True):
        salaries[rec.job_id] = rec  # later (better-ranked) assignments win
    tags: dict[int, list[str]] = defaultdict(list)
    for job_id, name in (
        await session.execute(
            select(JobTag.job_id, Tag.name).join(Tag, Tag.id == JobTag.tag_id).where(JobTag.job_id.in_(ids))
        )
    ).all():
        tags[job_id].append(str(name))

    items: list[JobListItem] = []
    for job, app, company, status in rows:
        rec = salaries.get(job.id)
        salary = (
            SalarySummary(
                provenance=rec.provenance,
                currency=rec.currency,
                period=rec.period,
                amount_min=rec.amount_min,
                amount_max=rec.amount_max,
                raw_text=rec.raw_text,
            )
            if rec
            else None
        )
        items.append(
            JobListItem(
                id=job.id,
                title=job.title,
                company=CompanyRef.model_validate(company),
                location_text=job.location_text,
                country_codes=list(job.country_codes),
                location_scope=job.location_scope,
                employment_type=job.employment_type,
                work_arrangement=job.work_arrangement,
                experience_min_years=job.experience_min_years,
                experience_max_years=job.experience_max_years,
                posted_at=job.posted_at,
                posted_at_precision=job.posted_at_precision,
                first_seen_at=job.first_seen_at,
                last_changed_at=job.last_changed_at,
                availability=job.availability,
                apply_url=job.apply_url,
                sources=sources.get(job.id, []),
                salary=salary,
                status=StatusRef.model_validate(status),
                application_id=app.id,
                row_version=app.row_version,
                reviewed=app.reviewed_at is not None,
                starred=app.starred,
                is_new=job.first_seen_at >= now - NEW_WINDOW,
                is_updated=(
                    job.last_changed_at > job.first_seen_at + UPDATED_MIN_AFTER_DISCOVERY
                    and now - job.last_changed_at <= UPDATED_WINDOW
                ),
                tags=sorted(tags.get(job.id, [])),
            )
        )
    return items


async def items_for_job_ids(session: AsyncSession, job_ids: Sequence[int], *, now: datetime) -> dict[int, JobListItem]:
    if not job_ids:
        return {}
    rows = (await session.execute(_base().where(Job.id.in_(set(job_ids))))).all()
    return {item.id: item for item in await build_items(session, [tuple(r) for r in rows], now=now)}


async def list_jobs(
    session: AsyncSession,
    filters: JobFilters,
    paging: PageParams,
    *,
    now: datetime | None = None,
) -> Page[JobListItem]:
    now = now or datetime.now(UTC)
    cfg = await get_setting(session, LocationScopeSettings)
    conds = job_conditions(filters, cfg, now=now)
    total = await session.scalar(_base_count(func.count()).where(*conds)) or 0
    rows = (
        await session.execute(
            _base().where(*conds).order_by(*_order_by(filters)).limit(paging.page_size).offset(paging.offset)
        )
    ).all()
    items = await build_items(session, [tuple(r) for r in rows], now=now)
    return Page[JobListItem](items=items, page=paging.page, page_size=paging.page_size, total=total)


def _label(value: str) -> str:
    return value.replace("_", " ").title()


async def job_facets(session: AsyncSession, filters: JobFilters, *, now: datetime | None = None) -> JobFacets:
    now = now or datetime.now(UTC)
    cfg = await get_setting(session, LocationScopeSettings)

    def where(skip: str) -> list:
        return job_conditions(filters, cfg, now=now, skip=skip)

    async def grouped(skip: str, value_col, label_col=None, *, join_sources: bool = False) -> list[FacetValue]:
        label_col = label_col if label_col is not None else value_col
        count = func.count(func.distinct(Job.id))
        stmt = _base_count(value_col, label_col, count)
        if join_sources:
            stmt = stmt.join(Posting, Posting.job_id == Job.id).join(Source, Source.id == Posting.source_id)
        stmt = stmt.where(*where(skip)).group_by(value_col, label_col).order_by(count.desc(), label_col)
        return [
            FacetValue(value=str(getattr(v, "value", v)), label=str(getattr(lbl, "value", lbl)), count=c)
            for v, lbl, c in (await session.execute(stmt)).all()
        ]

    def enum_facet(skip: str, col):
        return grouped(skip, col)

    company = await grouped("company", Company.id, Company.name)
    status = await grouped("status", Status.key, Status.label)
    source = await grouped("source", Source.key, Source.name, join_sources=True)
    return JobFacets(
        company=company,
        status=status,
        source=source,
        country=await _country_facet(session, where("country")),
        employment_type=[_pretty(f) for f in await enum_facet("employment_type", Job.employment_type)],
        work_arrangement=[_pretty(f) for f in await enum_facet("work_arrangement", Job.work_arrangement)],
        availability=[_pretty(f) for f in await enum_facet("availability", Job.availability)],
        scope=[_pretty(f) for f in await enum_facet("scope", Job.location_scope)],
    )


def _pretty(f: FacetValue) -> FacetValue:
    return FacetValue(value=f.value, label=_label(f.label), count=f.count)


async def _country_facet(session: AsyncSession, conds: list) -> list[FacetValue]:
    exploded = _base_count(Job.id.label("job_id"), func.unnest(Job.country_codes).label("cc")).where(*conds).subquery()
    count = func.count(func.distinct(exploded.c.job_id))
    stmt = select(exploded.c.cc, count).group_by(exploded.c.cc).order_by(count.desc(), exploded.c.cc)
    return [FacetValue(value=v, label=v, count=c) for v, c in (await session.execute(stmt)).all()]


async def inbox_summary(session: AsyncSession, *, now: datetime | None = None) -> InboxSummary:
    now = now or datetime.now(UTC)
    cfg = await get_setting(session, LocationScopeSettings)
    tz = ZoneInfo((await get_setting(session, TimezoneSettings)).name)
    local_midnight = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    conds = job_conditions(JobFilters(inbox=True), cfg, now=now)
    unreviewed = await session.scalar(_base_count(func.count()).where(*conds)) or 0
    today = (
        await session.scalar(
            _base_count(func.count()).where(*conds, Job.first_seen_at >= local_midnight.astimezone(UTC))
        )
        or 0
    )
    count = func.count()
    by_company = (
        await session.execute(
            _base_count(Company.id, Company.name, count)
            .where(*conds)
            .group_by(Company.id, Company.name)
            .order_by(count.desc(), Company.name)
            .limit(10)
        )
    ).all()
    return InboxSummary(
        unreviewed=unreviewed,
        unreviewed_today=today,
        by_company=[FacetValue(value=str(cid), label=str(name), count=c) for cid, name, c in by_company],
    )
