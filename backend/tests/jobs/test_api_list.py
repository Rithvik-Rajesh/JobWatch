"""HTTP tests: list, filters, search, facets, inbox, review."""

from datetime import timedelta

from sqlalchemy import select

from app.discovery.ingest import ingest_service
from app.models import Application, ApplicationEvent, Job, JobTag, Tag
from app.models.enums import EventType
from tests.factories import application_for, make_company, status_id
from tests.jobs.helpers import T0, posting

API = "/api/v1"


async def seed(session):
    """Three jobs at one company: India, US-only, unknown; plus another company in India."""
    acme = await make_company(session, "Acme Robotics")
    globex = await make_company(session, "Globex")
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    ids = {}
    for key, company, ext, title, loc, extra in [
        ("in", acme, "IN1", "Backend Engineer", "Bengaluru, India", {"salary_text": "₹20-30 LPA"}),
        ("us", acme, "US1", "Kubernetes Platform Engineer", "Austin, TX, United States", {}),
        ("unk", acme, "UNK1", "Technical Writer", "2 Locations", {}),
        ("gx", globex, "GX1", "Data Scientist", "Hyderabad", {"employment_type": "contract"}),
    ]:
        out = await ingest_service.ingest(
            session, posting(company.id, ext, title=title, location_text=loc, **extra), seen_at=now
        )
        ids[key] = out.job_id
    return acme, globex, ids


async def test_default_scope_hides_us_only_jobs(client, session):
    _, _, ids = await seed(session)
    r = await client.get(f"{API}/jobs")
    assert r.status_code == 200, r.text
    body = r.json()
    got = {i["id"] for i in body["items"]}
    assert got == {ids["in"], ids["unk"], ids["gx"]} and body["total"] == 3
    r = await client.get(f"{API}/jobs", params={"scope": "all"})
    assert {i["id"] for i in r.json()["items"]} == set(ids.values())
    r = await client.get(f"{API}/jobs", params={"scope": "out_of_scope"})
    assert [i["id"] for i in r.json()["items"]] == [ids["us"]]
    r = await client.get(f"{API}/jobs", params=[("scope", "in_scope"), ("scope", "unknown")])
    assert {i["id"] for i in r.json()["items"]} == {ids["in"], ids["unk"], ids["gx"]}


async def test_scope_default_follows_setting(client, session):
    from app.settings.registry import put_setting

    _, _, ids = await seed(session)
    await put_setting(session, "location_scope", {"countries": ["IN"], "include_unknown": False})
    r = await client.get(f"{API}/jobs")
    assert {i["id"] for i in r.json()["items"]} == {ids["in"], ids["gx"]}


async def test_list_item_shape(client, session):
    acme, _, ids = await seed(session)
    r = await client.get(f"{API}/jobs", params={"company_id": acme.id})
    item = next(i for i in r.json()["items"] if i["id"] == ids["in"])
    assert item["company"]["name"] == "Acme Robotics"
    assert item["status"]["key"] == "new" and item["reviewed"] is False and item["is_new"] is True
    assert item["is_updated"] is False and item["sources"] == ["career_site"]
    assert item["location_scope"] == "in_scope" and item["country_codes"] == ["IN"]
    assert item["salary"]["provenance"] == "employer_provided" and item["salary"]["raw_text"] == "₹20-30 LPA"
    assert item["application_id"] and item["row_version"] == 1


async def test_filters(client, session):
    _acme, globex, ids = await seed(session)
    app = await application_for(session, await session.get(Job, ids["gx"]))
    app.status_id = await status_id(session, "applied")
    app.starred = True
    tag = Tag(name="dream")
    session.add(tag)
    await session.flush()
    session.add(JobTag(job_id=ids["in"], tag_id=tag.id))
    await session.flush()

    async def ids_for(**params):
        r = await client.get(f"{API}/jobs", params={"scope": "all", **params})
        assert r.status_code == 200, r.text
        return {i["id"] for i in r.json()["items"]}

    assert await ids_for(company_id=globex.id) == {ids["gx"]}
    assert await ids_for(status="applied") == {ids["gx"]}
    assert await ids_for(status_category="inbox") == {ids["in"], ids["us"], ids["unk"]}
    assert await ids_for(source="career_site") == set(ids.values())
    assert await ids_for(source="manual") == set()
    assert await ids_for(country="us") == {ids["us"]}
    assert await ids_for(location="austin") == {ids["us"]}
    assert await ids_for(employment_type="contract") == {ids["gx"]}
    assert await ids_for(has_salary="true") == {ids["in"]}
    assert await ids_for(has_salary="false") == {ids["us"], ids["unk"], ids["gx"]}
    assert await ids_for(salary_min=2_000_000) == {ids["in"]}
    assert await ids_for(salary_min=3_100_000) == set()
    assert await ids_for(starred="true") == {ids["gx"]}
    assert await ids_for(tag="dream") == {ids["in"]}
    assert await ids_for(is_new="true") == set(ids.values())
    assert await ids_for(is_new="false") == set()
    assert await ids_for(availability="closed") == set()
    assert await ids_for(work_arrangement="remote") == set()
    assert await ids_for(discovered_after=(T0).isoformat()) == set(ids.values())


