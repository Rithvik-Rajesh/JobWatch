from sqlalchemy import select, update

from app.models import Source
from tests.factories import application_for, make_job

S = "/api/v1/statuses"
SRC = "/api/v1/sources"


async def test_list_and_create_status(client):
    r = await client.get(S)
    assert r.status_code == 200
    items = r.json()
    orders = [s["sort_order"] for s in items]
    assert orders == sorted(orders) and items[0]["key"] == "new"
    r = await client.post(S, json={"key": "phone_screen", "label": "Phone Screen", "category": "in_process"})
    assert r.status_code == 201
    created = r.json()
    assert created["is_system"] is False and created["sort_order"] > max(orders)
    assert (await client.post(S, json={"key": "phone_screen", "label": "x", "category": "offer"})).status_code == 409
    assert (await client.post(S, json={"key": "Bad Key", "label": "x", "category": "offer"})).status_code == 422


async def test_update_status_rules(client):
    items = (await client.get(S)).json()
    system = next(s for s in items if s["is_system"])
    r = await client.patch(f"{S}/{system['id']}", json={"label": "Renamed", "color": "blue"})
    assert r.status_code == 200 and r.json()["label"] == "Renamed"
    same = await client.patch(f"{S}/{system['id']}", json={"category": system["category"]})
    assert same.status_code == 200
    other = "offer" if system["category"] != "offer" else "applied"
    assert (await client.patch(f"{S}/{system['id']}", json={"category": other})).status_code == 409
    custom = (await client.post(S, json={"key": "custom_one", "label": "C", "category": "considering"})).json()
    r = await client.patch(f"{S}/{custom['id']}", json={"category": "applied"})
    assert r.status_code == 200 and r.json()["category"] == "applied"
    assert (await client.patch(f"{S}/99999", json={"label": "x"})).status_code == 404


async def test_reorder_statuses(client):
    ids = [s["id"] for s in (await client.get(S)).json()]
    rev = list(reversed(ids))
    r = await client.post(f"{S}/reorder", json={"ordered_ids": rev})
    assert r.status_code == 200
    assert [s["id"] for s in r.json()] == rev
    assert [s["id"] for s in (await client.get(S)).json()] == rev
    assert (await client.post(f"{S}/reorder", json={"ordered_ids": ids[:-1]})).status_code == 422
    assert (await client.post(f"{S}/reorder", json={"ordered_ids": [*ids, ids[0]]})).status_code == 422
    assert (await client.post(f"{S}/reorder", json={"ordered_ids": [*ids[:-1], 99999]})).status_code == 422


async def test_archive_status(client, session):
    system = next(s for s in (await client.get(S)).json() if s["is_system"])
    assert (await client.post(f"{S}/{system['id']}/archive")).status_code == 409

    used = (await client.post(S, json={"key": "used_one", "label": "U", "category": "considering"})).json()
    job = await make_job(session)
    app = await application_for(session, job)
    app.status_id = used["id"]
    await session.flush()
    assert (await client.post(f"{S}/{used['id']}/archive")).status_code == 409

    free = (await client.post(S, json={"key": "free_one", "label": "F", "category": "considering"})).json()
    assert (await client.post(f"{S}/{free['id']}/archive")).status_code == 200
    assert free["id"] not in [s["id"] for s in (await client.get(S)).json()]
    assert free["id"] in [s["id"] for s in (await client.get(S, params={"include_archived": True})).json()]
    assert (await client.post(f"{S}/99999/archive")).status_code == 404


async def test_sources_list_with_counts(client, session):
    job = await make_job(session, source_key="manual")
    merged = await make_job(session, source_key="manual")
    merged.merged_into_job_id = job.id
    await session.flush()
    r = await client.get(SRC)
    assert r.status_code == 200
    by_key = {s["key"]: s for s in r.json()}
    assert by_key["manual"]["jobs_count"] == 1
    assert by_key["career_site"]["jobs_count"] == 0 and by_key["career_site"]["last_run_at"] is None


async def test_sources_last_run_at(client, session):
    from app.models import ScrapeRun
    from tests.factories import source_id

    sid = await source_id(session, "adzuna")
    session.add(ScrapeRun(source_id=sid, trigger="schedule", status="succeeded"))
    await session.flush()
    by_key = {s["key"]: s for s in (await client.get(SRC)).json()}
    assert by_key["adzuna"]["last_run_at"] is not None


async def test_update_source(client, session):
    by_key = {s["key"]: s for s in (await client.get(SRC)).json()}
    adz = by_key["adzuna"]
    r = await client.patch(f"{SRC}/{adz['id']}", json={"enabled": True, "config": {"country": "in"}})
    assert r.status_code == 200
    assert r.json()["enabled"] is True and r.json()["config"] == {"country": "in"}
    assert (await client.patch(f"{SRC}/{adz['id']}", json={"enabled": False})).status_code == 200
    for key in ("career_site", "manual", "capture"):
        assert (await client.patch(f"{SRC}/{by_key[key]['id']}", json={"enabled": False})).status_code == 409
    assert (await client.patch(f"{SRC}/99999", json={"enabled": True})).status_code == 404
    await session.execute(update(Source).where(Source.key == "manual").values(enabled=True))
    assert (await session.scalar(select(Source.enabled).where(Source.key == "manual"))) is True
