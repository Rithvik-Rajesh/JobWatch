"""Career sites service (Phase 3): CRUD, resume, dry-run test, manual scrape, runs, scheduler status.

Services use flush() only; the request-scoped session commits. The scrape worker (`app.worker`) and the polite
HTTP client (`app.discovery.http`) belong to the Runtime agent and are imported lazily so this module works
(and is testable) without them.
"""

import inspect
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.companies.schemas import ScrapeRunOut
from app.core.errors import AppError, Conflict, NotFound, ValidationFailed
from app.core.logging import get_logger
from app.core.pagination import Page, PageParams
from app.discovery.adapters import get_adapter
from app.discovery.configs import CONFIG_MODELS, validate_config
from app.models import CareerSite, Company, ScrapeRun, WorkerHeartbeat
from app.models.enums import ExtractorKind, HealthStatus, IndiaCoverage, RunStatus, RunTrigger, SupportClass
from app.settings.registry import SchedulerSettings, get_setting
from app.sites.schemas import (
    CareerSiteCreate,
    CareerSiteOut,
    CareerSiteUpdate,
    DryRunResult,
    DryRunSample,
    SchedulerStatus,
)

log = get_logger(__name__)


class ServiceUnavailable(AppError):
    status_code = 503
    code = "queue_unavailable"


# worst first (used for the company-level health summary)
HEALTH_SEVERITY = {
    HealthStatus.blocked: 0,
    HealthStatus.failing: 1,
    HealthStatus.degraded: 2,
    HealthStatus.unknown: 3,
    HealthStatus.paused: 4,
    HealthStatus.healthy: 5,
}
MAIN_LABEL = "main"
DRY_RUN_SAMPLE_LIMIT = 50


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


# ------------------------------------------------------------------------------------------ helpers
def derive_careers_url(platform: ExtractorKind | str, config: dict[str, Any]) -> str | None:
    """Best-effort public careers URL for a platform config (None when it cannot be derived)."""
    for key in ("url", "listing_url", "base_url"):
        if config.get(key):
            return str(config[key])
    platform = ExtractorKind(platform)
    match platform:
        case ExtractorKind.greenhouse:
            return f"https://boards.greenhouse.io/{config['board_token']}" if config.get("board_token") else None
        case ExtractorKind.lever:
            return f"https://jobs.lever.co/{config['company']}" if config.get("company") else None
        case ExtractorKind.ashby:
            return f"https://jobs.ashbyhq.com/{config['org']}" if config.get("org") else None
        case ExtractorKind.oracle_hcm | ExtractorKind.eightfold:
            return f"https://{config['host']}" if config.get("host") else None
    return None


def validated_config(platform: ExtractorKind | str, config: dict[str, Any]) -> dict[str, Any]:
    """Validate against the platform's config model (ValidationFailed -> 422) and return the stored document."""
    platform = ExtractorKind(platform)
    if platform == ExtractorKind.none:
        return {}
    if platform.value not in CONFIG_MODELS:
        raise ValidationFailed(f"Platform '{platform.value}' has no configuration model")
    try:
        return validate_config(platform.value, config).model_dump(mode="json")
    except ValidationError as e:
        raise ValidationFailed(
            f"Invalid {platform.value} configuration", details=e.errors(include_url=False, include_context=False)
        ) from e


def adapter_available(platform: ExtractorKind | str, config: dict[str, Any] | None = None) -> bool:
    try:
        platform = ExtractorKind(platform)
        get_adapter(platform, (config or {}).get("module") if platform == ExtractorKind.custom else None)
        return True
    except Exception:
        return False


async def _default_interval(session: AsyncSession) -> int:
    return (await get_setting(session, SchedulerSettings)).default_interval_min


def site_out(site: CareerSite, company_name: str, default_interval: int) -> CareerSiteOut:
    data = {name: getattr(site, name) for name in CareerSiteOut.model_fields if hasattr(site, name)}
    data.update(
        company_name=company_name,
        effective_interval_min=site.check_interval_min or default_interval,
        adapter_available=adapter_available(site.platform, site.config),
    )
    return CareerSiteOut.model_validate(data)


async def _get(session: AsyncSession, site_id: int) -> CareerSite:
    site = await session.get(CareerSite, site_id)
    if site is None:
        raise NotFound("Career site not found")
    return site


async def _company(session: AsyncSession, company_id: int) -> Company:
    company = await session.get(Company, company_id)
    if company is None:
        raise NotFound("Company not found")
    return company


async def _out(session: AsyncSession, site: CareerSite) -> CareerSiteOut:
    await session.refresh(site)  # server-side defaults / onupdate timestamps
    company = await _company(session, site.company_id)
    return site_out(site, company.name, await _default_interval(session))


