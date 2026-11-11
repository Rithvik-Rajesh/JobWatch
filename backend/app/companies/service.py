"""Companies service: CRUD, aliases, slugging, archive, manual scrape (queued run), runs and history.

Services use flush() only; the request-scoped session commits.
"""

import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import Date, Text, case, cast, delete, func, literal, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.companies.schemas import (
    CompanyCreate,
    CompanyListItem,
    CompanyOut,
    CompanyUpdate,
    DiscoveryHistoryPoint,
    ScrapeRunOut,
)
from app.core.errors import Conflict, NotFound, ValidationFailed
from app.core.pagination import Page, PageParams
from app.models import Application, CareerSite, Company, CompanyAlias, Job, ScrapeRun, Status
from app.models.enums import ExtractorKind, HealthStatus, StatusCategory
from app.settings.registry import TimezoneSettings, get_setting
from app.sites import service as sites_service

SORTS = ("name_asc", "name_desc", "open_jobs_desc", "last_success_desc", "health")
_HEALTH_RANK = {
    HealthStatus.blocked: -1,
    HealthStatus.failing: 0,
    HealthStatus.degraded: 1,
    HealthStatus.unknown: 2,
    HealthStatus.paused: 3,
    HealthStatus.healthy: 4,
}


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


# ---------------------------------------------------------------- helpers


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "company"


async def unique_slug(session: AsyncSession, name: str) -> str:
    base = slugify(name)
    rows = await session.scalars(
        select(func.lower(Company.slug)).where(
            (func.lower(Company.slug) == base) | func.lower(Company.slug).like(f"{base}-%")
        )
    )
    taken = set(rows.all())
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def clean_aliases(name: str, aliases: list[str]) -> list[str]:
    seen = {name.strip().casefold()}
    out: list[str] = []
    for a in aliases:
        a = a.strip()
        if a and a.casefold() not in seen:
            seen.add(a.casefold())
            out.append(a)
    return out


def config_url(config) -> str | None:
    url = getattr(config, "url", None) or getattr(config, "listing_url", None)
    return str(url) if url else None


async def assert_name_free(session: AsyncSession, name: str, *, exclude_id: int | None = None) -> None:
    low = name.strip().lower()
    q = select(Company.id).where(func.lower(Company.name) == low)
    if exclude_id is not None:
        q = q.where(Company.id != exclude_id)
    if await session.scalar(q.limit(1)) is not None:
        raise Conflict(f"A company named '{name}' already exists")
    q2 = select(CompanyAlias.company_id).where(func.lower(CompanyAlias.alias) == low)
    if exclude_id is not None:
        q2 = q2.where(CompanyAlias.company_id != exclude_id)
    if await session.scalar(q2.limit(1)) is not None:
        raise Conflict(f"'{name}' is already an alias of another company")


async def assert_alias_free(session: AsyncSession, alias: str, *, exclude_id: int | None = None) -> None:
    # Same namespace as names: an alias may not equal another company's name or alias.
    await assert_name_free(session, alias, exclude_id=exclude_id)


async def get_aliases(session: AsyncSession, company_id: int) -> list[str]:
    rows = await session.scalars(
        select(CompanyAlias.alias).where(CompanyAlias.company_id == company_id).order_by(func.lower(CompanyAlias.alias))
    )
    return list(rows.all())


async def add_aliases(session: AsyncSession, company: Company, aliases: list[str]) -> None:
    existing = {a.casefold() for a in await get_aliases(session, company.id)}
    for alias in clean_aliases(company.name, aliases):
        if alias.casefold() in existing:
            continue
        await assert_alias_free(session, alias, exclude_id=company.id)
        session.add(CompanyAlias(company_id=company.id, alias=alias))
        existing.add(alias.casefold())
    await session.flush()


async def replace_aliases(session: AsyncSession, company: Company, aliases: list[str]) -> None:
    cleaned = clean_aliases(company.name, aliases)
    for alias in cleaned:
        await assert_alias_free(session, alias, exclude_id=company.id)
    await session.execute(delete(CompanyAlias).where(CompanyAlias.company_id == company.id))
    await session.flush()
    for alias in cleaned:
        session.add(CompanyAlias(company_id=company.id, alias=alias))
    await session.flush()


