"""HTTP tests: duplicates, merge and re-parenting, salary provenance and providers."""

from datetime import timedelta

from sqlalchemy import func, select

from app.discovery.ingest import ingest_service
from app.models import (
    Application,
    ApplicationEvent,
    Contact,
    DuplicateSuggestion,
    Interview,
    Job,
    JobTag,
    Note,
    Posting,
    Reminder,
    SalaryRecord,
    Tag,
)
from app.models.enums import (
    DuplicateState,
    EventType,
    ReminderKind,
    SalaryProvenance,
)
from app.salary.providers import SalaryProvider, list_providers
from tests.factories import application_for, make_company, status_id
from tests.jobs.helpers import T0, posting

API = "/api/v1"


async def similar_pair(session):
    company = await make_company(session, "Pairco")
    a = await ingest_service.ingest(session, posting(company.id, "A", title="Senior Software Engineer"), seen_at=T0)
    b = await ingest_service.ingest(
        session,
        posting(company.id, "B", source_key="capture", title="Sr. Software Engineer", salary_text="₹30-40 LPA"),
        seen_at=T0 + timedelta(hours=1),
    )
    return company, a, b


async def test_duplicate_listing_confirm_and_dismiss(client, session):
    company, a, b = await similar_pair(session)
    other = await ingest_service.ingest(
        session, posting(company.id, "C", title="Senior Software Engineer II"), seen_at=T0
    )
    assert other.duplicate_suggestion_ids  # similar to both

    r = await client.get(f"{API}/duplicates")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == len(body["items"]) >= 2
    scores = [s["score"] for s in body["items"]]
    assert scores == sorted(scores, reverse=True)
    top = body["items"][0]
    assert top["state"] == "pending" and top["job_a"]["id"] < top["job_b"]["id"] and top["reasons"]

    r = await client.get(f"{API}/jobs/{a.job_id}/duplicates")
    assert {s["job_b"]["id"] for s in r.json()} >= {b.job_id}
    assert (await client.get(f"{API}/jobs/{a.job_id}")).json()["pending_duplicate_count"] >= 1

    # dismiss one pair, confirm another
    pair = await session.scalar(
        select(DuplicateSuggestion).where(
            DuplicateSuggestion.job_a_id == a.job_id, DuplicateSuggestion.job_b_id == b.job_id
        )
    )
    r = await client.post(f"{API}/duplicates/{pair.id}/dismiss")
    assert r.status_code == 204
    await session.refresh(pair)
    assert pair.state == DuplicateState.dismissed and pair.resolved_at is not None
    ids = {s["id"] for s in (await client.get(f"{API}/duplicates")).json()["items"]}
    assert pair.id not in ids
    assert (await client.post(f"{API}/duplicates/{pair.id}/confirm")).status_code == 409

    other_pair = await session.scalar(
        select(DuplicateSuggestion).where(DuplicateSuggestion.id == other.duplicate_suggestion_ids[0])
    )
    r = await client.post(f"{API}/duplicates/{other_pair.id}/confirm")
    assert r.status_code == 200, r.text
    survivor = r.json()
    assert survivor["id"] == other_pair.job_a_id and len(survivor["postings"]) == 2
    await session.refresh(other_pair)
    assert other_pair.state == DuplicateState.confirmed
    assert (await client.post(f"{API}/duplicates/{other_pair.id}/dismiss")).status_code == 409
    assert (await client.post(f"{API}/duplicates/9999999/confirm")).status_code == 404
    # merged jobs no longer appear in pending lists
    listed = (await client.get(f"{API}/duplicates")).json()["items"]
    merged_id = other_pair.job_b_id
    assert all(merged_id not in (s["job_a"]["id"], s["job_b"]["id"]) for s in listed)


