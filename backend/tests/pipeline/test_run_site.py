"""run_site integration tests (real Postgres, fake adapter, no network)."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.discovery.adapters.base import PortalBlocked, RobotsDisallowed, TransientError
from app.discovery.pipeline import run_site
from app.models import CareerSite, Company, Job, JobVersion, Posting, ScrapeRun
from app.models.enums import Availability, HealthStatus, RunStatus, ScopeMatch
from app.settings.registry import put_setting
from tests.discovery_support import FixtureFetcher
from tests.factories import make_company
from tests.pipeline.fakes import T0, listing, lp, make_site

H = timedelta(hours=1)


@pytest.fixture
async def env(session, session_factory, fake_adapter):
    company = await make_company(session, "Acme")
    site = await make_site(session, company, monitoring=True)

    class Env:
        pass

    e = Env()
    e.company, e.site, e.adapter = company, site, fake_adapter

    async def run(result=None, *, at=T0, site_obj=None, fetcher=None, **kw):
        target = site_obj or site
        if result is not None:
            fake_adapter.result = result
        summary = await run_site(
            target.id,
            trigger="manual",
            fetcher=fetcher or FixtureFetcher(),
            now=at,
            session_factory=session_factory,
            **kw,
        )
        await session.refresh(target)
        return summary

    e.run = run
    return e


async def count(session, model, *where):
    return await session.scalar(select(func.count()).select_from(model).where(*where))


async def postings(session, site):
    rows = await session.scalars(select(Posting).where(Posting.career_site_id == site.id).order_by(Posting.id))
    return {p.external_id: p for p in rows}


async def reload(session, *objs):
    for o in objs:
        await session.refresh(o)


# ------------------------------------------------------------------------------------------ scope / storage
async def test_out_of_scope_not_stored_unknown_stored_and_hidden(env, session, client):
    s = await env.run(
        listing(
            lp("IN1", "Backend Engineer", "Pune, India"),
            lp("US1", "Backend Engineer", "Austin, TX"),
            lp("IND1", "Backend Engineer", "Indianapolis, IN"),
            lp("UNK1", "Platform Engineer", "2 Locations"),
            lp("REM1", "SRE", "Remote"),
        )
    )
    assert s.status == "succeeded" and s.complete
    assert (s.postings_seen, s.postings_in_scope, s.postings_unknown_scope, s.postings_out_of_scope) == (5, 1, 2, 2)
    assert s.postings_new == 3
    stored = await postings(session, env.site)
    assert set(stored) == {"IN1", "UNK1", "REM1"}
    jobs = {j.id: j for j in (await session.scalars(select(Job).where(Job.company_id == env.company.id))).all()}
    by_ext = {ext: jobs[p.job_id] for ext, p in stored.items()}
    assert by_ext["IN1"].location_scope == ScopeMatch.in_scope and by_ext["IN1"].scope_reason == "city: Pune"
    assert by_ext["UNK1"].location_scope == ScopeMatch.unknown and by_ext["UNK1"].scope_reason == "no location"

    r = await client.get("/api/v1/jobs", params={"company_id": env.company.id})
    assert [i["title"] for i in r.json()["items"]] == ["Backend Engineer"]
    r = await client.get("/api/v1/jobs", params={"scope": "unknown", "company_id": env.company.id})
    assert {i["title"] for i in r.json()["items"]} == {"Platform Engineer", "SRE"}


async def test_title_filters_count_but_do_not_store(env, session):
    await put_setting(session, "title_filters", {"include": ["engineer"], "exclude": ["sales"]})
    s = await env.run(
        listing(
            lp("1", "Software Engineer"),
            lp("2", "Sales Engineer"),
            lp("3", "Office Manager"),
            lp("4", "DevOps Engineer"),
        )
    )
    assert s.postings_filtered == 2 and s.postings_new == 2
    assert set(await postings(session, env.site)) == {"1", "4"}


async def test_detail_persisted_and_scope_reevaluated_with_detail(env, session, monkeypatch):
    from app.discovery.adapters.base import DetailRecord

    async def detail(self, config, posting, http):
        return DetailRecord(description_text="Great job", location_texts=["Chennai, India"])

    monkeypatch.setattr(env.adapter, "detail", detail)
    s = await env.run(listing(lp("A", "Engineer", "2 Locations")))
    assert (s.postings_in_scope, s.postings_unknown_scope) == (1, 0)
    (p,) = (await postings(session, env.site)).values()
    assert p.detail_fetched_at == T0
    job = await session.get(Job, p.job_id)
    assert job.location_scope == ScopeMatch.in_scope and job.scope_reason == "city: Chennai"


# ------------------------------------------------------------------------------------------ details / idempotency
async def test_detail_is_fetched_only_for_new_jobs(env, session):
    a, b = lp("A", "Engineer"), lp("B", "Developer")
    s1 = await env.run(listing(a, b))
    assert s1.details_fetched == 2 and sorted(env.adapter.detail_calls) == ["A", "B"]

    env.adapter.detail_calls.clear()
    s2 = await env.run(listing(a, b), at=T0 + H)
    assert s2.details_fetched == 0 and env.adapter.detail_calls == []

    changed = lp("A", "Engineer", fingerprint="fp-A-changed")
    s3 = await env.run(listing(changed, b), at=T0 + 2 * H)
    assert env.adapter.detail_calls == [] and s3.details_fetched == 0

    env.adapter.detail_calls.clear()
    s4 = await env.run(listing(changed, b), at=T0 + timedelta(days=8))
    assert env.adapter.detail_calls == [] and s4.details_fetched == 0


async def test_has_full_detail_still_calls_detail_but_not_counted(env, session):
    s = await env.run(listing(lp("A", has_full_detail=True)))
    assert env.adapter.detail_calls == ["A"] and s.details_fetched == 0 and s.postings_new == 1
    (p,) = (await postings(session, env.site)).values()
    assert p.apply_url == "https://careers.example.com/jobs/A/apply"


async def test_detail_failure_still_stores_new_job_without_retrying_existing(env, session):
    await env.run(listing(lp("A", "Engineer")))
    (p,) = (await postings(session, env.site)).values()
    versions = await count(session, JobVersion, JobVersion.posting_id == p.id)
    env.adapter.detail_error = TransientError("boom")
    s = await env.run(listing(lp("A", "Engineer", fingerprint="new"), lp("B", "Developer")), at=T0 + H)
    assert s.status == "succeeded" and s.details_fetched == 0
    await reload(session, p)
    assert await count(session, JobVersion, JobVersion.posting_id == p.id) == versions  # content untouched
    assert p.listing_fingerprint != "new" and p.consecutive_misses == 0 and p.last_seen_at == T0 + H
    assert "B" in await postings(session, env.site)  # every new listing is retained
    env.adapter.detail_error = None
    env.adapter.detail_calls.clear()
    await env.run(at=T0 + 2 * H)
    assert env.adapter.detail_calls == []  # both listings now exist, so neither is checked again
    assert "B" in await postings(session, env.site)


async def test_idempotent_rerun(env, session):
    result = listing(lp("A"), lp("B", "Data Engineer", "Hyderabad"), lp("C", "Analyst", "2 Locations"))
    s1 = await env.run(result)
    models = (Job, Posting, JobVersion)
    before = [await count(session, m) for m in models]
    s2 = await env.run(result, at=T0 + H)
    after = [await count(session, m) for m in models]
    assert before == after == [3, 3, 3]
    assert (s2.postings_new, s2.postings_updated, s2.postings_closed) == (0, 0, 0)
    assert s1.postings_new == 3


# ------------------------------------------------------------------------------------------ closure rules
async def _open(session, site, ext):
    return (await postings(session, site))[ext].availability == Availability.open


async def test_failed_blocked_partial_quarantined_runs_never_close_jobs(env, session):
    await env.run(listing(lp("A"), lp("B")))
    steps = [
        TransientError("network down"),
        PortalBlocked("HTTP 403"),
        listing(lp("B"), complete=False),  # partial
        listing(),  # 0 postings with baseline 2 -> quarantined
    ]
    statuses = []
    for i, result in enumerate(steps, start=1):
        s = await env.run(result, at=T0 + timedelta(hours=10 * i))
        statuses.append((s.status, s.error_class))
    assert statuses == [
        ("failed", "network"),
        ("failed", "blocked"),
        ("partial", "incomplete"),
        ("quarantined", "anomaly"),
    ]
    stored = await postings(session, env.site)
    assert all(p.availability == Availability.open and p.consecutive_misses == 0 for p in stored.values())


async def test_closure_after_two_complete_runs_six_hours_apart(env, session):
    await env.run(listing(lp("A"), lp("B")))
    only_b = listing(lp("B"))
    await env.run(only_b, at=T0 + H)  # miss 1
    await env.run(only_b, at=T0 + 2 * H)  # miss 2 but only 1h span
    assert await _open(session, env.site, "A")
    s = await env.run(only_b, at=T0 + 7 * H + timedelta(minutes=1))  # span >= 6h
    assert not await _open(session, env.site, "A") and await _open(session, env.site, "B")
    assert s.postings_closed == 1
    job = await session.get(Job, (await postings(session, env.site))["A"].job_id)
    assert job.availability == Availability.closed
    await reload(session, env.site)
    assert env.site.open_jobs_count == 1


async def test_misses_are_per_portal(env, session):
    site_b = await make_site(session, env.company, "careers-b", monitoring=True)
    await env.run(listing(lp("A1")))
    await env.run(listing(lp("B1", "Data Engineer")), site_obj=site_b)
    # portal A now lists a different posting; A1 goes missing on portal A only
    for hours in (1, 2, 8):
        await env.run(listing(lp("A2", "Other Engineer")), at=T0 + hours * H)
    a, b = await postings(session, env.site), await postings(session, site_b)
    assert a["A1"].availability == Availability.closed and a["A2"].availability == Availability.open
    assert b["B1"].availability == Availability.open and b["B1"].consecutive_misses == 0
    assert b["B1"].career_site_id == site_b.id


# ------------------------------------------------------------------------------------------ robots / health
async def test_robots_disallowed_mid_run_auto_pauses_site(env, session):
    env.adapter.detail_error = RobotsDisallowed("robots.txt disallows /jobs/1")
    s = await env.run(listing(lp("A")))
    assert (s.status, s.error_class) == ("failed", "robots_disallowed")
    assert await count(session, Posting) == 0
    assert env.site.health == HealthStatus.blocked and env.site.monitoring_enabled is False
    assert env.site.next_check_at is None and "robots" in env.site.paused_reason
    assert env.site.robots_status == "disallowed"
    await session.refresh(env.company)
    assert env.company.health == HealthStatus.blocked


async def test_robots_precheck_blocks_before_listing(env, session):
    class RobotsFetcher(FixtureFetcher):
        def force_refresh_robots(self, host):
            self.refreshed = host
            return "disallowed"

    fetcher = RobotsFetcher()
    s = await env.run(listing(lp("A")), fetcher=fetcher)
    assert s.error_class == "robots_disallowed" and env.adapter.list_calls == []
    assert env.site.health == HealthStatus.blocked and fetcher.refreshed == "careers.example.com"


async def test_portal_blocked_pauses_site_and_health_transitions(env, session):
    await env.run(listing(lp("A")))
    assert env.site.health == HealthStatus.healthy and env.site.last_success_at == T0
    states = []
    for i in range(1, 4):
        await env.run(TransientError("flaky"), at=T0 + i * H)
        states.append((env.site.health, env.site.consecutive_failures))
    assert states == [(HealthStatus.degraded, 1), (HealthStatus.degraded, 2), (HealthStatus.failing, 3)]
    assert env.site.last_error == "flaky"
    await env.run(listing(lp("A")), at=T0 + 5 * H)
    assert (env.site.health, env.site.consecutive_failures, env.site.last_error) == (HealthStatus.healthy, 0, None)
    assert env.site.next_check_at == T0 + 5 * H + timedelta(minutes=30)
    await env.run(PortalBlocked("HTTP 403 challenge"), at=T0 + 6 * H)
    assert env.site.health == HealthStatus.blocked and not env.site.monitoring_enabled
    assert env.site.paused_reason == "HTTP 403 challenge"


async def test_missing_adapter_is_config_error(env, session):
    env.site.config = {"kind": "custom", "module": "does_not_exist", "options": {}}
    await session.flush()
    s = await env.run()
    assert (s.status, s.error_class) == ("failed", "config")
    assert env.site.health == HealthStatus.degraded


async def test_scrape_run_row_is_reused_and_counters_filled(env, session):
    run = ScrapeRun(company_id=env.company.id, career_site_id=env.site.id, trigger="manual", status="queued")
    session.add(run)
    await session.flush()
    s = await env.run(listing(lp("A"), lp("B", "Engineer", "Austin, TX")), scrape_run_id=run.id)
    assert s.scrape_run_id == run.id
    await session.refresh(run)
    assert run.status == RunStatus.succeeded and run.complete and run.started_at == T0
    assert (run.postings_seen, run.postings_in_scope, run.postings_out_of_scope, run.postings_new) == (2, 1, 1, 1)
    assert run.extractor_version == "fake-1" and run.finished_at == T0
    assert await count(session, ScrapeRun, ScrapeRun.career_site_id == env.site.id) == 1


async def test_adapter_config_cache_is_persisted(env, session):
    env.adapter.cache_value = {"facet": "abc"}
    await env.run(listing(lp("A")))
    assert env.site.config["options"]["cached"] == {"facet": "abc"}


async def test_quarantine_stores_nothing_and_baseline_updates(env, session):
    await env.run(listing(*[lp(f"P{i}") for i in range(30)]))
    await session.refresh(env.site)
    assert env.site.baseline_jobs_count == 30
    s = await env.run(listing(*[lp(f"P{i}") for i in range(5)]), at=T0 + H)
    assert s.status == "quarantined" and s.postings_new == 0
    assert env.site.health == HealthStatus.degraded and "Quarantined" in env.site.last_error
    assert await count(session, Posting, Posting.consecutive_misses > 0) == 0


# ------------------------------------------------------------------------------------------ dry run
async def test_dry_run_writes_nothing(env, session):
    await put_setting(session, "title_filters", {"exclude": ["sales"]})
    runs, jobs, postings_, versions = [await count(session, m) for m in (ScrapeRun, Job, Posting, JobVersion)]
    s = await env.run(
        listing(lp("A", "Engineer"), lp("B", "Sales Rep"), lp("C", "Engineer", "Austin, TX"), lp("D", location=None)),
        dry_run=True,
    )
    assert s.scrape_run_id is None and s.status == "succeeded"
    assert (s.postings_seen, s.postings_in_scope, s.postings_unknown_scope) == (4, 2, 1)
    assert (s.postings_out_of_scope, s.postings_filtered) == (1, 1)
    assert (await count(session, ScrapeRun), await count(session, Job), await count(session, Posting)) == (
        runs,
        jobs,
        postings_,
    )
    assert await count(session, JobVersion) == versions
    assert len(env.adapter.detail_calls) <= 1  # one read-only detail probe for the coverage report
    assert env.site.last_checked_at is None and env.site.health == HealthStatus.unknown
    by_title = {x["title"]: x for x in s.samples}
    assert by_title["Sales Rep"]["filtered"] and by_title["Engineer"]["url"].startswith("https://")
    assert {x["scope"] for x in s.samples} == {"in_scope", "unknown", "out_of_scope"}


async def test_dry_run_reports_errors_without_writing(env, session):
    s = await env.run(PortalBlocked("HTTP 403"), dry_run=True)
    assert (s.status, s.error_class) == ("failed", "blocked")
    assert env.site.health == HealthStatus.unknown and env.site.monitoring_enabled is True


async def test_company_summary_follows_sites(env, session):
    await env.run(listing(lp("A")))
    await session.refresh(env.company)
    assert env.company.health == HealthStatus.healthy and env.company.extractor.value == "custom"
    assert env.company.last_success_at == T0 and isinstance(env.company, Company)
    assert await count(session, CareerSite, CareerSite.company_id == env.company.id) == 1


async def test_detail_requests_are_capped_per_run_and_resumed_next_run(env, session):
    """Lead addition: politeness cap on detail requests; the remainder is fetched on later runs."""
    from app.settings.registry import put_setting

    await put_setting(session, "scheduler", {"max_details_per_run": 2})
    posts = [lp(f"P{i}", f"Engineer {i}") for i in range(5)]
    s1 = await env.run(listing(*posts))
    assert s1.details_fetched == 2 and s1.postings_new == 5
    stored = await postings(session, env.site)
    assert len(stored) == 5 and sum(p.detail_fetched_at is not None for p in stored.values()) == 2

    env.adapter.detail_calls.clear()
    s2 = await env.run(listing(*posts), at=T0 + H)
    assert s2.details_fetched == 0 and s2.postings_new == 0
    assert env.adapter.detail_calls == []
    assert len(await postings(session, env.site)) == 5


async def test_progress_reports_only_new_job_classification(env):
    events = []
    await env.run(listing(lp("A", "Software Engineer")), progress=events.append)
    assert [e["stage"] for e in events] == ["listed", "fetching_jd", "classifying", "classified", "stored"]

    events.clear()
    await env.run(listing(lp("A", "Software Engineer")), at=T0 + H, progress=events.append)
    assert [e["stage"] for e in events] == ["listed"]


async def test_dry_run_is_read_only_everywhere_and_reports_diagnostics(env, session):
    """Phase 3.5 safety: a dry run touches no table at all (jobs, postings, applications, events, runs, outbox,
    the site row), yet returns field completeness and a single detail probe."""
    from sqlalchemy import func

    from app.models import (
        Application,
        ApplicationEvent,
        CareerSite,
        Job,
        JobVersion,
        NotificationOutbox,
        Posting,
        ScrapeRun,
    )

    tables = (Job, Posting, JobVersion, Application, ApplicationEvent, ScrapeRun, NotificationOutbox)

    async def counts():
        return [await session.scalar(select(func.count()).select_from(t)) for t in tables]

    before = await counts()
    keys = ("config", "health", "monitoring_enabled", "next_check_at", "robots_status", "last_checked_at")
    row = await session.get(CareerSite, env.site.id, populate_existing=True)
    site_before = {k: getattr(row, k) for k in keys}
    s = await env.run(listing(lp("A", "Engineer"), lp("B", "Analyst", "Austin, TX")), dry_run=True)
    assert await counts() == before
    site_after = await session.get(CareerSite, env.site.id, populate_existing=True)
    assert {k: getattr(site_after, k) for k in keys} == site_before
    assert s.scrape_run_id is None and (s.postings_in_scope, s.postings_out_of_scope) == (1, 1)
    d = s.diagnostics
    assert d["detail_probe"]["attempted"] and d["detail_probe"]["ok"] and d["detail_probe"]["external_id"] == "A"
    assert set(d["field_completeness_pct"]) >= {"location", "posted_at", "requisition_id", "structured_country"}
    assert env.adapter.detail_calls == ["A"]
