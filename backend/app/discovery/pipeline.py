"""Per-site discovery pipeline: list -> India gate -> title filters -> detail -> ingest -> anomaly -> misses -> health.

`run_site` never raises for adapter/HTTP errors: it records the failed run, updates site health (blocked sites are
paused) and returns a RunSummary. It runs in the worker, so it uses its own short-lived sessions
(`session_factory`, default `get_sessionmaker()`); tests inject a factory bound to the test connection.

Counter semantics (scrape_runs / RunSummary):
  postings_seen        every posting the listing returned
  postings_in_scope / postings_unknown_scope / postings_out_of_scope   final India-gate decisions
                       (after detail evidence); they add up to `seen` minus postings with a missing title/url
  postings_filtered    in-scope/unknown postings dropped by the global title filters (not stored)
Out-of-scope and filtered postings are never stored.

Anomaly checks run after detail fetching and BEFORE ingest: a quarantined run stores nothing and records no misses.
"""

import asyncio
import inspect
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_sessionmaker
from app.core.errors import NotFound
from app.core.logging import get_logger
from app.discovery import anomaly
from app.discovery.adapters import get_adapter
from app.discovery.adapters.base import (
    AdapterError,
    ConfigError,
    DetailRecord,
    Fetcher,
    IndiaQuery,
    ListedPosting,
    PortalBlocked,
    RobotsDisallowed,
)
from app.discovery.classifier import FresherClassification, FresherClassifierSettings, JobClassifier
from app.discovery.configs import validate_config
from app.discovery.contracts import LocationInput, PostingInput, RunSummary, ScopeDecision
from app.discovery.india import evaluate_listing
from app.discovery.ingest import ingest_service, mark_seen
from app.discovery.normalize.html import html_to_text
from app.discovery.normalize.location import country_for, country_name, parse_locations
from app.discovery.title_filter import passes
from app.models import CareerSite, Company, Job, Posting, ScrapeRun, Source
from app.models.enums import (
    Availability,
    DatePrecision,
    ExtractorKind,
    HealthStatus,
    RunStatus,
    RunTrigger,
    ScopeMatch,
    WorkArrangement,
)
from app.settings.registry import LocationScopeSettings, SchedulerSettings, TitleFilterSettings, get_setting
from app.sites.service import sync_company_summary

log = get_logger(__name__)

INGEST_BATCH = 20
DRY_RUN_MAX_PAGES = 3
DRY_RUN_MAX_POSTINGS = 60  # 3 pages x 20
FAILURES_BEFORE_FAILING = 3
SessionFactory = Callable[[], Any]  # () -> async context manager yielding an AsyncSession
ProgressCallback = Callable[[dict[str, Any]], None]


def _emit(progress: ProgressCallback | None, stage: str, **details: Any) -> None:
    """Best-effort live progress; UI reporting must never be able to fail a scrape."""
    if progress is not None:
        try:
            progress({"stage": stage, **details})
        except Exception:
            log.warning("progress_callback_failed", stage=stage)


@dataclass
class _Candidate:
    posting: ListedPosting
    decision: ScopeDecision
    filtered: bool = False
    detail: DetailRecord | None = None
    detail_ok: bool = False
    needs_detail: bool = False
    touch_only: bool = False  # unchanged open posting: only mark it seen
    existing_id: int | None = None
    classification: FresherClassification | None = None


@dataclass
class _Outcome:
    """What happened while listing/processing, before anything is persisted."""

    status: RunStatus = RunStatus.succeeded
    complete: bool = False
    error_class: str | None = None
    error_message: str | None = None
    blocked: bool = False
    robots_status: str | None = None
    warnings: list[dict[str, Any]] = field(default_factory=list)


# ------------------------------------------------------------------------------------------- helpers
def _utcnow() -> datetime:
    return datetime.now(UTC)


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def data_host(platform: ExtractorKind, cfg: BaseModel, careers_url: str | None) -> str | None:
    """Host of the data endpoint (robots.txt is checked for it)."""
    match platform:
        case ExtractorKind.workday:
            return cfg.site.host  # type: ignore[attr-defined]
        case ExtractorKind.greenhouse:
            return getattr(cfg, "api_host", None)
        case ExtractorKind.lever:
            return "api.eu.lever.co" if getattr(cfg, "region", "global") == "eu" else "api.lever.co"
        case ExtractorKind.ashby:
            return "api.ashbyhq.com"
        case ExtractorKind.oracle_hcm | ExtractorKind.eightfold:
            return getattr(cfg, "host", None)
    for attr in ("url", "listing_url", "base_url"):
        if getattr(cfg, attr, None):
            return urlparse(str(getattr(cfg, attr))).hostname
    return urlparse(careers_url).hostname if careers_url else None