async def get_main_site(session: AsyncSession, company_id: int, *, include_archived: bool = False) -> CareerSite | None:
    query = select(CareerSite).where(CareerSite.company_id == company_id, CareerSite.label == MAIN_LABEL)
    if not include_archived:
        query = query.where(CareerSite.archived_at.is_(None))
    return await session.scalar(query)


async def primary_site(session: AsyncSession, company_id: int) -> CareerSite | None:
    """`main` if present, else the first (lowest id) non-archived site."""
    sites = list(
        (
            await session.scalars(
                select(CareerSite)
                .where(CareerSite.company_id == company_id, CareerSite.archived_at.is_(None))
                .order_by(CareerSite.id)
            )
        ).all()
    )
    return next((s for s in sites if s.label == MAIN_LABEL), sites[0] if sites else None)


# --------------------------------------------------------------------- company summary (read-only fields)
async def sync_company_summary(session: AsyncSession, company_id: int) -> None:
    """Refresh the legacy `companies.extractor*` / health / schedule columns from the company's sites.

    primary = `main` (else the first site); health = worst across non-archived sites; counters/timestamps are
    aggregated. `companies.open_jobs_count` stays owned by ingest (it counts the company's open JOBS across all
    sources, which is not the sum of per-portal posting counts)."""
    company = await _company(session, company_id)
    sites = list(
        (
            await session.scalars(
                select(CareerSite)
                .where(CareerSite.company_id == company_id, CareerSite.archived_at.is_(None))
                .order_by(CareerSite.id)
            )
        ).all()
    )
    primary = next((s for s in sites if s.label == MAIN_LABEL), sites[0] if sites else None)
    if primary is None:
        company.extractor, company.extractor_config, company.next_check_at = ExtractorKind.none, {}, None
        await session.flush()
        return
    if not company.careers_url and primary.careers_url:
        company.careers_url = primary.careers_url
    if company.careers_url or primary.platform == ExtractorKind.none:
        company.extractor, company.extractor_config = primary.platform, primary.config
    else:  # companies.extractor_requires_url
        company.extractor, company.extractor_config = ExtractorKind.none, {}
    worst = min(sites, key=lambda s: HEALTH_SEVERITY[s.health])
    company.health = worst.health
    company.consecutive_failures = max(s.consecutive_failures for s in sites)
    company.last_error = worst.last_error if worst.health != HealthStatus.healthy else None
    checked = [s.last_checked_at for s in sites if s.last_checked_at]
    succeeded = [s.last_success_at for s in sites if s.last_success_at]
    company.last_checked_at = max(checked) if checked else None
    company.last_success_at = max(succeeded) if succeeded else None
    due = [s.next_check_at for s in sites if s.monitoring_enabled and s.next_check_at]
    company.next_check_at = min(due) if due else None
    await session.flush()


# ---------------------------------------------------------------------------------------------- CRUD
async def create_site_row(
    session: AsyncSession,
    company: Company,
    *,
    label: str,
    platform: ExtractorKind,
    config: dict[str, Any],
    careers_url: str | None = None,
    monitoring_enabled: bool = False,
    check_interval_min: int | None = None,
    now: datetime | None = None,
    **extra: Any,
) -> CareerSite:
    """Insert a site from an already-validated config (shared by the API, company compat and registry import).

    `support_class`: the registry import passes the verified classification. Sites the user adds directly
    (API / company form) are classified from adapter availability: shared platform -> auto, custom module
    -> adapter, otherwise manual. robots.txt and block detection still apply at run time.
    """
    now = _now(now)
    if "support_class" not in extra:
        if not adapter_available(platform, config):
            extra["support_class"] = SupportClass.manual
        elif ExtractorKind(platform) == ExtractorKind.custom:
            extra["support_class"] = SupportClass.adapter
        else:
            extra["support_class"] = SupportClass.auto
        extra.setdefault("verification_status", "user_added")
    if await session.scalar(
        select(CareerSite.id).where(CareerSite.company_id == company.id, func.lower(CareerSite.label) == label.lower())
    ):
        raise Conflict(f"Company already has a career site labelled '{label}'")
    site = CareerSite(
        company_id=company.id,
        label=label,
        platform=platform,
        config=config,
        careers_url=careers_url or derive_careers_url(platform, config),
        monitoring_enabled=monitoring_enabled,
        check_interval_min=check_interval_min,
        next_check_at=now if monitoring_enabled else None,
        **extra,
    )
    session.add(site)
    await session.flush()
    return site