async def company_out(session: AsyncSession, company: Company) -> CompanyOut:
    await session.refresh(company)  # server-side defaults / onupdate timestamps
    out = CompanyOut.model_validate(company)
    out.aliases = await get_aliases(session, company.id)
    return out


async def _get(session: AsyncSession, company_id: int) -> Company:
    company = await session.get(Company, company_id)
    if company is None:
        raise NotFound("Company not found")
    return company


# ---------------------------------------------------------------- create


async def insert_company(
    session: AsyncSession,
    *,
    name: str,
    slug: str | None = None,
    website_url: str | None = None,
    careers_url: str | None = None,
    logo_url: str | None = None,
    description: str | None = None,
    notes: str | None = None,
    monitoring_enabled: bool = False,
    check_interval_min: int = 30,
    config=None,
    aliases: list[str] | None = None,
    now: datetime | None = None,
) -> Company:
    now = _now(now)
    name = name.strip()
    await assert_name_free(session, name)
    if slug is not None:
        if await session.scalar(select(Company.id).where(func.lower(Company.slug) == slug.lower())) is not None:
            raise Conflict(f"Slug '{slug}' is already in use")
    else:
        slug = await unique_slug(session, name)
    extractor = ExtractorKind(config.kind) if config is not None else ExtractorKind.none
    careers_url = careers_url or (config_url(config) if config is not None else None)
    if extractor != ExtractorKind.none and not careers_url:
        raise ValidationFailed("careers_url is required when an extractor is configured")
    company = Company(
        name=name,
        slug=slug,
        website_url=website_url,
        careers_url=careers_url,
        logo_url=logo_url,
        description=description,
        notes=notes,
        monitoring_enabled=monitoring_enabled,
        check_interval_min=check_interval_min,
        extractor=extractor,
        extractor_config=config.model_dump(mode="json") if config is not None else {},
        next_check_at=now if monitoring_enabled and extractor != ExtractorKind.none else None,
    )
    session.add(company)
    await session.flush()
    await add_aliases(session, company, aliases or [])
    if config is not None:  # Phase 3: the configured extractor lives on the company's `main` career site
        await sites_service.create_site_row(
            session,
            company,
            label=sites_service.MAIN_LABEL,
            platform=extractor,
            config=config.model_dump(mode="json"),
            careers_url=careers_url,
            monitoring_enabled=monitoring_enabled,
            check_interval_min=check_interval_min if check_interval_min != 30 else None,
            now=now,
        )
        await sites_service.sync_company_summary(session, company.id)
    return company


def _s(url) -> str | None:
    return str(url) if url is not None else None


async def create_company(session: AsyncSession, body: CompanyCreate, *, now: datetime | None = None) -> CompanyOut:
    company = await insert_company(
        session,
        name=body.name,
        slug=body.slug,
        website_url=_s(body.website_url),
        careers_url=_s(body.careers_url),
        logo_url=_s(body.logo_url),
        description=body.description,
        notes=body.notes,
        monitoring_enabled=body.monitoring_enabled,
        check_interval_min=body.check_interval_min,
        config=body.extractor_config,
        aliases=body.aliases,
        now=now,
    )
    return await company_out(session, company)


# ---------------------------------------------------------------- read


async def get_company(session: AsyncSession, company_id: int) -> CompanyOut:
    return await company_out(session, await _get(session, company_id))


def _tracked_subquery():
    return (
        select(func.count(Job.id))
        .join(Application, Application.job_id == Job.id)
        .join(Status, Status.id == Application.status_id)
        .where(
            Job.company_id == Company.id,
            Job.merged_into_job_id.is_(None),
            Status.category != StatusCategory.inbox,
        )
        .correlate(Company)
        .scalar_subquery()
    )