async def _refresh_robots(fetcher: Any, host: str | None) -> str | None:
    refresh = getattr(fetcher, "force_refresh_robots", None)
    if refresh is None or not host:
        return None
    result = refresh(host)
    if inspect.isawaitable(result):
        result = await result
    return str(result) if isinstance(result, str) else None


def _request_count(fetcher: Any) -> int:
    count = getattr(fetcher, "request_count", None)
    if isinstance(count, int):
        return count
    calls = getattr(fetcher, "calls", None)
    return len(calls) if isinstance(calls, list) else 0


def _classify_error(exc: Exception) -> tuple[str, bool]:
    if isinstance(exc, RobotsDisallowed) and any(
        m in str(exc) for m in ("unavailable", "fetch failed", "returned HTTP 5", "unreachable")
    ):
        return "network", False  # robots.txt temporarily unreachable: fail this run, never pause the site
    if isinstance(exc, AdapterError):
        return exc.error_class, isinstance(exc, PortalBlocked | RobotsDisallowed)
    return "internal", False


def build_posting_input(
    site: "_SiteInfo", posting: ListedPosting, detail: DetailRecord | None, decision: ScopeDecision
) -> PostingInput:
    d = detail
    texts = [t for t in ((d.location_texts if d and d.location_texts else None) or posting.location_texts) if t]
    codes: list[str] = []
    for code in [*posting.country_codes, *((d.country_codes or []) if d else [])]:
        if code and len(code) == 2 and code.upper() not in codes:
            codes.append(code.upper())
    eligibility = [*posting.remote_eligibility, *((d.remote_eligibility or []) if d else [])]

    locations: list[LocationInput] = []
    for text in texts:
        locations.extend(parse_locations(text))
    have = {loc.country_code for loc in locations if loc.country_code}
    for code in codes:
        if code not in have:
            locations.append(LocationInput(raw=", ".join(texts) or code, country_code=code))
            have.add(code)
    for item in eligibility:
        cc = country_for(item)
        if cc and cc not in have:
            locations.append(LocationInput(raw=f"Remote - {item}", country_code=cc))
            have.add(cc)

    remote = bool(posting.remote or (d and d.remote))
    work_arrangement = (d.work_arrangement if d else None) or (WorkArrangement.remote if remote else None)
    posted_at = (d.posted_at if d and d.posted_at else None) or posting.posted_at
    if d and d.posted_at:
        precision = d.posted_at_precision or posting.posted_at_precision
    else:
        precision = posting.posted_at_precision
    return PostingInput(
        source_key="career_site",
        company_id=site.company_id,
        career_site_id=site.id,
        external_id=posting.external_id,
        requisition_id=(d.requisition_id if d and d.requisition_id else None) or posting.requisition_id,
        source_url=posting.url,  # type: ignore[arg-type]
        apply_url=(d.apply_url if d else None) or None,  # type: ignore[arg-type]
        title=posting.title,
        department=(d.department if d and d.department else None) or posting.department,
        location_text="; ".join(texts) or None,
        locations=locations or None,
        employment_type=(d.employment_type if d and d.employment_type else None) or posting.employment_type,
        work_arrangement=work_arrangement,
        posted_at=posted_at,
        posted_at_precision=precision,
        posted_at_raw=posting.posted_at_raw,
        deadline_at=d.deadline_at if d else None,
        salary_text=d.salary_text if d else None,
        description_html=d.description_html if d else None,
        description_text=d.description_text if d else None,
        listing_fingerprint=posting.fingerprint,
        # Adapter payloads can be very large and are not needed after normalization.
        raw_payload=None,
        scope=decision.scope,
        scope_reason=decision.reason,
    )


@dataclass
class _SiteInfo:
    id: int
    company_id: int
    company_name: str
    label: str
    platform: ExtractorKind
    config: dict[str, Any]
    careers_url: str | None
    baseline: int | None
    monitoring_enabled: bool
    interval_min: int


