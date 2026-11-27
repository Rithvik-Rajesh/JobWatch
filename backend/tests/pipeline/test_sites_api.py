"""Sites API + company compatibility (HTTP through the full ASGI stack, real Postgres)."""

import sys
import types
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import CareerSite, ScrapeRun, WorkerHeartbeat
from app.models.enums import HealthStatus
from app.settings.registry import put_setting
from app.sites import service as sites_service
from tests.discovery_support import FixtureFetcher
from tests.factories import make_company
from tests.pipeline.fakes import listing, lp

API = "/api/v1"
FAKE = {"label": "main", "platform": "custom", "config": {"module": "fake"}}
GH = {"label": "boards", "platform": "greenhouse", "config": {"board_token": "acme"}}
WORKDAY = "https://acme.wd5.myworkdayjobs.com/en-US/External"


@pytest.fixture
def queue(monkeypatch):
    """Replace app.worker.enqueue (Runtime agent) with a recorder; works whether or not the real module exists."""
    calls: list[tuple] = []
    mod = types.ModuleType("app.worker.enqueue")

    async def enqueue_site_run(site_id, run_id, trigger="manual"):
        calls.append((site_id, run_id, trigger))

    mod.enqueue_site_run = enqueue_site_run
    monkeypatch.setitem(sys.modules, "app.worker.enqueue", mod)
    return calls


async def new_company(client, name="Acme", **kw):
    r = await client.post(f"{API}/companies", json={"name": name, **kw})
    assert r.status_code == 201, r.text
    return r.json()


async def new_site(client, company_id, body=None):
    r = await client.post(f"{API}/companies/{company_id}/sites", json=body or FAKE)
    assert r.status_code == 201, r.text
    return r.json()


async def test_watchlist_install_and_check(client, session, fake_adapter, monkeypatch):
    from app.sites import watchlist

    monkeypatch.setattr(watchlist, "watchlist", lambda: ["Acme", "Amazon", "Swiggy"])
    company = await new_company(client)
    acme_site = await new_site(client, company["id"])
    fake_adapter.result = listing(lp("JR1"))
    monkeypatch.setattr(sites_service, "make_fetcher", FixtureFetcher)
    endpoint = f"{API}/settings/scrapers"

    download = await client.get(f"{endpoint}/registry.json")
    assert download.status_code == 200
    assert "companies.json" in download.headers["content-disposition"]
    assert {"Amazon", "AMD", "Atlassian", "Grab"} <= {site["company"] for site in download.json()["sites"]}
    assert (await client.get(endpoint)).json()[1]["sites"][0]["status"] == "not_added"
    response = await client.post(f"{endpoint}/install")
    assert response.status_code == 200, response.text
    assert response.json()["sites_created"] == 2
    assert (await client.post(f"{endpoint}/install")).json()["sites_created"] == 0

    rows = (await client.get(endpoint)).json()
    assert rows[1]["sites"][0]["checkable"] is True
    assert rows[2]["sites"][0]["checkable"] is False
    response = await client.post(f"{endpoint}/{acme_site['id']}/check")
    assert response.status_code == 200, response.text
    checked = response.json()
    assert checked["ok"] and checked["sampled"] == 1 and checked["detail_checked"]
    assert (await client.get(endpoint)).json()[0]["sites"][0]["check"] == checked
    assert (await client.get(f"{API}/jobs")).json()["total"] == 0

    site = await session.get(CareerSite, acme_site["id"])
    site.health = HealthStatus.blocked
    site.paused_reason = "Previous request failed"
    await session.flush()
    paused = (await client.get(endpoint)).json()[0]["sites"][0]
    assert paused["status"] == "paused" and paused["check"]["ok"]
    response = await client.post(f"{endpoint}/{site.id}/check?resume=true")
    assert response.status_code == 200, response.text
    assert response.json()["resumed"] is True
    await session.refresh(site)
    assert site.health == HealthStatus.unknown and site.paused_reason is None
    assert not site.monitoring_enabled


