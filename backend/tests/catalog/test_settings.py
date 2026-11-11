from app.models.enums import ScopeMatch
from tests.factories import make_job

API = "/api/v1"


async def test_list_get_settings(client):
    r = await client.get(f"{API}/settings")
    assert r.status_code == 200
    by_key = {s["key"]: s["value"] for s in r.json()}
    assert {"location_scope", "timezone", "reminder_defaults", "notifications"} <= by_key.keys()
    assert by_key["location_scope"]["countries"] == ["IN"]
    r = await client.get(f"{API}/settings/timezone")
    assert r.json() == {"key": "timezone", "value": {"name": "Asia/Kolkata"}}
    assert (await client.get(f"{API}/settings/nope")).status_code == 404


async def test_put_setting_and_validation(client):
    r = await client.put(f"{API}/settings/timezone", json={"name": "UTC"})
    assert r.status_code == 200 and r.json()["value"] == {"name": "UTC"}
    assert (await client.get(f"{API}/settings/timezone")).json()["value"] == {"name": "UTC"}
    r = await client.put(f"{API}/settings/reminder_defaults", json={"notify_lead_minutes": -5})
    assert r.status_code == 422
    body = r.json()["error"]
    assert body["code"] == "validation_failed" and body["details"]
    assert (await client.put(f"{API}/settings/nope", json={})).status_code == 404
    r = await client.put(f"{API}/settings/location_scope", json={"countries": ["INDIA"]})
    assert r.status_code == 422


async def test_location_scope_put_reevaluates_jobs(client, session):
    india = await make_job(session, country_codes=["IN"])
    us = await make_job(session, country_codes=["US"])
    unknown = await make_job(session, country_codes=[])
    overridden = await make_job(session, country_codes=["US"], scope_overridden=True)
    assert (india.location_scope, us.location_scope) == (ScopeMatch.in_scope, ScopeMatch.out_of_scope)

    r = await client.put(
        f"{API}/settings/location_scope",
        json={"countries": ["us", "gb"], "include_unknown": True, "include_remote_anywhere": False},
    )
    assert r.status_code == 200 and r.json()["value"]["countries"] == ["GB", "US"]
    for j in (india, us, unknown, overridden):
        await session.refresh(j)
    assert india.location_scope == ScopeMatch.out_of_scope
    assert us.location_scope == ScopeMatch.in_scope
    assert unknown.location_scope == ScopeMatch.unknown
    assert overridden.location_scope == ScopeMatch.out_of_scope  # user override is respected


async def test_saved_views_crud(client):
    V = f"{API}/saved-views"
    a = await client.post(V, json={"page": "jobs", "name": "A", "filters": {"q": "x"}, "is_default": True})
    assert a.status_code == 201, a.text
    a = a.json()
    b = (await client.post(V, json={"page": "jobs", "name": "B", "is_default": True})).json()
    other = (await client.post(V, json={"page": "inbox", "name": "A", "is_default": True})).json()
    assert b["position"] > a["position"]
    views = {v["id"]: v for v in (await client.get(V, params={"page": "jobs"})).json()}
    assert set(views) == {a["id"], b["id"]}
    assert views[a["id"]]["is_default"] is False and views[b["id"]]["is_default"] is True
    all_views = {v["id"]: v for v in (await client.get(V)).json()}
    assert all_views[other["id"]]["is_default"] is True  # other page untouched

    assert (await client.post(V, json={"page": "jobs", "name": "A"})).status_code == 409
    assert (await client.post(V, json={"page": "bogus", "name": "Z"})).status_code == 422

    r = await client.patch(f"{V}/{a['id']}", json={"is_default": True, "position": 7, "name": "A2"})
    assert r.status_code == 200 and r.json()["name"] == "A2" and r.json()["position"] == 7
    views = {v["id"]: v for v in (await client.get(V, params={"page": "jobs"})).json()}
    assert views[b["id"]]["is_default"] is False
    assert (await client.patch(f"{V}/{a['id']}", json={"name": "B"})).status_code == 409
    assert (await client.patch(f"{V}/99999", json={"name": "B"})).status_code == 404

    assert (await client.delete(f"{V}/{a['id']}")).status_code == 204
    assert (await client.delete(f"{V}/{a['id']}")).status_code == 404
    assert [v["id"] for v in (await client.get(V, params={"page": "jobs"})).json()] == [b["id"]]


async def test_resume_versions(client):
    R = f"{API}/resume-versions"
    r = await client.post(R, json={"label": "v1", "file_url": "https://files.example/cv.pdf"})
    assert r.status_code == 201
    v1 = r.json()
    assert (await client.post(R, json={"label": "v1"})).status_code == 409
    v2 = (await client.post(R, json={"label": "v2"})).json()
    r = await client.patch(f"{R}/{v1['id']}", json={"archived": True, "notes": "old"})
    assert r.status_code == 200 and r.json()["archived_at"] and r.json()["notes"] == "old"
    assert [v["id"] for v in (await client.get(R)).json()] == [v2["id"]]
    assert {v["id"] for v in (await client.get(R, params={"include_archived": True})).json()} == {v1["id"], v2["id"]}
    r = await client.patch(f"{R}/{v1['id']}", json={"archived": False, "label": "v1b"})
    assert r.json()["archived_at"] is None and r.json()["label"] == "v1b"
    assert (await client.patch(f"{R}/{v1['id']}", json={"label": "v2"})).status_code == 409
    assert (await client.patch(f"{R}/99999", json={"label": "z"})).status_code == 404


async def test_tags_crud(client):
    T = f"{API}/tags"
    r = await client.post(T, json={"name": "Dream", "color": "red"})
    assert r.status_code == 201
    tag = r.json()
    assert (await client.post(T, json={"name": "dream"})).status_code == 409
    other = (await client.post(T, json={"name": "Backend"})).json()
    assert [t["name"] for t in (await client.get(T)).json()] == ["Backend", "Dream"]
    r = await client.patch(f"{T}/{tag['id']}", json={"name": "Dream co", "color": "blue"})
    assert r.json()["name"] == "Dream co" and r.json()["color"] == "blue"
    assert (await client.patch(f"{T}/{tag['id']}", json={"name": "backend"})).status_code == 409
    assert (await client.patch(f"{T}/99999", json={"name": "z"})).status_code == 404
    assert (await client.delete(f"{T}/{other['id']}")).status_code == 204
    assert (await client.delete(f"{T}/{other['id']}")).status_code == 404


async def test_set_job_tags_replaces_and_creates(client, session):
    job = await make_job(session)
    existing = (await client.post(f"{API}/tags", json={"name": "Remote"})).json()
    url = f"{API}/jobs/{job.id}/tags"
    r = await client.put(url, json=["remote", "New Tag", "new tag", " "])
    assert r.status_code == 200, r.text
    names = [t["name"] for t in r.json()]
    assert names == ["New Tag", "remote"] or names == ["New Tag", "Remote"]
    assert existing["id"] in [t["id"] for t in r.json()]  # reused existing (case-insensitive)
    r = await client.put(url, json=["Other"])
    assert [t["name"] for t in r.json()] == ["Other"]
    assert (await client.put(url, json=[])).json() == []
    assert len((await client.get(f"{API}/tags")).json()) == 3  # tags survive detach
    assert (await client.put(f"{API}/jobs/999999/tags", json=["x"])).status_code == 404
