"""Lead review fixes (integration): stale queued/running runs never block manual scrapes, and a worker that
starts before the API transaction commits does not create a duplicate run."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.discovery import pipeline
from app.models import ScrapeRun
from app.models.enums import RunStatus, RunTrigger
from tests.pipeline.test_sites_api import FAKE, new_company, new_site, queue  # noqa: F401 (fixture)


async def test_stale_queued_run_is_expired_and_does_not_block_manual_scrape(client, session, fake_adapter, queue):
    c = await new_company(client)
    site = await new_site(client, c["id"], FAKE)
    old = ScrapeRun(
        company_id=c["id"],
        career_site_id=site["id"],
        trigger=RunTrigger.manual,
        status=RunStatus.queued,
        queued_at=datetime.now(UTC) - timedelta(minutes=30),
    )
    session.add(old)
    await session.flush()

    r = await client.post(f"/api/v1/sites/{site['id']}/scrape")
    assert r.status_code == 202, r.text
    assert r.json()["id"] != old.id  # a fresh run was queued
    await session.refresh(old)
    assert old.status == RunStatus.failed and old.error_class == "stale"
    assert len(queue) == 1


async def test_recent_queued_run_is_reused(client, session, fake_adapter, queue):
    c = await new_company(client)
    site = await new_site(client, c["id"], FAKE)
    first = (await client.post(f"/api/v1/sites/{site['id']}/scrape")).json()
    second = (await client.post(f"/api/v1/sites/{site['id']}/scrape")).json()
    assert first["id"] == second["id"] and len(queue) == 1


async def test_missing_queued_run_waits_then_creates_one(session, monkeypatch):
    calls = []

    class FakeSession:
        async def get(self, model, ident, populate_existing=False):
            calls.append(ident)
            return None

        async def rollback(self):
            pass

    monkeypatch.setattr(pipeline, "QUEUED_RUN_WAIT_S", 0.6)
    assert await pipeline._await_queued_run(FakeSession(), 123) is None
    assert len(calls) >= 2  # polled again after the first miss instead of giving up immediately
    assert (await session.scalars(select(ScrapeRun))).all() == []


async def test_company_csv_update_with_workday_url_creates_main_site(client):
    c = await new_company(client, name="Nvidia")
    csv_body = "name,careers_url\nNvidia,https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite\n"
    r = await client.post("/api/v1/companies/import", files={"file": ("c.csv", csv_body, "text/csv")})
    assert r.status_code == 200, r.text
    preview = r.json()
    assert preview["rows"][0]["action"] == "update"
    r = await client.post(f"/api/v1/companies/import/{preview['token']}/commit")
    assert r.status_code == 200, r.text
    sites = (await client.get(f"/api/v1/companies/{c['id']}/sites")).json()
    assert [(s["label"], s["platform"]) for s in sites] == [("main", "workday")]
    company = (await client.get(f"/api/v1/companies/{c['id']}")).json()
    assert company["extractor"] == "workday"  # summary derived from the site
    assert sites[0]["monitoring_enabled"] is False


async def test_not_reliable_site_can_never_be_fetched_via_api(client, session, queue):
    """Policy guard (e.g. Palo Alto Networks): robots-disallowed / blocked / manual sites stay bookmarklet-only."""
    from app.models import CareerSite
    from app.models.enums import ExtractorKind, SupportClass
    from tests.factories import make_company

    company = await make_company(session, "Palo Alto Networks")
    site = CareerSite(
        company_id=company.id,
        label="main",
        platform=ExtractorKind.workday,
        config={"kind": "workday", "url": "https://paloaltonetworks.wd5.myworkdayjobs.com/panwexternalcareers"},
        support_class=SupportClass.not_reliable,
        evidence={"support_reason": "robots.txt disallows the Workday site path"},
    )
    session.add(site)
    await session.flush()

    r = await client.patch(f"/api/v1/sites/{site.id}", json={"monitoring_enabled": True})
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_automatable"
    assert "robots.txt disallows" in r.json()["error"]["message"]
    assert (await client.post(f"/api/v1/sites/{site.id}/test")).status_code == 409
    assert (await client.post(f"/api/v1/sites/{site.id}/scrape")).status_code == 409
    assert queue == []
    await session.refresh(site)
    assert site.monitoring_enabled is False


async def test_user_added_supported_site_is_automatable(client, fake_adapter):
    c = await new_company(client, name="Acme")
    gh = await new_site(
        client, c["id"], {"label": "boards", "platform": "greenhouse", "config": {"board_token": "acme"}}
    )
    assert gh["support_class"] == "auto" and gh["verification_status"] == "user_added"
    custom = await new_site(client, c["id"], FAKE)
    assert custom["support_class"] == "adapter"
