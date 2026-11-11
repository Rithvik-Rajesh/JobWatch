from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import Company, ScrapeRun
from app.sites import service as sites_service
from tests.factories import application_for, make_company, make_job, status_id

P = "/api/v1/companies"


@pytest.fixture(autouse=True)
def _no_real_queue(monkeypatch):
    """Phase 3: manual scrapes hand the run to the worker queue; these tests must not touch a real queue."""

    async def _enqueue(site_id, run_id, trigger):
        return None

    monkeypatch.setattr(sites_service, "enqueue_run", _enqueue)


WORKDAY = "https://acme.wd5.myworkdayjobs.com/en-US/External"


async def _create(client, name="Acme Corp", **kw):
    r = await client.post(P, json={"name": name, **kw})
    assert r.status_code == 201, r.text
    return r.json()


async def test_create_derives_slug_and_dedupes(client):
    a = await _create(client, "Acme & Sons!")
    b = await _create(client, "ACME  sons")  # different name casing/spacing, same slug base
    c = await _create(client, "Acme sons?")
    assert a["slug"] == "acme-sons"
    assert b["slug"] == "acme-sons-2"
    assert c["slug"] == "acme-sons-3"
    assert a["extractor"] == "none" and a["next_check_at"] is None


async def test_create_with_workday_config_and_aliases(client):
    body = {
        "name": "Acme",
        "monitoring_enabled": True,
        "extractor_config": {"kind": "workday", "url": WORKDAY},
        "aliases": ["Acme Inc", "acme inc", " ACME Global "],
    }
    c = await _create(client, **body)
    assert c["extractor"] == "workday"
    assert c["extractor_config"]["kind"] == "workday"
    assert c["careers_url"].startswith("https://acme.wd5.myworkdayjobs.com")
    assert c["next_check_at"] is not None
    assert sorted(c["aliases"]) == ["ACME Global", "Acme Inc"]


async def test_create_conflicts_and_validation(client):
    await _create(client, "Acme", aliases=["Acme Inc"])
    assert (await client.post(P, json={"name": "acme"})).status_code == 409
    assert (await client.post(P, json={"name": "Other", "aliases": ["ACME INC"]})).status_code == 409
    assert (await client.post(P, json={"name": "Acme Inc"})).status_code == 409
    r = await client.post(P, json={"name": "X", "extractor_config": {"kind": "custom", "module": "x"}})
    assert r.status_code == 422  # extractor without a careers url
    assert (await client.post(P, json={"name": ""})).status_code == 422


async def test_get_and_not_found(client):
    c = await _create(client, "Acme", aliases=["A"])
    r = await client.get(f"{P}/{c['id']}")
    assert r.status_code == 200 and r.json()["aliases"] == ["A"]
    assert (await client.get(f"{P}/999999")).status_code == 404


async def test_patch_only_provided_fields_and_aliases(client):
    c = await _create(client, "Acme", description="d", aliases=["One", "Two"])
    r = await client.patch(f"{P}/{c['id']}", json={"notes": "n", "aliases": ["Three"]})
    assert r.status_code == 200
    body = r.json()
    assert body["notes"] == "n" and body["description"] == "d" and body["aliases"] == ["Three"]

    r = await client.patch(
        f"{P}/{c['id']}",
        json={"extractor_config": {"kind": "workday", "url": WORKDAY}, "monitoring_enabled": True},
    )
    body = r.json()
    assert body["extractor"] == "workday" and body["next_check_at"] is not None and body["careers_url"]
    r = await client.patch(f"{P}/{c['id']}", json={"clear_extractor": True})
    body = r.json()
    assert body["extractor"] == "none" and body["extractor_config"] == {} and body["next_check_at"] is None

    other = await _create(client, "Other")
    assert (await client.patch(f"{P}/{other['id']}", json={"name": "acme"})).status_code == 409
    assert (await client.patch(f"{P}/{other['id']}", json={"aliases": ["Three"]})).status_code == 409
    assert (await client.patch(f"{P}/999999", json={"notes": "x"})).status_code == 404


async def test_archive_and_unarchive(client):
    c = await _create(client, "Acme", monitoring_enabled=True, extractor_config={"kind": "workday", "url": WORKDAY})
    r = await client.post(f"{P}/{c['id']}/archive")
    body = r.json()
    assert r.status_code == 200 and body["archived_at"] and not body["monitoring_enabled"]
    assert body["next_check_at"] is None
    ids = [i["id"] for i in (await client.get(P)).json()["items"]]
    assert c["id"] not in ids
    ids = [i["id"] for i in (await client.get(P, params={"include_archived": True})).json()["items"]]
    assert c["id"] in ids
    r = await client.post(f"{P}/{c['id']}/unarchive")
    assert r.json()["archived_at"] is None
    assert (await client.post(f"{P}/999999/archive")).status_code == 404