# ------------------------------------------------------------------------------------------------ CRUD
async def test_create_get_list_and_validation(client, fake_adapter):
    c = await new_company(client)
    site = await new_site(client, c["id"])
    assert site["company_name"] == "Acme" and site["adapter_available"] is True
    assert site["monitoring_enabled"] is False and site["next_check_at"] is None
    assert site["effective_interval_min"] == 30 and site["config"]["kind"] == "custom"
    gh = await new_site(client, c["id"], GH)
    assert gh["careers_url"] == "https://boards.greenhouse.io/acme" and gh["adapter_available"] is True

    r = await client.post(f"{API}/companies/{c['id']}/sites", json={**GH, "label": "bad", "config": {}})
    assert r.status_code == 422
    r = await client.post(f"{API}/companies/{c['id']}/sites", json={**GH, "config": {"board_token": "a b"}})
    assert r.status_code == 422
    assert (await client.post(f"{API}/companies/{c['id']}/sites", json=FAKE)).status_code == 409  # label taken
    assert (await client.post(f"{API}/companies/999999/sites", json=FAKE)).status_code == 404
    none_site = await new_site(client, c["id"], {"label": "manual", "platform": "none", "config": {}})
    assert none_site["adapter_available"] is False

    assert (await client.get(f"{API}/sites/{site['id']}")).json()["id"] == site["id"]
    assert (await client.get(f"{API}/sites/999999")).status_code == 404
    listed = (await client.get(f"{API}/companies/{c['id']}/sites")).json()
    assert [s["label"] for s in listed] == ["main", "boards", "manual"]
    page = (await client.get(f"{API}/sites", params={"platform": "greenhouse"})).json()
    assert [s["id"] for s in page["items"]] == [gh["id"]] and page["total"] == 1
    page = (await client.get(f"{API}/sites", params={"company_id": c["id"], "page_size": 2})).json()
    assert len(page["items"]) == 2 and page["total"] == 3
    assert (await client.get(f"{API}/sites", params={"monitoring": True})).json()["total"] == 0


async def test_patch_interval_monitoring_and_archive(client, session, fake_adapter):
    c = await new_company(client)
    site = await new_site(client, c["id"])
    r = await client.patch(f"{API}/sites/{site['id']}", json={"check_interval_min": 60, "monitoring_enabled": True})
    body = r.json()
    assert r.status_code == 200 and body["effective_interval_min"] == 60 and body["next_check_at"]
    r = await client.patch(f"{API}/sites/{site['id']}", json={"clear_check_interval": True, "label": "primary"})
    assert r.json()["effective_interval_min"] == 30 and r.json()["label"] == "primary"
    await put_setting(session, "scheduler", {"default_interval_min": 45})
    assert (await client.get(f"{API}/sites/{site['id']}")).json()["effective_interval_min"] == 45
    r = await client.patch(f"{API}/sites/{site['id']}", json={"config": {"module": "x", "bogus": 1}})
    assert r.status_code == 422
    r = await client.patch(f"{API}/sites/{site['id']}", json={"monitoring_enabled": False})
    assert r.json()["next_check_at"] is None
    r = await client.post(f"{API}/sites/{site['id']}/archive")
    assert r.json()["archived_at"] and not r.json()["monitoring_enabled"]
    assert (await client.get(f"{API}/companies/{c['id']}/sites")).json() == []
    r = await client.patch(f"{API}/sites/{site['id']}", json={"monitoring_enabled": True})
    assert r.status_code == 409
    assert (await client.patch(f"{API}/sites/999999", json={"label": "z"})).status_code == 404