def _sample(c: _Candidate) -> dict[str, Any]:
    p = c.posting
    return {
        "title": p.title,
        "location": "; ".join(p.location_texts) or None,
        "scope": c.decision.scope.value,
        "reason": c.decision.reason,
        "filtered": c.filtered,
        "url": p.url,
    }


# ----------------------------------------------------------------------------------------- main entry
QUEUED_RUN_WAIT_S = 10.0


async def _await_queued_run(s: AsyncSession, scrape_run_id: int) -> ScrapeRun | None:
    """A manual run is enqueued inside the API request's transaction, so the worker can start before that
    transaction commits. Wait briefly for the queued row instead of creating a duplicate run."""
    deadline = time.monotonic() + QUEUED_RUN_WAIT_S
    while True:
        run = await s.get(ScrapeRun, scrape_run_id, populate_existing=True)
        if run is not None or time.monotonic() >= deadline:
            return run
        await s.rollback()  # end the snapshot so the next read can see the newly committed row
        await asyncio.sleep(0.5)


async def run_site(
    site_id: int,
    *,
    trigger: RunTrigger | str,
    fetcher: Fetcher,
    now: datetime | None = None,
    dry_run: bool = False,
    scrape_run_id: int | None = None,
    session_factory: SessionFactory | None = None,
    progress: ProgressCallback | None = None,
    classify_new: bool = True,
) -> RunSummary:
    trigger = RunTrigger(trigger)
    now = now or _utcnow()
    sf: SessionFactory = session_factory or get_sessionmaker()
    started = time.monotonic()
    requests_before = _request_count(fetcher)

    # -- phase A: load site + settings, create/mark the run ---------------------------------------------
    async with sf() as s:
        site_row = await s.get(CareerSite, site_id)
        if site_row is None:
            raise NotFound("Career site not found")
        company = await s.get(Company, site_row.company_id)
        assert company is not None
        loc_cfg = await get_setting(s, LocationScopeSettings)
        title_cfg = await get_setting(s, TitleFilterSettings)
        fresher_cfg = await get_setting(s, FresherClassifierSettings)
        sched = await get_setting(s, SchedulerSettings)
        site = _SiteInfo(
            id=site_row.id,
            company_id=site_row.company_id,
            company_name=company.name,
            label=site_row.label,
            platform=site_row.platform,
            config=dict(site_row.config),
            careers_url=site_row.careers_url,
            baseline=site_row.baseline_jobs_count,
            monitoring_enabled=site_row.monitoring_enabled,
            interval_min=site_row.check_interval_min or sched.default_interval_min,
        )
        run_id: int | None = None
        if not dry_run:
            run = await _await_queued_run(s, scrape_run_id) if scrape_run_id else None
            if run is None:
                run = ScrapeRun(company_id=site.company_id, career_site_id=site.id, trigger=trigger, queued_at=now)
                s.add(run)
            run.status = RunStatus.running
            run.started_at = now
            run.extractor = site.platform
            await s.flush()
            run_id = run.id
            await s.commit()

    summary = RunSummary(career_site_id=site.id, scrape_run_id=run_id, status=RunStatus.succeeded.value, complete=False)
    outcome = _Outcome()
    candidates: list[_Candidate] = []
    seen_ids: set[str] = set()
    ingest_errors: list[dict[str, Any]] = []
    version = "unknown"
    cfg_before: dict[str, Any] | None = None
    cfg_after: dict[str, Any] | None = None
    listed_count = 0
    missing_fields = 0

    try:
        # -- phase B: validate config, adapter, robots, list -------------------------------------------------
        try:
            cfg = validate_config(site.platform.value, site.config)
        except (ValidationError, KeyError) as e:
            raise ConfigError(f"Invalid {site.platform.value} configuration: {e}") from e
        try:
            adapter = get_adapter(
                site.platform, getattr(cfg, "module", None) if site.platform == ExtractorKind.custom else None
            )
        except Exception as e:
            raise ConfigError(f"No adapter available for '{site.platform.value}': {e}") from e
        version = getattr(adapter, "version", "unknown")
        cfg_before = cfg.model_dump(mode="json")

        robots = await _refresh_robots(fetcher, data_host(site.platform, cfg, site.careers_url))
        outcome.robots_status = robots
        if robots == "disallowed":
            raise RobotsDisallowed("robots.txt disallows this portal for our User-Agent")
        if robots == "unavailable":
            raise RobotsDisallowed("robots.txt unavailable (temporary); skipping this run")

        list_cfg = cfg
        if dry_run and hasattr(cfg, "max_pages"):
            list_cfg = cfg.model_copy(update={"max_pages": min(cfg.max_pages, DRY_RUN_MAX_PAGES)})
        india = IndiaQuery(
            country_codes=tuple(loc_cfg.countries), country_names=tuple(country_name(c) for c in loc_cfg.countries)
        )
        result = await adapter.list(list_cfg, india, fetcher)
        cfg_after = list_cfg.model_dump(mode="json") if not dry_run else None
        outcome.complete = result.complete
        outcome.warnings.extend(result.warnings)
        postings = list(result.postings)
        if dry_run and len(postings) > DRY_RUN_MAX_POSTINGS:
            postings, outcome.complete = postings[:DRY_RUN_MAX_POSTINGS], False
        summary.postings_seen = listed_count = len(postings)
        _emit(progress, "listed", count=listed_count, site_id=site.id, company=site.company_name)

        # -- India gate + title filters (listing evidence) ---------------------------------------------------
        by_id: dict[str, ListedPosting] = {}
        for p in postings:
            if not (p.title or "").strip() or not (p.url or "").strip() or not p.external_id:
                missing_fields += 1
                continue
            by_id.setdefault(p.external_id, p)
        for p in by_id.values():
            decision = evaluate_listing(p, None, loc_cfg)
            if decision.scope == ScopeMatch.out_of_scope:
                candidates.append(_Candidate(p, decision))
                continue
            candidates.append(_Candidate(p, decision, filtered=not passes(p.title, title_cfg)))

        if dry_run:
            for c in candidates:
                _tally(summary, c)
            summary.samples = [_sample(c) for c in candidates]
            summary.complete = outcome.complete
            summary.diagnostics = await _dry_run_diagnostics(
                adapter, list_cfg, fetcher, result, postings, candidates, loc_cfg, requests_before
            )
            return summary

        # -- phase C/D: decide and fetch details --------------------------------------------------------------
        keep = [c for c in candidates if c.decision.scope != ScopeMatch.out_of_scope and not c.filtered]
        async with sf() as s:
            existing = await _existing_postings(s, site.company_id, [c.posting.external_id for c in keep])
        for c in keep:
            row = existing.get(c.posting.external_id)
            c.existing_id = row.id if row else None
            # Career-site jobs are immutable inbox entries: fetch and classify only unseen listings.
            # Existing rows are merely touched below, preventing repeated JD downloads and versions.
            c.needs_detail = row is None
        deferred = 0
        for c in keep:
            c.touch_only = c.existing_id is not None and not c.needs_detail
            if not c.needs_detail:
                continue
            _emit(progress, "fetching_jd", external_id=c.posting.external_id, title=c.posting.title)
            if not c.posting.has_full_detail and summary.details_fetched >= sched.max_details_per_run:
                # Store the listing even when the politeness cap prevents a JD request.
                deferred += 1
                continue
            try:
                c.detail = await adapter.detail(cfg, c.posting, fetcher)
                c.detail_ok = True
                if not c.posting.has_full_detail:
                    summary.details_fetched += 1
            except (PortalBlocked, RobotsDisallowed):
                raise
            except Exception as e:
                outcome.warnings.append(
                    {"code": "detail_failed", "external_id": c.posting.external_id, "detail": str(e)[:200]}
                )
            if c.detail is not None:  # re-evaluate scope with detail evidence
                c.decision = evaluate_listing(c.posting, c.detail, loc_cfg)

        classifier = JobClassifier(classifier_settings=fresher_cfg)
        for c in keep:
            if c.existing_id is not None:
                continue
            description = None
            if c.detail is not None:
                description = c.detail.description_text or html_to_text(c.detail.description_html)
            if classify_new:
                _emit(progress, "classifying", external_id=c.posting.external_id, title=c.posting.title)
                c.classification = await classifier.classify(c.posting.title, description)
                _emit(
                    progress,
                    "classified",
                    external_id=c.posting.external_id,
                    title=c.posting.title,
                    verdict=c.classification.is_fresher,
                    provider=c.classification.provider,
                    reason=c.classification.reason,
                )

        if deferred:
            outcome.warnings.append({"code": "details_deferred", "count": deferred, "cap": sched.max_details_per_run})
        for c in candidates:
            _tally(summary, c)

        # -- anomaly checks (before anything is stored) -------------------------------------------------------
        async with sf() as s:
            history = await anomaly.compute_baseline(s, site.id, exclude_run_id=run_id)
        baseline = history if history is not None else site.baseline
        verdict = anomaly.check_run(
            in_scope_count=summary.postings_in_scope,
            listed=listed_count,
            missing_fields=missing_fields,
            baseline=baseline,
        )
        if verdict.quarantined:
            outcome.status = RunStatus.quarantined
            outcome.error_class = "anomaly"
            outcome.error_message = f"Quarantined: {verdict.reason}"
        elif not outcome.complete:
            outcome.status = RunStatus.partial
            outcome.error_class = "incomplete"
            outcome.error_message = "Listing was not exhaustive (a page failed or a cap was hit)"

        # -- phase E: ingest in batches of 20 (savepoint per posting) -------------------------------------------
        eligible = [c for c in candidates if c.decision.scope != ScopeMatch.out_of_scope and not c.filtered]
        seen_ids = {c.posting.external_id for c in eligible}
        if outcome.status != RunStatus.quarantined:
            await _ingest_all(sf, site, eligible, summary, ingest_errors, run_id, now, progress)
        outcome.warnings.extend(ingest_errors)
    except (PortalBlocked, RobotsDisallowed, AdapterError) as e:
        outcome.status = RunStatus.failed
        outcome.error_class, outcome.blocked = _classify_error(e)
        outcome.error_message = str(e)[:500]
        if isinstance(e, RobotsDisallowed):
            outcome.robots_status = "disallowed"
    except Exception as e:
        log.exception("run_site_internal_error", site_id=site_id)
        outcome.status = RunStatus.failed
        outcome.error_class = "internal"
        outcome.error_message = f"{type(e).__name__}: {e}"[:500]

    if dry_run:
        summary.status = RunStatus.failed.value if outcome.error_class else RunStatus.succeeded.value
        summary.complete = False
        summary.error_class, summary.error_message = outcome.error_class, outcome.error_message
        return summary

    # -- phase F: misses, run record, site health ----------------------------------------------------------
    listing_ok = outcome.status != RunStatus.failed
    async with sf() as s:
        await _finalize(
            s,
            site=site,
            run_id=run_id,
            outcome=outcome,
            summary=summary,
            seen_ids=seen_ids,
            listing_ok=listing_ok,
            now=now,
            version=version,
            duration_ms=int((time.monotonic() - started) * 1000),
            http_requests=max(0, _request_count(fetcher) - requests_before),
            new_config=cfg_after if cfg_after is not None and cfg_after != cfg_before else None,
        )
        await s.commit()
    summary.status = outcome.status.value
    summary.complete = outcome.complete and listing_ok
    summary.error_class, summary.error_message = outcome.error_class, outcome.error_message
    return summary