async def test_list_filters_sort_pagination_and_tracked_count(client, session):
    now = datetime.now(UTC)
    a = await make_company(session, "Alpha", open_jobs_count=1, health="failing", last_success_at=now)
    b = await make_company(session, "Beta", open_jobs_count=9, health="healthy", monitoring_enabled=True)
    c = await make_company(
        session, "Gamma", open_jobs_count=5, health="degraded", last_success_at=now - timedelta(days=1)
    )
    job1 = await make_job(session, a)
    await make_job(session, a)  # stays in inbox -> not tracked
    app = await application_for(session, job1)
    app.status_id = await status_id(session, "applied")
    merged = await make_job(session, a)
    (await application_for(session, merged)).status_id = await status_id(session, "applied")
    merged.merged_into_job_id = job1.id
    await session.flush()

    def names(r):
        return [i["name"] for i in r.json()["items"]]

    r = await client.get(P, params={"sort": "name_desc"})
    assert names(r) == ["Gamma", "Beta", "Alpha"]
    assert r.json()["total"] == 3
    r = await client.get(P)
    items = {i["name"]: i for i in r.json()["items"]}
    assert names(r) == ["Alpha", "Beta", "Gamma"]
    assert items["Alpha"]["tracked_jobs_count"] == 1 and items["Beta"]["tracked_jobs_count"] == 0
    assert names(await client.get(P, params={"sort": "open_jobs_desc"})) == ["Beta", "Gamma", "Alpha"]
    assert names(await client.get(P, params={"sort": "last_success_desc"})) == ["Alpha", "Gamma", "Beta"]
    assert names(await client.get(P, params={"sort": "health"})) == ["Alpha", "Gamma", "Beta"]
    assert names(await client.get(P, params={"q": "amm"})) == ["Gamma"]
    assert names(await client.get(P, params={"monitoring": True})) == ["Beta"]
    assert names(await client.get(P, params={"health": ["failing", "degraded"], "sort": "name_desc"})) == [
        "Gamma",
        "Alpha",
    ]
    r = await client.get(P, params={"page": 2, "page_size": 2})
    assert names(r) == ["Gamma"] and r.json()["total"] == 3
    assert (await client.get(P, params={"sort": "bogus"})).status_code == 422
    del b, c


async def test_manual_scrape_is_idempotent(client, session):
    c = await _create(client, "Acme", extractor_config={"kind": "workday", "url": WORKDAY})
    r1 = await client.post(f"{P}/{c['id']}/scrape")
    assert r1.status_code == 202
    run = r1.json()
    assert run["status"] == "queued" and run["trigger"] == "manual"
    r2 = await client.post(f"{P}/{c['id']}/scrape")
    assert r2.json()["id"] == run["id"]
    count = len((await session.scalars(select(ScrapeRun).where(ScrapeRun.company_id == c["id"]))).all())
    assert count == 1
    stored = await session.scalar(select(ScrapeRun).where(ScrapeRun.id == run["id"]))
    assert stored.extractor.value == "workday"


async def test_scrape_conflicts(client):
    plain = await _create(client, "Plain")
    assert (await client.post(f"{P}/{plain['id']}/scrape")).status_code == 409
    c = await _create(client, "Acme", extractor_config={"kind": "workday", "url": WORKDAY})
    await client.post(f"{P}/{c['id']}/archive")
    assert (await client.post(f"{P}/{c['id']}/scrape")).status_code == 409
    assert (await client.post(f"{P}/999999/scrape")).status_code == 404


async def test_runs_paginated_newest_first(client, session):
    company = await make_company(session)
    now = datetime.now(UTC)
    for i in range(3):
        session.add(
            ScrapeRun(company_id=company.id, trigger="schedule", status="succeeded", queued_at=now - timedelta(hours=i))
        )
    await session.flush()
    r = await client.get(f"{P}/{company.id}/runs", params={"page_size": 2})
    body = r.json()
    assert body["total"] == 3 and len(body["items"]) == 2
    queued = [i["queued_at"] for i in body["items"]]
    assert queued == sorted(queued, reverse=True)
    assert (await client.get(f"{P}/999999/runs")).status_code == 404