async def test_enabling_blocked_site_is_409_until_resume(client, session, fake_adapter):
    c = await new_company(client)
    site = await new_site(client, c["id"])
    row = await session.get(CareerSite, site["id"])
    row.health, row.paused_reason, row.consecutive_failures = HealthStatus.blocked, "robots.txt disallows", 2
    await session.flush()
    r = await client.patch(f"{API}/sites/{site['id']}", json={"monitoring_enabled": True})
    assert r.status_code == 409
    r = await client.post(f"{API}/sites/{site['id']}/resume")
    body = r.json()
    assert r.status_code == 200 and body["health"] == "unknown" and body["paused_reason"] is None
    assert body["monitoring_enabled"] is False  # not auto-enabled
    r = await client.patch(f"{API}/sites/{site['id']}", json={"monitoring_enabled": True})
    assert r.status_code == 200 and r.json()["monitoring_enabled"] is True
    assert (await client.post(f"{API}/sites/999999/resume")).status_code == 404


# ------------------------------------------------------------------------------------------- test / scrape
async def test_dry_run_endpoint_uses_injected_fetcher(client, session, fake_adapter, monkeypatch):
    monkeypatch.setattr(sites_service, "make_fetcher", lambda: FixtureFetcher())
    fake_adapter.result = listing(lp("A", "Engineer"), lp("B", "Engineer", "Austin, TX"), lp("C", location=None))
    c = await new_company(client)
    site = await new_site(client, c["id"])
    r = await client.post(f"{API}/sites/{site['id']}/test")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["postings_seen"] == 3 and body["postings_in_scope"] == 1
    assert (body["postings_unknown_scope"], body["postings_out_of_scope"]) == (1, 1)
    assert {s["scope"] for s in body["samples"]} == {"in_scope", "out_of_scope", "unknown"}
    assert (await session.scalars(select(ScrapeRun))).all() == []
    assert (await client.get(f"{API}/sites/{site['id']}")).json()["last_checked_at"] is None
    assert (await client.post(f"{API}/sites/999999/test")).status_code == 404


async def test_dry_run_endpoint_reports_robots_block(client, fake_adapter, monkeypatch):
    from app.discovery.adapters.base import RobotsDisallowed

    monkeypatch.setattr(sites_service, "make_fetcher", lambda: FixtureFetcher())
    fake_adapter.result = RobotsDisallowed("nope")
    c = await new_company(client)
    site = await new_site(client, c["id"])
    body = (await client.post(f"{API}/sites/{site['id']}/test")).json()
    assert body["ok"] is False and body["error_class"] == "robots_disallowed" and body["robots_status"] == "disallowed"


async def test_scrape_endpoint_idempotent_and_conflicts(client, session, fake_adapter, queue):
    c = await new_company(client)
    site = await new_site(client, c["id"])
    r1 = await client.post(f"{API}/sites/{site['id']}/scrape")
    assert r1.status_code == 202
    run = r1.json()
    assert run["status"] == "queued" and run["trigger"] == "manual" and run["career_site_id"] == site["id"]
    assert queue == [(site["id"], run["id"], "manual")]
    r2 = await client.post(f"{API}/sites/{site['id']}/scrape")
    assert r2.json()["id"] == run["id"] and len(queue) == 1
    stored = await session.get(ScrapeRun, run["id"])
    assert stored.company_id == c["id"] and stored.extractor.value == "custom"

    runs = (await client.get(f"{API}/sites/{site['id']}/runs")).json()
    assert runs["total"] == 1 and runs["items"][0]["id"] == run["id"]
    assert (await client.get(f"{API}/sites/999999/runs")).status_code == 404

    no_adapter = await new_site(client, c["id"], {"label": "manual", "platform": "none", "config": {}})
    assert (await client.post(f"{API}/sites/{no_adapter['id']}/scrape")).status_code == 409
    row = await session.get(CareerSite, site["id"])
    row.health, row.paused_reason = HealthStatus.blocked, "blocked"
    await session.flush()
    assert (await client.post(f"{API}/sites/{site['id']}/scrape")).status_code == 409
    archived = await new_site(client, c["id"], {**GH, "label": "old"})
    await client.post(f"{API}/sites/{archived['id']}/archive")
    assert (await client.post(f"{API}/sites/{archived['id']}/scrape")).status_code == 409
    assert (await client.post(f"{API}/sites/999999/scrape")).status_code == 404