ABSOLUTE_DATES = (DatePrecision.exact, DatePrecision.day)


def _pct(n: int, d: int) -> float | None:
    return round(100.0 * n / d, 1) if d else None


async def _dry_run_diagnostics(
    adapter: Any,
    cfg: BaseModel,
    fetcher: Fetcher,
    result: Any,
    postings: list[ListedPosting],
    candidates: list["_Candidate"],
    loc_cfg: LocationScopeSettings,
    requests_before: int,
) -> dict[str, Any]:
    """Read-only quality signals for the Phase 3.5 coverage report. Probes ONE detail (the first India-eligible,
    unfiltered posting) so description availability can be reported; nothing is written."""
    n = len(postings)
    completeness = {
        "location": _pct(sum(1 for p in postings if p.location_texts or p.country_codes), n),
        "structured_country": _pct(sum(1 for p in postings if p.country_codes), n),
        "posted_at": _pct(sum(1 for p in postings if p.posted_at), n),
        "posted_at_absolute": _pct(
            sum(
                1 for p in postings if p.posted_at and p.posted_at_precision in (DatePrecision.exact, DatePrecision.day)
            ),
            n,
        ),
        "requisition_id": _pct(sum(1 for p in postings if p.requisition_id), n),
        "department": _pct(sum(1 for p in postings if p.department), n),
        "employment_type": _pct(sum(1 for p in postings if p.employment_type), n),
        "full_detail_in_listing": _pct(sum(1 for p in postings if p.has_full_detail), n),
    }
    probe: dict[str, Any] = {"attempted": False}
    target = next((c for c in candidates if c.decision.scope == ScopeMatch.in_scope and not c.filtered), None) or next(
        (c for c in candidates if c.decision.scope == ScopeMatch.unknown), None
    )
    if target is not None:
        probe = {"attempted": True, "external_id": target.posting.external_id, "title": target.posting.title}
        try:
            d = await adapter.detail(cfg, target.posting, fetcher)
            text = (d.description_text or "") + (d.description_html or "")
            after = evaluate_listing(target.posting, d, loc_cfg)
            probe.update(
                ok=True,
                description_chars=len(text),
                description_available=len(text.strip()) >= 200,
                apply_url=bool(d.apply_url),
                posted_at=d.posted_at.isoformat() if d.posted_at else None,
                posted_at_precision=(d.posted_at_precision.value if d.posted_at_precision else None),
                employment_type=d.employment_type.value if d.employment_type else None,
                work_arrangement=d.work_arrangement.value if d.work_arrangement else None,
                salary_text=bool(d.salary_text),
                detail_locations=(d.location_texts or [])[:5],
                detail_country_codes=d.country_codes or [],
                scope_before=target.decision.scope.value,
                scope_after_detail=after.scope.value,
                reason_after_detail=after.reason,
            )
        except (PortalBlocked, RobotsDisallowed) as e:
            probe.update(ok=False, error_class=type(e).__name__, error=str(e)[:300])
        except Exception as e:  # report, never fail the dry run on the probe
            probe.update(ok=False, error_class=type(e).__name__, error=str(e)[:300])
    return {
        "adapter_complete": result.complete,
        "pages_fetched": result.pages_fetched,
        "source_total": result.source_total,
        "used_source_india_filter": result.used_source_india_filter,
        "adapter_warnings": result.warnings[:20],
        "field_completeness_pct": completeness,
        "detail_probe": probe,
        "http_requests": max(0, _request_count(fetcher) - requests_before),
    }