async def test_merge_reparents_application_children(client, session):
    _company, a, b = await similar_pair(session)
    app_a = await application_for(session, await session.get(Job, a.job_id))
    app_b = await application_for(session, await session.get(Job, b.job_id))
    app_a.status_id = await status_id(session, "interested")
    app_b.status_id = await status_id(session, "applied")
    app_b.applied_at = T0 + timedelta(days=2)
    session.add(Note(application_id=app_b.id, body="note on b"))
    session.add(Contact(application_id=app_b.id, role="recruiter", name="Rita"))
    interview = Interview(application_id=app_b.id, round_name="Screen")
    session.add(interview)
    session.add(
        Reminder(application_id=app_b.id, kind=ReminderKind.custom, title="ping", due_at=T0 + timedelta(days=9))
    )
    tag = Tag(name="hot")
    session.add(tag)
    await session.flush()
    session.add(JobTag(job_id=b.job_id, tag_id=tag.id))
    await session.flush()
    b_events = await session.scalar(
        select(func.count()).select_from(ApplicationEvent).where(ApplicationEvent.application_id == app_b.id)
    )
    a_events = await session.scalar(
        select(func.count()).select_from(ApplicationEvent).where(ApplicationEvent.application_id == app_a.id)
    )

    r = await client.post(f"{API}/jobs/{a.job_id}/merge", json={"other_job_id": b.job_id})
    assert r.status_code == 200, r.text
    merged = r.json()
    assert merged["id"] == a.job_id and len(merged["postings"]) == 2
    assert merged["status"]["key"] == "interested"  # survivor keeps its status
    assert merged["tags"] == ["hot"]
    assert merged["salary"]["provenance"] == "employer_provided"  # salary record moved to the survivor

    tomb = await session.get(Job, b.job_id)
    await session.refresh(tomb)
    assert tomb.merged_into_job_id == a.job_id
    for model in (Note, Contact, Interview, Reminder):
        assert (
            await session.scalar(select(func.count()).select_from(model).where(model.application_id == app_a.id)) == 1
        )
        assert (
            await session.scalar(select(func.count()).select_from(model).where(model.application_id == app_b.id)) == 0
        )
    assert (
        await session.scalar(select(func.count()).select_from(Application).where(Application.job_id == b.job_id)) == 1
    )
    assert await session.scalar(select(func.count()).select_from(Posting).where(Posting.job_id == b.job_id)) == 0
    assert (
        await session.scalar(select(func.count()).select_from(SalaryRecord).where(SalaryRecord.job_id == b.job_id)) == 0
    )
    assert await session.scalar(select(func.count()).select_from(JobTag).where(JobTag.job_id == b.job_id)) == 0
    new_events = await session.scalar(
        select(func.count()).select_from(ApplicationEvent).where(ApplicationEvent.application_id == app_a.id)
    )
    assert new_events == a_events + b_events + 1  # b's history copied + job_merged
    merged_evt = await session.scalar(
        select(ApplicationEvent).where(
            ApplicationEvent.application_id == app_a.id, ApplicationEvent.event_type == EventType.job_merged
        )
    )
    assert merged_evt.payload["merged_job_id"] == b.job_id
    pair = await session.scalar(select(DuplicateSuggestion))
    assert pair.state == DuplicateState.confirmed

    # tombstone is hidden by default but readable
    assert b.job_id not in {i["id"] for i in (await client.get(f"{API}/jobs", params={"scope": "all"})).json()["items"]}
    tomb_view = (await client.get(f"{API}/jobs/{b.job_id}")).json()
    assert tomb_view["merged_into_job_id"] == a.job_id