async def test_scrape_enqueue_failure_is_503_and_leaves_no_queued_run(client, session, fake_adapter, monkeypatch):
    mod = types.ModuleType("app.worker.enqueue")

    async def enqueue_site_run(*a, **k):
        raise ConnectionError("queue down")

    mod.enqueue_site_run = enqueue_site_run
    monkeypatch.setitem(sys.modules, "app.worker.enqueue", mod)
    c = await new_company(client)
    site = await new_site(client, c["id"])
    r = await client.post(f"{API}/sites/{site['id']}/scrape")
    assert r.status_code == 503
    assert (await session.scalars(select(ScrapeRun).where(ScrapeRun.career_site_id == site["id"]))).all() == []


async def test_runs_are_paginated_newest_first(client, session, fake_adapter):
    company = await make_company(session)
    site = CareerSite(company_id=company.id, label="main", platform="custom", config={"module": "fake"})
    session.add(site)
    await session.flush()
    now = datetime.now(UTC)
    for i in range(3):
        session.add(
            ScrapeRun(
                company_id=company.id,
                career_site_id=site.id,
                trigger="schedule",
                status="succeeded",
                queued_at=now - timedelta(hours=i),
            )
        )
    await session.flush()
    body = (await client.get(f"{API}/sites/{site.id}/runs", params={"page_size": 2})).json()
    assert body["total"] == 3 and len(body["items"]) == 2
    queued = [i["queued_at"] for i in body["items"]]
    assert queued == sorted(queued, reverse=True)


async def test_scheduler_status(client, session, fake_adapter):
    s0 = (await client.get(f"{API}/scheduler")).json()
    assert s0["enabled"] is False and s0["default_interval_min"] == 30 and s0["worker_heartbeat_at"] is None
    c = await new_company(client)
    a = await new_site(client, c["id"])
    b = await new_site(client, c["id"], {**GH, "monitoring_enabled": True})
    row = await session.get(CareerSite, a["id"])
    row.health = HealthStatus.blocked
    session.add(ScrapeRun(company_id=c["id"], career_site_id=b["id"], trigger="manual", status="queued"))
    beat = datetime.now(UTC) - timedelta(seconds=20)
    session.add(WorkerHeartbeat(name="w1", beat_at=beat - timedelta(minutes=5)))
    session.add(WorkerHeartbeat(name="w2", beat_at=beat))
    await session.flush()
    await put_setting(session, "scheduler", {"enabled": True, "default_interval_min": 20})
    s = (await client.get(f"{API}/scheduler")).json()
    assert (s["enabled"], s["default_interval_min"]) == (True, 20)
    assert (s["sites_total"], s["sites_monitored"], s["sites_blocked"], s["runs_queued_or_running"]) == (
        s0["sites_total"] + 2,
        s0["sites_monitored"] + 1,
        1,
        1,
    )
    assert s["sites_due"] == 1
    assert s["worker_heartbeat_at"].startswith(beat.strftime("%Y-%m-%dT%H:%M"))


# --------------------------------------------------------------------------------- company compatibility
async def test_company_create_with_extractor_config_makes_main_site(client, fake_adapter):
    c = await new_company(
        client, monitoring_enabled=True, extractor_config={"kind": "workday", "url": WORKDAY}, check_interval_min=60
    )
    assert c["extractor"] == "workday" and c["extractor_config"]["kind"] == "workday"
    assert c["next_check_at"] is not None and c["health"] == "unknown"
    (site,) = (await client.get(f"{API}/companies/{c['id']}/sites")).json()
    assert site["label"] == "main" and site["platform"] == "workday" and site["monitoring_enabled"] is True
    assert site["check_interval_min"] == 60 and site["careers_url"].startswith("https://acme.wd5")