def _tally(summary: RunSummary, c: _Candidate) -> None:
    match c.decision.scope:
        case ScopeMatch.in_scope:
            summary.postings_in_scope += 1
        case ScopeMatch.unknown:
            summary.postings_unknown_scope += 1
        case ScopeMatch.out_of_scope:
            summary.postings_out_of_scope += 1
    if c.filtered and c.decision.scope != ScopeMatch.out_of_scope:
        summary.postings_filtered += 1


async def _existing_postings(session: AsyncSession, company_id: int, external_ids: list[str]) -> dict[str, Posting]:
    out: dict[str, Posting] = {}
    if not external_ids:
        return out
    source_id = await session.scalar(select(Source.id).where(Source.key == "career_site"))
    for i in range(0, len(external_ids), 1000):
        rows = await session.scalars(
            select(Posting).where(
                Posting.source_id == source_id,
                Posting.company_id == company_id,
                Posting.external_id.in_(external_ids[i : i + 1000]),
            )
        )
        out.update({p.external_id: p for p in rows.all()})
    return out


async def _ingest_all(
    sf: SessionFactory,
    site: _SiteInfo,
    store: list[_Candidate],
    summary: RunSummary,
    errors: list[dict[str, Any]],
    run_id: int | None,
    now: datetime,
    progress: ProgressCallback | None = None,
) -> None:
    for i in range(0, len(store), INGEST_BATCH):
        batch = store[i : i + INGEST_BATCH]
        async with sf() as s:
            touch: list[int] = []
            for c in batch:
                if c.touch_only and c.existing_id is not None:
                    touch.append(c.existing_id)  # unchanged: never overwrite stored content
                    continue
                try:
                    data = build_posting_input(site, c.posting, c.detail, c.decision)
                    async with s.begin_nested():
                        outcome = await ingest_service.ingest(s, data, seen_at=now, scrape_run_id=run_id)
                        if c.classification is not None:
                            job = await s.get(Job, outcome.job_id)
                            assert job is not None
                            if job.classified_at is None:
                                job.is_fresher_role = c.classification.is_fresher
                                job.fresher_reason = c.classification.reason
                                job.classified_by = c.classification.provider
                                job.classified_at = now
                        if c.detail_ok:
                            posting = await s.get(Posting, outcome.posting_id)
                            assert posting is not None
                            posting.detail_fetched_at = now
                except Exception as e:
                    errors.append(
                        {"code": "ingest_failed", "external_id": c.posting.external_id, "detail": str(e)[:200]}
                    )
                    log.warning("ingest_failed", site_id=site.id, external_id=c.posting.external_id, error=str(e))
                    continue
                if outcome.created_posting:
                    summary.postings_new += 1
                    _emit(
                        progress,
                        "stored",
                        job_id=outcome.job_id,
                        external_id=c.posting.external_id,
                        title=c.posting.title,
                        company=site.company_name,
                        location="; ".join(c.posting.location_texts) or None,
                        description=(
                            (c.detail.description_text or html_to_text(c.detail.description_html))
                            if c.detail is not None
                            else None
                        ),
                        verdict=c.classification.is_fresher if c.classification else None,
                    )
                elif outcome.new_version:
                    summary.postings_updated += 1
                if outcome.reopened:
                    summary.postings_reopened += 1
            await mark_seen(s, touch, now=now, career_site_id=site.id)
            await s.commit()