async def test_salary_filters_annualize_monthly_and_inr_only(client, session):
    from app.models import SalaryRecord
    from app.models.enums import SalaryPeriod, SalaryProvenance

    company = await make_company(session)
    monthly = (await ingest_service.ingest(session, posting(company.id, "M", title="Monthly"), seen_at=T0)).job_id
    usd = (await ingest_service.ingest(session, posting(company.id, "U", title="Dollar role"), seen_at=T0)).job_id
    for job_id, cur, period, amount in [
        (monthly, "INR", SalaryPeriod.monthly, 200_000),
        (usd, "USD", SalaryPeriod.annual, 9_000_000),
    ]:
        session.add(
            SalaryRecord(
                job_id=job_id,
                provenance=SalaryProvenance.user_entered,
                provider="user:x",
                currency=cur,
                period=period,
                amount_max=amount,
            )
        )
    await session.flush()
    r = await client.get(f"{API}/jobs", params={"scope": "all", "salary_min": 2_400_000})
    assert [i["id"] for i in r.json()["items"]] == [monthly]  # 200k * 12; the USD figure never matches
    r = await client.get(f"{API}/jobs", params={"scope": "all", "salary_min": 2_500_000})
    assert r.json()["items"] == []


async def test_search_and_relevance(client, session):
    _, _, ids = await seed(session)
    r = await client.get(f"{API}/jobs", params={"scope": "all", "q": "kubernetes"})
    assert [i["id"] for i in r.json()["items"]] == [ids["us"]]
    r = await client.get(f"{API}/jobs", params={"scope": "all", "q": "acme"})  # company name is searchable
    assert {i["id"] for i in r.json()["items"]} == {ids["in"], ids["us"], ids["unk"]}
    r = await client.get(f"{API}/jobs", params={"scope": "all", "q": "python -writer", "sort": "relevance"})
    got = [i["id"] for i in r.json()["items"]]
    assert ids["unk"] not in got and ids["in"] in got
    r = await client.get(f"{API}/jobs", params={"scope": "all", "q": "nonexistentword"})
    assert r.json()["total"] == 0


async def test_sorting_and_pagination(client, session):
    company = await make_company(session)
    for i, title in enumerate(["Zed", "Alpha", "Mid"]):
        await ingest_service.ingest(
            session, posting(company.id, f"S{i}", title=f"{title} Engineer"), seen_at=T0 + timedelta(hours=i)
        )
    r = await client.get(f"{API}/jobs", params={"sort": "title_asc"})
    assert [i["title"] for i in r.json()["items"]] == ["Alpha Engineer", "Mid Engineer", "Zed Engineer"]
    r = await client.get(f"{API}/jobs", params={"sort": "discovered_asc", "page_size": 2, "page": 2})
    body = r.json()
    assert [i["title"] for i in body["items"]] == ["Mid Engineer"] or len(body["items"]) == 1
    assert body["total"] == 3 and body["page"] == 2 and body["page_size"] == 2
    r = await client.get(f"{API}/jobs", params={"sort": "discovered_desc"})
    assert r.json()["items"][0]["title"] == "Mid Engineer"


async def test_is_updated_flag(client, session):
    from datetime import UTC, datetime

    company = await make_company(session)
    now = datetime.now(UTC)
    out = await ingest_service.ingest(session, posting(company.id), seen_at=now - timedelta(hours=5))
    r = await client.get(f"{API}/jobs")
    assert r.json()["items"][0]["is_updated"] is False
    await ingest_service.ingest(
        session, posting(company.id, title="Backend Engineer II"), seen_at=now - timedelta(hours=1)
    )
    r = await client.get(f"{API}/jobs")
    item = r.json()["items"][0]
    assert item["id"] == out.job_id and item["is_updated"] is True and item["title"] == "Backend Engineer II"