async def create_site(
    session: AsyncSession, company_id: int, body: CareerSiteCreate, *, now: datetime | None = None
) -> CareerSiteOut:
    company = await _company(session, company_id)
    if company.archived_at is not None:
        raise Conflict("Company is archived")
    config = validated_config(body.platform, body.config)
    site = await create_site_row(
        session,
        company,
        label=body.label,
        platform=body.platform,
        config=config,
        careers_url=str(body.careers_url) if body.careers_url else None,
        monitoring_enabled=body.monitoring_enabled,
        check_interval_min=body.check_interval_min,
        now=now,
    )
    await sync_company_summary(session, company_id)
    return await _out(session, site)


async def list_sites(
    session: AsyncSession,
    paging: PageParams,
    *,
    company_id: int | None = None,
    platform: list[ExtractorKind] | None = None,
    health: list[HealthStatus] | None = None,
    support_class: list[SupportClass] | None = None,
    india_coverage: list[IndiaCoverage] | None = None,
    monitoring: bool | None = None,
    include_archived: bool = False,
) -> Page[CareerSiteOut]:
    conds = []
    if company_id is not None:
        conds.append(CareerSite.company_id == company_id)
    if platform:
        conds.append(CareerSite.platform.in_(platform))
    if health:
        conds.append(CareerSite.health.in_(health))
    if support_class:
        conds.append(CareerSite.support_class.in_(support_class))
    if india_coverage:
        conds.append(CareerSite.india_coverage.in_(india_coverage))
    if monitoring is not None:
        conds.append(CareerSite.monitoring_enabled.is_(monitoring))
    if not include_archived:
        conds.append(CareerSite.archived_at.is_(None))
    total = await session.scalar(select(func.count()).select_from(CareerSite).where(*conds)) or 0
    rows = (
        await session.execute(
            select(CareerSite, Company.name)
            .join(Company, Company.id == CareerSite.company_id)
            .where(*conds)
            .order_by(func.lower(Company.name), CareerSite.label, CareerSite.id)
            .limit(paging.page_size)
            .offset(paging.offset)
        )
    ).all()
    interval = await _default_interval(session)
    return Page[CareerSiteOut](
        items=[site_out(site, name, interval) for site, name in rows],
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


async def company_sites(session: AsyncSession, company_id: int) -> list[CareerSiteOut]:
    company = await _company(session, company_id)
    sites = (
        await session.scalars(
            select(CareerSite)
            .where(CareerSite.company_id == company_id, CareerSite.archived_at.is_(None))
            .order_by(CareerSite.id)
        )
    ).all()
    interval = await _default_interval(session)
    return [site_out(s, company.name, interval) for s in sites]


async def get_site(session: AsyncSession, site_id: int) -> CareerSiteOut:
    return await _out(session, await _get(session, site_id))


AUTOMATABLE = (SupportClass.auto, SupportClass.adapter)


def _assert_automatable(site: CareerSite) -> None:
    """Policy guard: sites the verification classified as not_reliable (blocked / robots-disallowed /
    token-gated / provenance unconfirmed, e.g. Palo Alto Networks) or manual are NEVER fetched
    automatically — no monitoring, no dry run, no manual scrape. Reclassifying requires new evidence
    (data/companies.csv + registry import), not an API toggle."""
    if site.support_class not in AUTOMATABLE:
        reason = (site.evidence or {}).get("support_reason") or "see data/companies.csv"
        raise Conflict(
            f"Career site is classified '{site.support_class.value}' (bookmarklet/manual only): {reason}",
            code="not_automatable",
        )


def _assert_can_monitor(site: CareerSite) -> None:
    _assert_automatable(site)
    if site.archived_at is not None:
        raise Conflict("Career site is archived")
    if site.health == HealthStatus.blocked or site.paused_reason:
        raise Conflict("Career site is blocked/paused; resume it first (POST /sites/{id}/resume)")


def set_monitoring(site: CareerSite, enabled: bool, now: datetime) -> None:
    if enabled:
        _assert_can_monitor(site)
        site.monitoring_enabled = True
        if site.next_check_at is None:
            site.next_check_at = now
    else:
        site.monitoring_enabled = False
        site.next_check_at = None


async def update_site(
    session: AsyncSession, site_id: int, body: CareerSiteUpdate, *, now: datetime | None = None
) -> CareerSiteOut:
    now = _now(now)
    site = await _get(session, site_id)
    provided = body.model_fields_set
    if "label" in provided and body.label is not None and body.label != site.label:
        taken = await session.scalar(
            select(CareerSite.id).where(
                CareerSite.company_id == site.company_id,
                func.lower(CareerSite.label) == body.label.lower(),
                CareerSite.id != site.id,
            )
        )
        if taken:
            raise Conflict(f"Company already has a career site labelled '{body.label}'")
        site.label = body.label
    if "config" in provided and body.config is not None:
        site.config = validated_config(site.platform, body.config)
        site.careers_url = site.careers_url or derive_careers_url(site.platform, site.config)
    if "careers_url" in provided and body.careers_url is not None:
        site.careers_url = str(body.careers_url)
    if body.clear_check_interval:
        site.check_interval_min = None
    elif "check_interval_min" in provided and body.check_interval_min is not None:
        site.check_interval_min = body.check_interval_min
    if "monitoring_enabled" in provided and body.monitoring_enabled is not None:
        set_monitoring(site, body.monitoring_enabled, now)
    await session.flush()
    await sync_company_summary(session, site.company_id)
    return await _out(session, site)


async def archive_site(session: AsyncSession, site_id: int, *, now: datetime | None = None) -> CareerSiteOut:
    site = await _get(session, site_id)
    if site.archived_at is None:
        site.archived_at = _now(now)
    site.monitoring_enabled = False
    site.next_check_at = None
    await session.flush()
    await sync_company_summary(session, site.company_id)
    return await _out(session, site)


async def resume_site(session: AsyncSession, site_id: int) -> CareerSiteOut:
    """Clear an automatic pause after the user reviewed it. Monitoring is NOT re-enabled automatically."""
    site = await _get(session, site_id)
    site.paused_reason = None
    site.health = HealthStatus.unknown
    site.consecutive_failures = 0
    await session.flush()
    await sync_company_summary(session, site.company_id)
    return await _out(session, site)


# ----------------------------------------------------------------------------------------- dry-run test
def make_fetcher() -> Any:
    """Production fetcher (PoliteClient, owned by the Runtime agent). Imported lazily; tests replace this."""
    from app.discovery.http import PoliteClient

    return PoliteClient()


@asynccontextmanager
async def _borrowed(session: AsyncSession) -> AsyncIterator[AsyncSession]:
    yield session


def borrowed_session_factory(session: AsyncSession) -> Callable[[], Any]:
    """A `session_factory` for run_site that hands out the caller's session (never closes or commits it)."""
    return lambda: _borrowed(session)


async def _close(fetcher: Any) -> None:
    closer = getattr(fetcher, "aclose", None) or getattr(fetcher, "close", None)
    if closer is not None:
        result = closer()
        if inspect.isawaitable(result):
            await result


async def test_site(session: AsyncSession, site_id: int, *, now: datetime | None = None) -> DryRunResult:
    from app.discovery.pipeline import run_site  # local import: pipeline imports this module's helpers

    site = await _get(session, site_id)
    _assert_automatable(site)
    fetcher = make_fetcher()
    try:
        summary = await run_site(
            site_id,
            trigger=RunTrigger.test,
            fetcher=fetcher,
            now=now,
            dry_run=True,
            session_factory=borrowed_session_factory(session),
        )
    finally:
        await _close(fetcher)
    robots = "disallowed" if summary.error_class == "robots_disallowed" else site.robots_status
    samples = [
        DryRunSample(
            title=s["title"],
            location=s.get("location"),
            scope=s["scope"],
            reason=s["reason"],
            filtered=bool(s.get("filtered")),
            url=s["url"],
        )
        for s in summary.samples[:DRY_RUN_SAMPLE_LIMIT]
    ]
    return DryRunResult(
        career_site_id=site_id,
        ok=summary.error_class is None,
        complete=summary.complete,
        postings_seen=summary.postings_seen,
        postings_in_scope=summary.postings_in_scope,
        postings_unknown_scope=summary.postings_unknown_scope,
        postings_out_of_scope=summary.postings_out_of_scope,
        postings_filtered=summary.postings_filtered,
        error_class=summary.error_class,
        error_message=summary.error_message,
        robots_status=robots,
        samples=samples,
    )


# ------------------------------------------------------------------------------------- manual scrape
async def enqueue_run(site_id: int, run_id: int, trigger: str) -> None:
    """Hand a queued run to the worker (`app.worker.enqueue.enqueue_site_run`, written by the Runtime agent)."""
    from app.worker.enqueue import enqueue_site_run

    result = enqueue_site_run(site_id, run_id, trigger)
    if inspect.isawaitable(result):
        await result


STALE_RUN_AFTER = timedelta(minutes=15)


async def active_run(session: AsyncSession, site_id: int, *, now: datetime | None = None) -> ScrapeRun | None:
    """The site's queued/running run, after expiring stale ones (a queued row the worker never picked up,
    or a running row whose worker died) so they can never block manual runs forever."""
    cutoff = _now(now) - STALE_RUN_AFTER
    stale = (
        await session.scalars(
            select(ScrapeRun).where(
                ScrapeRun.career_site_id == site_id,
                ScrapeRun.status.in_([RunStatus.queued, RunStatus.running]),
                func.coalesce(ScrapeRun.started_at, ScrapeRun.queued_at) < cutoff,
            )
        )
    ).all()
    for run in stale:
        run.status = RunStatus.failed
        run.finished_at = _now(now)
        run.error_class = "stale"
        run.error_message = "Run was not picked up / did not finish within 15 minutes"
    if stale:
        await session.flush()
    return await session.scalar(
        select(ScrapeRun)
        .where(ScrapeRun.career_site_id == site_id, ScrapeRun.status.in_([RunStatus.queued, RunStatus.running]))
        .order_by(ScrapeRun.queued_at.desc(), ScrapeRun.id.desc())
        .limit(1)
    )


async def scrape_site(session: AsyncSession, site_id: int, *, now: datetime | None = None) -> ScrapeRunOut:
    """Queue ONE manual run (idempotent while a run is queued/running). 409 if blocked, archived or no adapter."""
    site = await _get(session, site_id)
    _assert_automatable(site)
    if site.archived_at is not None:
        raise Conflict("Career site is archived")
    if site.health == HealthStatus.blocked or site.paused_reason:
        raise Conflict("Career site is blocked/paused; resume it first")
    if not adapter_available(site.platform, site.config):
        raise Conflict(f"No adapter is available for platform '{site.platform.value}'")
    run = await active_run(session, site.id, now=now)
    if run is None:
        run = ScrapeRun(
            company_id=site.company_id,
            career_site_id=site.id,
            trigger=RunTrigger.manual,
            status=RunStatus.queued,
            extractor=site.platform,
            queued_at=_now(now),
        )
        session.add(run)
        await session.flush()
        try:
            await enqueue_run(site.id, run.id, RunTrigger.manual.value)
        except Exception as e:  # never leave an un-enqueued run 'queued' (it would block later manual runs)
            log.warning("enqueue_failed", site_id=site.id, run_id=run.id, error=str(e))
            run.status = RunStatus.failed
            run.finished_at = _now(now)
            run.error_class = "queue"
            run.error_message = f"Could not hand the run to the worker: {e}"[:500]
            await session.flush()
            # The request transaction rolls back on this error, so no un-enqueued row survives.
            raise ServiceUnavailable("The scrape queue is unavailable; try again shortly") from e
    await session.refresh(run)
    return ScrapeRunOut.model_validate(run)


async def site_runs(session: AsyncSession, site_id: int, paging: PageParams) -> Page[ScrapeRunOut]:
    await _get(session, site_id)
    total = (
        await session.scalar(select(func.count()).select_from(ScrapeRun).where(ScrapeRun.career_site_id == site_id))
        or 0
    )
    rows = await session.scalars(
        select(ScrapeRun)
        .where(ScrapeRun.career_site_id == site_id)
        .order_by(ScrapeRun.queued_at.desc(), ScrapeRun.id.desc())
        .limit(paging.page_size)
        .offset(paging.offset)
    )
    return Page[ScrapeRunOut](
        items=[ScrapeRunOut.model_validate(r) for r in rows.all()],
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


# ------------------------------------------------------------------------------------- scheduler
async def scheduler_status(session: AsyncSession, *, now: datetime | None = None) -> SchedulerStatus:
    now = _now(now)
    cfg = await get_setting(session, SchedulerSettings)
    live = CareerSite.archived_at.is_(None)

    async def count(*where: Any) -> int:
        return await session.scalar(select(func.count()).select_from(CareerSite).where(live, *where)) or 0

    queued = (
        await session.scalar(
            select(func.count())
            .select_from(ScrapeRun)
            .where(ScrapeRun.status.in_([RunStatus.queued, RunStatus.running]))
        )
        or 0
    )
    heartbeat = await session.scalar(select(func.max(WorkerHeartbeat.beat_at)))
    return SchedulerStatus(
        enabled=cfg.enabled,
        default_interval_min=cfg.default_interval_min,
        sites_total=await count(),
        sites_monitored=await count(CareerSite.monitoring_enabled.is_(True)),
        sites_due=await count(CareerSite.monitoring_enabled.is_(True), CareerSite.next_check_at <= now),
        sites_blocked=await count(CareerSite.health == HealthStatus.blocked),
        runs_queued_or_running=queued,
        worker_heartbeat_at=heartbeat,
    )