async def list_companies(
    session: AsyncSession,
    paging: PageParams,
    *,
    q: str | None = None,
    monitoring: bool | None = None,
    health: list[HealthStatus] | None = None,
    extractor: list[ExtractorKind] | None = None,
    include_archived: bool = False,
    sort: str = "name_asc",
) -> Page[CompanyListItem]:
    if sort not in SORTS:
        raise ValidationFailed(f"Unknown sort '{sort}'", details={"allowed": list(SORTS)})
    conds = []
    if not include_archived:
        conds.append(Company.archived_at.is_(None))
    if q and q.strip():
        term = q.strip()
        alias_match = (
            select(CompanyAlias.company_id)
            .where(CompanyAlias.company_id == Company.id, CompanyAlias.alias.icontains(term, autoescape=True))
            .exists()
        )
        conds.append(Company.name.icontains(term, autoescape=True) | alias_match)
    if monitoring is not None:
        conds.append(Company.monitoring_enabled.is_(monitoring))
    if health:
        conds.append(Company.health.in_(health))
    if extractor:
        conds.append(Company.extractor.in_(extractor))

    total = await session.scalar(select(func.count()).select_from(Company).where(*conds)) or 0
    tracked = _tracked_subquery().label("tracked")
    name_key = func.lower(Company.name)
    match sort:
        case "name_asc":
            order = [name_key, Company.id]
        case "name_desc":
            order = [name_key.desc(), Company.id]
        case "open_jobs_desc":
            order = [Company.open_jobs_count.desc(), name_key, Company.id]
        case "last_success_desc":
            order = [Company.last_success_at.desc().nulls_last(), name_key, Company.id]
        case _:  # health: worst first
            rank = case(_HEALTH_RANK, value=Company.health, else_=5)
            order = [rank, name_key, Company.id]
    rows = await session.execute(
        select(Company, tracked).where(*conds).order_by(*order).limit(paging.page_size).offset(paging.offset)
    )
    items = [CompanyListItem.model_validate(c).model_copy(update={"tracked_jobs_count": n or 0}) for c, n in rows.all()]
    return Page[CompanyListItem](items=items, page=paging.page, page_size=paging.page_size, total=total)


# ---------------------------------------------------------------- update / archive


async def update_company(
    session: AsyncSession, company_id: int, body: CompanyUpdate, *, now: datetime | None = None
) -> CompanyOut:
    now = _now(now)
    company = await _get(session, company_id)
    provided = body.model_fields_set

    if "name" in provided and body.name is not None:
        new_name = body.name.strip()
        if new_name.casefold() != company.name.casefold():
            await assert_name_free(session, new_name, exclude_id=company.id)
        company.name = new_name
    for field in ("website_url", "careers_url", "logo_url"):
        if field in provided:
            setattr(company, field, _s(getattr(body, field)))
    for field in ("description", "notes"):
        if field in provided:
            setattr(company, field, getattr(body, field))
    if "check_interval_min" in provided and body.check_interval_min is not None:
        company.check_interval_min = body.check_interval_min

    # Phase 3: extractor / schedule live on career sites. Company fields are read-only summaries of them;
    # these company-level inputs are applied to the `main` site.
    main = await sites_service.get_main_site(session, company.id, include_archived=True)
    if body.clear_extractor:
        if main is not None and main.archived_at is None:
            main.archived_at = now
            main.monitoring_enabled = False
            main.next_check_at = None
    elif body.extractor_config is not None:
        cfg = body.extractor_config
        careers_url = company.careers_url or config_url(cfg)
        if not careers_url:
            raise ValidationFailed("careers_url is required when an extractor is configured")
        company.careers_url = careers_url
        if main is None:
            await session.flush()
            main = await sites_service.create_site_row(
                session,
                company,
                label=sites_service.MAIN_LABEL,
                platform=ExtractorKind(cfg.kind),
                config=cfg.model_dump(mode="json"),
                careers_url=careers_url,
                monitoring_enabled=company.monitoring_enabled,
                check_interval_min=company.check_interval_min if company.check_interval_min != 30 else None,
                now=now,
            )
        else:
            main.platform = ExtractorKind(cfg.kind)
            main.config = cfg.model_dump(mode="json")
            main.careers_url = careers_url
            if main.archived_at is not None:
                main.archived_at = None
                main.monitoring_enabled = company.monitoring_enabled
                main.next_check_at = now if company.monitoring_enabled else None
            elif company.monitoring_enabled and main.monitoring_enabled and main.next_check_at is None:
                main.next_check_at = now

    if "monitoring_enabled" in provided and body.monitoring_enabled is not None:
        company.monitoring_enabled = body.monitoring_enabled
    if main is not None and main.archived_at is None:
        if "check_interval_min" in provided and body.check_interval_min is not None:
            main.check_interval_min = body.check_interval_min
        if "monitoring_enabled" in provided and body.monitoring_enabled is not None:
            sites_service.set_monitoring(main, body.monitoring_enabled, now)
    await session.flush()
    if main is not None:
        await sites_service.sync_company_summary(session, company.id)
    await session.flush()
    if body.aliases is not None:
        await replace_aliases(session, company, body.aliases)
    return await company_out(session, company)