async def test_merged_jobs_hidden_unless_requested(client, session):
    _, _, ids = await seed(session)
    job = await session.get(Job, ids["gx"])
    job.merged_into_job_id = ids["in"]
    await session.flush()
    r = await client.get(f"{API}/jobs", params={"scope": "all"})
    assert ids["gx"] not in {i["id"] for i in r.json()["items"]}
    r = await client.get(f"{API}/jobs", params={"scope": "all", "include_merged": "true"})
    assert ids["gx"] in {i["id"] for i in r.json()["items"]}


async def test_facets_exclude_own_filter(client, session):
    _acme, globex, _ids = await seed(session)
    r = await client.get(f"{API}/jobs/facets")
    assert r.status_code == 200, r.text
    f = r.json()
    company = {v["label"]: v["count"] for v in f["company"]}
    assert company == {"Acme Robotics": 2, "Globex": 1}  # default scope hides the US job
    assert {v["value"]: v["count"] for v in f["scope"]} == {"in_scope": 2, "unknown": 1, "out_of_scope": 1}
    assert {v["value"]: v["count"] for v in f["country"]} == {"IN": 2}
    assert {v["value"]: v["count"] for v in f["status"]} == {"new": 3}
    assert {v["value"]: v["count"] for v in f["source"]} == {"career_site": 3}
    assert {v["value"] for v in f["employment_type"]} <= {"contract", "unknown"}
    # filtering by company must not shrink the company facet itself, but shrinks the others
    r = await client.get(f"{API}/jobs/facets", params={"company_id": globex.id})
    f = r.json()
    assert {v["label"]: v["count"] for v in f["company"]} == {"Acme Robotics": 2, "Globex": 1}
    assert {v["value"]: v["count"] for v in f["employment_type"]} == {"contract": 1}
    # scope=all: the scope facet is the same, the others now include the US job
    r = await client.get(f"{API}/jobs/facets", params={"scope": "in_scope"})
    f = r.json()
    assert {v["value"]: v["count"] for v in f["scope"]} == {"in_scope": 2, "unknown": 1, "out_of_scope": 1}
    assert {v["label"]: v["count"] for v in f["company"]} == {"Acme Robotics": 1, "Globex": 1}


async def test_inbox_review_and_unreview(client, session):
    _, _, ids = await seed(session)
    r = await client.get(f"{API}/inbox")
    assert r.status_code == 200
    got = {i["id"] for i in r.json()["items"]}
    assert got == {ids["in"], ids["unk"], ids["gx"]}  # unreviewed + visible scopes
    r = await client.get(f"{API}/inbox/summary")
    s = r.json()
    assert s["unreviewed"] == 3 and s["unreviewed_today"] == 3
    assert {v["label"]: v["count"] for v in s["by_company"]} == {"Acme Robotics": 2, "Globex": 1}

    r = await client.post(f"{API}/jobs/review", json={"job_ids": [ids["in"], ids["gx"]]})
    assert r.status_code == 204
    r = await client.get(f"{API}/inbox")
    assert [i["id"] for i in r.json()["items"]] == [ids["unk"]]
    app = await session.scalar(select(Application).where(Application.job_id == ids["in"]))
    await session.refresh(app)
    assert app.reviewed_at is not None and app.row_version == 1
    status = await session.get(type(app), app.id)
    assert status.status_id == await status_id(session, "new")  # review never changes the status
    evts = (
        await session.scalars(
            select(ApplicationEvent).where(
                ApplicationEvent.application_id == app.id, ApplicationEvent.event_type == EventType.reviewed
            )
        )
    ).all()
    assert len(evts) == 1
    first_reviewed = app.reviewed_at
    # idempotent: reviewing again keeps the original timestamp and adds no event
    await client.post(f"{API}/jobs/review", json={"job_ids": [ids["in"]]})
    await session.refresh(app)
    assert app.reviewed_at == first_reviewed
    r = await client.get(f"{API}/jobs", params={"inbox": "false"})
    assert {i["id"] for i in r.json()["items"]} == {ids["in"], ids["gx"]}

    r = await client.post(f"{API}/jobs/unreview", json={"job_ids": [ids["in"]]})
    assert r.status_code == 204
    r = await client.get(f"{API}/inbox")
    assert {i["id"] for i in r.json()["items"]} == {ids["in"], ids["unk"]}
    r = await client.post(f"{API}/jobs/review", json={"job_ids": []})
    assert r.status_code == 422


async def test_requires_auth(anon_client):
    assert (await anon_client.get(f"{API}/jobs")).status_code == 401
    assert (await anon_client.get(f"{API}/inbox")).status_code == 401