async def test_company_summary_is_derived_from_sites(client, session, fake_adapter):
    c = await new_company(client, extractor_config={"kind": "workday", "url": WORKDAY})
    second = await new_site(client, c["id"], GH)
    row = await session.get(CareerSite, second["id"])
    row.health, row.consecutive_failures, row.last_error = HealthStatus.failing, 4, "boom"
    await session.flush()
    await sites_service.sync_company_summary(session, c["id"])
    got = (await client.get(f"{API}/companies/{c['id']}")).json()
    assert got["extractor"] == "workday"  # primary = main
    assert got["health"] == "failing" and got["consecutive_failures"] == 4 and got["last_error"] == "boom"
    listed = (await client.get(f"{API}/companies", params={"health": "failing"})).json()
    assert [i["id"] for i in listed["items"]] == [c["id"]]

    # no `main`: the first site becomes the primary
    await client.post(f"{API}/companies/{c['id']}/archive")
    await client.post(f"{API}/companies/{c['id']}/unarchive")
    plain = await new_company(client, "Plain")
    await new_site(client, plain["id"], {**GH, "label": "careers"})
    assert (await client.get(f"{API}/companies/{plain['id']}")).json()["extractor"] == "greenhouse"


async def test_company_patch_updates_and_archives_main_site(client, fake_adapter):
    c = await new_company(client, "Acme")
    assert c["extractor"] == "none"
    r = await client.patch(
        f"{API}/companies/{c['id']}",
        json={"extractor_config": {"kind": "workday", "url": WORKDAY}, "monitoring_enabled": True},
    )
    assert r.json()["extractor"] == "workday" and r.json()["next_check_at"]
    (site,) = (await client.get(f"{API}/companies/{c['id']}/sites")).json()
    new_url = "https://acme.wd1.myworkdayjobs.com/Other"
    r = await client.patch(f"{API}/companies/{c['id']}", json={"extractor_config": {"kind": "workday", "url": new_url}})
    assert r.json()["extractor_config"]["url"] == new_url
    (same,) = (await client.get(f"{API}/companies/{c['id']}/sites")).json()
    assert same["id"] == site["id"] and same["config"]["url"] == new_url

    r = await client.patch(f"{API}/companies/{c['id']}", json={"clear_extractor": True})
    body = r.json()
    assert body["extractor"] == "none" and body["extractor_config"] == {} and body["next_check_at"] is None
    assert (await client.get(f"{API}/companies/{c['id']}/sites")).json() == []
    # configuring again revives the archived main site instead of colliding on the label
    r = await client.patch(f"{API}/companies/{c['id']}", json={"extractor_config": {"kind": "workday", "url": WORKDAY}})
    assert r.json()["extractor"] == "workday"
    assert [s["id"] for s in (await client.get(f"{API}/companies/{c['id']}/sites")).json()] == [site["id"]]


async def test_company_scrape_delegates_to_main_site(client, session, fake_adapter, queue, monkeypatch):
    monkeypatch.setitem(
        __import__("app.discovery.adapters", fromlist=["x"])._REGISTRY, "workday", "tests.pipeline.fakes:FakeAdapter"
    )
    c = await new_company(client, extractor_config={"kind": "workday", "url": WORKDAY})
    (main,) = (await client.get(f"{API}/companies/{c['id']}/sites")).json()
    await new_site(client, c["id"], {**GH, "label": "zzz"})
    r = await client.post(f"{API}/companies/{c['id']}/scrape")
    assert r.status_code == 202 and r.json()["career_site_id"] == main["id"] and r.json()["status"] == "queued"
    assert (await client.post(f"{API}/companies/{c['id']}/scrape")).json()["id"] == r.json()["id"]
    assert len(queue) == 1
    plain = await new_company(client, "Plain")
    assert (await client.post(f"{API}/companies/{plain['id']}/scrape")).status_code == 409