async def _finalize(
    s: AsyncSession,
    *,
    site: _SiteInfo,
    run_id: int | None,
    outcome: _Outcome,
    summary: RunSummary,
    seen_ids: set[str],
    listing_ok: bool,
    now: datetime,
    version: str,
    duration_ms: int,
    http_requests: int,
    new_config: dict[str, Any] | None,
) -> None:
    run = await s.get(ScrapeRun, run_id)
    row = await s.get(CareerSite, site.id)
    assert run is not None and row is not None

    if listing_ok:
        misses = await ingest_service.apply_misses(
            s,
            source_key="career_site",
            company_id=site.company_id,
            seen_external_ids=seen_ids,
            career_site_id=site.id,
            run_complete=outcome.complete,
            run_trusted=outcome.status == RunStatus.succeeded,
            now=now,
        )
        summary.postings_closed = len(misses.closed_posting_ids)

    run.status = outcome.status
    run.complete = outcome.complete and listing_ok
    run.finished_at = now
    run.duration_ms = duration_ms
    run.extractor, run.extractor_version = site.platform, version
    run.postings_seen = summary.postings_seen
    run.postings_in_scope = summary.postings_in_scope
    run.postings_out_of_scope = summary.postings_out_of_scope
    run.postings_unknown_scope = summary.postings_unknown_scope
    run.postings_filtered = summary.postings_filtered
    run.postings_new = summary.postings_new
    run.postings_updated = summary.postings_updated
    run.postings_closed = summary.postings_closed
    run.postings_reopened = summary.postings_reopened
    run.details_fetched = summary.details_fetched
    run.http_requests = http_requests
    run.error_class, run.error_message = outcome.error_class, outcome.error_message
    run.warnings = _json_safe(outcome.warnings)
    await s.flush()

    # -- site health -----------------------------------------------------------------------------------
    row.last_checked_at = now
    if outcome.robots_status in ("allowed", "disallowed", "unavailable"):
        row.robots_status, row.robots_checked_at = outcome.robots_status, now
    elif listing_ok and not outcome.blocked:
        row.robots_status, row.robots_checked_at = "allowed", now
    if new_config is not None and listing_ok:
        row.config = new_config  # adapters cache discovered ids (facets, location ids) on the config
    if outcome.status == RunStatus.succeeded:
        row.health, row.consecutive_failures = HealthStatus.healthy, 0
        row.last_success_at, row.last_error = now, None
        history = await anomaly.compute_baseline(s, site.id)
        if history is not None:
            row.baseline_jobs_count = history
    else:
        row.consecutive_failures += 1
        row.last_error = outcome.error_message
        row.health = (
            HealthStatus.failing if row.consecutive_failures >= FAILURES_BEFORE_FAILING else HealthStatus.degraded
        )
    if outcome.blocked:
        row.health = HealthStatus.blocked
        row.monitoring_enabled = False
        row.next_check_at = None
        row.paused_reason = outcome.error_message or outcome.error_class
    elif row.monitoring_enabled:
        row.next_check_at = now + timedelta(minutes=site.interval_min)
    row.open_jobs_count = (
        await s.scalar(
            select(func.count(func.distinct(Posting.job_id))).where(
                Posting.career_site_id == site.id, Posting.availability == Availability.open
            )
        )
        or 0
    )
    await s.flush()
    await sync_company_summary(s, site.company_id)


__all__ = ["data_host", "run_site"]