async def test_merge_take_status_from_other(client, session):
    _, a, b = await similar_pair(session)
    app_a = await application_for(session, await session.get(Job, a.job_id))
    app_b = await application_for(session, await session.get(Job, b.job_id))
    app_b.status_id = await status_id(session, "applied")
    app_b.applied_at = T0 + timedelta(days=2)
    await session.flush()
    r = await client.post(
        f"{API}/jobs/{a.job_id}/merge", json={"other_job_id": b.job_id, "take_status_from_other": True}
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"]["key"] == "applied"
    await session.refresh(app_a)
    assert app_a.applied_at == T0 + timedelta(days=2) and app_a.row_version == 2
    evt = await session.scalar(
        select(ApplicationEvent).where(
            ApplicationEvent.application_id == app_a.id,
            ApplicationEvent.event_type == EventType.status_changed,
            ApplicationEvent.to_status_id == app_b.status_id,
            ApplicationEvent.actor == "user",
        )
    )
    assert evt is not None


async def test_merge_conflicts(client, session):
    _, a, b = await similar_pair(session)
    r = await client.post(f"{API}/jobs/{a.job_id}/merge", json={"other_job_id": a.job_id})
    assert r.status_code == 409 and r.json()["error"]["code"] == "merge_self"
    assert (await client.post(f"{API}/jobs/{a.job_id}/merge", json={"other_job_id": 99999999})).status_code == 404
    assert (await client.post(f"{API}/jobs/{a.job_id}/merge", json={"other_job_id": b.job_id})).status_code == 200
    r = await client.post(f"{API}/jobs/{a.job_id}/merge", json={"other_job_id": b.job_id})
    assert r.status_code == 409 and r.json()["error"]["code"] == "merge_target_merged"
    r = await client.post(f"{API}/jobs/{b.job_id}/merge", json={"other_job_id": a.job_id})  # b is a tombstone
    assert r.status_code == 409


async def test_merge_resolves_conflicting_current_salary_records(client, session):
    company = await make_company(session)
    a = await ingest_service.ingest(
        session, posting(company.id, "A", title="Alpha role", salary_text="₹10 LPA"), seen_at=T0
    )
    b = await ingest_service.ingest(
        session, posting(company.id, "B", title="Beta job", salary_text="₹20 LPA"), seen_at=T0
    )
    r = await client.post(f"{API}/jobs/{a.job_id}/merge", json={"other_job_id": b.job_id})
    assert r.status_code == 200, r.text
    recs = (await session.scalars(select(SalaryRecord).where(SalaryRecord.job_id == a.job_id))).all()
    assert len(recs) == 2 and sum(1 for x in recs if x.is_current) == 1


async def test_salary_provenance_separation(client, session):
    company = await make_company(session)
    out = await ingest_service.ingest(session, posting(company.id, salary_text="₹12-18 LPA"), seen_at=T0)
    job_id = out.job_id

    view = (await client.get(f"{API}/jobs/{job_id}/salary")).json()
    assert view["available"] is True and view["estimates"] == []
    assert [r["provenance"] for r in view["employer_provided"]] == ["employer_provided"]

    body = {
        "currency": "INR",
        "period": "annual",
        "amount_min": "1500000",
        "amount_max": "2200000",
        "source_name": "AmbitionBox",
        "source_url": "https://www.ambitionbox.com/x",
        "role_match": "close",
        "confidence": "medium",
    }
    r = await client.post(f"{API}/jobs/{job_id}/salary", json=body)
    assert r.status_code == 201, r.text
    view = r.json()
    assert len(view["employer_provided"]) == 1 and len(view["estimates"]) == 1
    est = view["estimates"][0]
    assert est["provenance"] == "user_entered" and est["provider"] == "user:ambitionbox"
    assert (
        est["amount_max"] == "2200000.00"
        and est["role_match"] == "close"
        and est["source_url"].startswith("https://www.ambitionbox")
    )
    assert view["employer_provided"][0]["amount_max"] == "1800000.00"  # untouched

    # entering again from the same source replaces the current record; another source coexists
    r = await client.post(f"{API}/jobs/{job_id}/salary", json={**body, "amount_min": "1600000"})
    assert [e["amount_min"] for e in r.json()["estimates"]] == ["1600000.00"]
    r = await client.post(f"{API}/jobs/{job_id}/salary", json={**body, "source_name": "Glassdoor"})
    assert {e["provider"] for e in r.json()["estimates"]} == {"user:ambitionbox", "user:glassdoor"}
    rows = (await session.scalars(select(SalaryRecord).where(SalaryRecord.job_id == job_id))).all()
    assert len(rows) == 4 and sum(1 for x in rows if x.is_current) == 3

    events = (
        await session.scalars(select(ApplicationEvent).where(ApplicationEvent.event_type == EventType.salary_added))
    ).all()
    assert len(events) == 3

    # list summary prefers the employer figure over estimates
    item = (await client.get(f"{API}/jobs")).json()["items"][0]
    assert item["salary"]["provenance"] == "employer_provided"

    # delete: user_entered only
    est_id = r.json()["estimates"][0]["id"]
    assert (await client.delete(f"{API}/jobs/{job_id}/salary/{est_id}")).status_code == 204
    view = (await client.get(f"{API}/jobs/{job_id}/salary")).json()
    assert est_id not in {e["id"] for e in view["estimates"]}
    employer_id = view["employer_provided"][0]["id"]
    r = await client.delete(f"{API}/jobs/{job_id}/salary/{employer_id}")
    assert r.status_code == 409 and r.json()["error"]["code"] == "salary_not_deletable"
    assert (await client.delete(f"{API}/jobs/{job_id}/salary/99999999")).status_code == 404
    kept = await session.get(SalaryRecord, est_id)
    await session.refresh(kept)
    assert kept.is_current is False  # history kept


async def test_salary_not_available_and_validation(client, session):
    company = await make_company(session)
    out = await ingest_service.ingest(session, posting(company.id), seen_at=T0)
    view = (await client.get(f"{API}/jobs/{out.job_id}/salary")).json()
    assert view == {"employer_provided": [], "estimates": [], "available": False}
    r = await client.post(f"{API}/jobs/{out.job_id}/salary", json={"source_name": "X"})
    assert r.status_code == 422
    r = await client.post(
        f"{API}/jobs/{out.job_id}/salary", json={"source_name": "X", "amount_min": 5, "amount_max": 1}
    )
    assert r.status_code == 422
    assert (await client.get(f"{API}/jobs/9999999/salary")).status_code == 404


def test_salary_providers_list():
    providers = {p.key: p for p in list_providers()}
    assert set(providers) == {"job_posting", "user", "ambitionbox_api"}
    assert providers["job_posting"].provenance == SalaryProvenance.employer_provided
    assert providers["job_posting"].enabled and providers["job_posting"].authorized
    assert providers["user"].provenance == SalaryProvenance.user_entered and providers["user"].enabled
    amb = providers["ambitionbox_api"]
    assert amb.provenance == SalaryProvenance.external_estimate
    assert amb.enabled is False and amb.authorized is False
    assert "scraper" in amb.description and "disabled" in amb.description
    assert SalaryProvider is not None