async def test_history_per_day_with_zero_days(client, session):
    company = await make_company(session)
    now = datetime.now(UTC)
    await make_job(session, company, first_seen_at=now)
    await make_job(session, company, first_seen_at=now)
    await make_job(session, company, first_seen_at=now - timedelta(days=3), closed_at=now, availability="closed")
    r = await client.get(f"{P}/{company.id}/history", params={"days": 5})
    assert r.status_code == 200
    pts = r.json()
    assert len(pts) == 5
    dates = [p["date"] for p in pts]
    assert dates == sorted(dates)
    assert sum(p["discovered"] for p in pts) == 3
    assert sum(p["closed"] for p in pts) == 1
    assert pts[-1]["discovered"] == 2 and pts[-1]["closed"] == 1
    assert sum(1 for p in pts if p["discovered"] == 0) == 3
    assert (await client.get(f"{P}/999999/history")).status_code == 404
    assert (await client.get(f"{P}/{company.id}/history", params={"days": 0})).status_code == 422


# ------------------------------------------------------------------ CSV import


def _csv(text: str, bom=False) -> dict:
    data = text.encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    return {"file": ("companies.csv", data, "text/csv")}


async def test_csv_import_mixed_file(client, session):
    existing = await _create(client, "Globex", aliases=["Globex Corp"], careers_url="https://globex.example/jobs")
    same = await _create(client, "Initech", website_url="https://initech.example")
    text = (
        "Name,careers_url,website_url,monitoring_enabled,aliases,ignored\n"
        f"Acme,{WORKDAY},,yes,Acme Inc;ACME Global,x\n"  # create + workday
        "globex corp,https://globex.example/careers,,,,x\n"  # update via alias
        "Initech,,https://initech.example,,,x\n"  # skip (identical)
        ",https://nameless.example,,,,x\n"  # error: name missing
        "Hooli,not a url,,,,x\n"  # error: bad url
        "Pied Piper,,,maybe,,x\n"  # error: bad bool
        "acme inc,,,,,x\n"  # error: duplicate of Acme alias in file
        "Umbrella,,https://umbrella.example,0,,x\n"  # create
    )
    r = await client.post(f"{P}/import", files=_csv(text, bom=True))
    assert r.status_code == 200, r.text
    prev = r.json()
    assert prev["counts"] == {"create": 2, "update": 1, "skip": 1, "error": 4}
    rows = {row["row_number"]: row for row in prev["rows"]}
    assert rows[2]["action"] == "create" and rows[2]["detected_extractor"] == "workday"
    assert rows[3]["action"] == "update" and rows[3]["existing_company_id"] == existing["id"]
    assert rows[4]["action"] == "skip" and rows[4]["existing_company_id"] == same["id"]
    assert all(rows[n]["action"] == "error" and rows[n]["message"] for n in (5, 6, 7, 8))
    # preview writes nothing
    assert await session.scalar(select(Company.id).where(Company.name == "Acme")) is None

    r = await client.post(f"{P}/import/{prev['token']}/commit")
    assert r.status_code == 200, r.text
    res = r.json()
    assert (res["created"], res["updated"], res["skipped"], res["errors"]) == (2, 1, 1, 4)
    acme = (await client.get(f"{P}", params={"q": "Acme"})).json()["items"][0]
    detail = (await client.get(f"{P}/{acme['id']}")).json()
    assert detail["extractor"] == "workday" and detail["monitoring_enabled"] and detail["next_check_at"]
    assert sorted(detail["aliases"]) == ["ACME Global", "Acme Inc"]
    g = (await client.get(f"{P}/{existing['id']}")).json()
    assert g["careers_url"] == "https://globex.example/careers"
    um = (await client.get(f"{P}", params={"q": "Umbrella"})).json()["items"][0]
    assert um["extractor"] == "none" and not um["monitoring_enabled"]

    # a token can only be committed once
    assert (await client.post(f"{P}/import/{prev['token']}/commit")).status_code == 409
    assert (await client.post(f"{P}/import/unknown-token/commit")).status_code == 404


async def test_csv_import_expired_token_and_bad_files(client, monkeypatch):
    from app.companies import csv_import

    r = await client.post(f"{P}/import", files=_csv("name\nZed\n"))
    token = r.json()["token"]
    csv_import._PREVIEWS[token].created_at -= timedelta(minutes=31)
    assert (await client.post(f"{P}/import/{token}/commit")).status_code == 404

    assert (await client.post(f"{P}/import", files=_csv("careers_url\nhttps://x.example\n"))).status_code == 422
    assert (await client.post(f"{P}/import", files=_csv(""))).status_code == 422
    big = "name\n" + "\n".join(f"Co {i}" for i in range(2001)) + "\n"
    assert (await client.post(f"{P}/import", files=_csv(big))).status_code == 422
    bad = {"file": ("c.csv", b"name\n\xff\xfe\n", "text/csv")}
    assert (await client.post(f"{P}/import", files=bad)).status_code == 422