async def archive_company(session: AsyncSession, company_id: int, *, now: datetime | None = None) -> CompanyOut:
    company = await _get(session, company_id)
    if company.archived_at is None:
        company.archived_at = _now(now)
    company.monitoring_enabled = False
    company.next_check_at = None
    for site in (await session.scalars(select(CareerSite).where(CareerSite.company_id == company.id))).all():
        site.monitoring_enabled = False
        site.next_check_at = None
    await session.flush()
    await sites_service.sync_company_summary(session, company.id)
    return await company_out(session, company)


async def unarchive_company(session: AsyncSession, company_id: int) -> CompanyOut:
    company = await _get(session, company_id)
    company.archived_at = None  # monitoring stays disabled; the user re-enables it explicitly
    await session.flush()
    return await company_out(session, company)


# ---------------------------------------------------------------- scrape / runs / history


async def trigger_scrape(session: AsyncSession, company_id: int, *, now: datetime | None = None) -> ScrapeRunOut:
    """Manual run of the company's primary (`main`, else first) career site."""
    company = await _get(session, company_id)
    if company.archived_at is not None:
        raise Conflict("Company is archived")
    site = await sites_service.primary_site(session, company.id)
    if site is None or site.platform == ExtractorKind.none:
        raise Conflict("Company has no extractor configured")
    return await sites_service.scrape_site(session, site.id, now=now)


async def company_runs(session: AsyncSession, company_id: int, paging: PageParams) -> Page[ScrapeRunOut]:
    await _get(session, company_id)
    total = await session.scalar(select(func.count()).select_from(ScrapeRun).where(ScrapeRun.company_id == company_id))
    rows = await session.scalars(
        select(ScrapeRun)
        .where(ScrapeRun.company_id == company_id)
        .order_by(ScrapeRun.queued_at.desc(), ScrapeRun.id.desc())
        .limit(paging.page_size)
        .offset(paging.offset)
    )
    return Page[ScrapeRunOut](
        items=[ScrapeRunOut.model_validate(r) for r in rows.all()],
        page=paging.page,
        page_size=paging.page_size,
        total=total or 0,
    )


async def company_history(
    session: AsyncSession, company_id: int, days: int, *, now: datetime | None = None
) -> list[DiscoveryHistoryPoint]:
    """Per-day discovered (first_seen_at) and closed (closed_at) counts in the user timezone."""
    await _get(session, company_id)
    tz_name = (await get_setting(session, TimezoneSettings)).name
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as e:
        raise ValidationFailed(f"Unknown timezone setting '{tz_name}'") from e
    today = _now(now).astimezone(tz).date()
    start = today - timedelta(days=days - 1)

    async def counts(column) -> dict[date, int]:
        day = cast(func.timezone(literal(tz_name, Text), column), Date)
        rows = await session.execute(
            select(day, func.count())
            .where(Job.company_id == company_id, Job.merged_into_job_id.is_(None), column.is_not(None), day >= start)
            .group_by(text("1"))
        )
        return {d: n for d, n in rows.all()}

    discovered = await counts(Job.first_seen_at)
    closed = await counts(Job.closed_at)
    points = []
    for i in range(days):
        d = start + timedelta(days=i)
        points.append(
            DiscoveryHistoryPoint(date=d.isoformat(), discovered=discovered.get(d, 0), closed=closed.get(d, 0))
        )
    return points
