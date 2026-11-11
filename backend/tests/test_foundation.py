"""Foundation checks: migrations, seeds, DB-level invariants, auth/CSRF, error model."""

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.applications.lifecycle import create_tracking_record, record_event
from app.jobs import scope
from app.models import Application, Company, Job, Posting, Source, Status
from app.models.enums import EventType, ScopeMatch, WorkArrangement
from app.settings.registry import LocationScopeSettings, get_setting


async def test_seeded_reference_data(session):
    keys = (await session.scalars(select(Status.key).order_by(Status.sort_order))).all()
    assert keys[:4] == ["new", "interested", "shortlisted", "applied"]
    assert "expired" not in keys and "closed" not in keys  # availability is NOT a status
    sources = set((await session.scalars(select(Source.key))).all())
    assert {"career_site", "manual", "capture", "adzuna"} <= sources
    cfg = await get_setting(session, LocationScopeSettings)
    assert cfg.countries == ["IN"]


async def _company(session, name="Acme") -> Company:
    c = Company(name=name, slug=name.lower())
    session.add(c)
    await session.flush()
    return c


async def _job(session, company: Company, **kw) -> Job:
    job = Job(
        company_id=company.id, title=kw.pop("title", "Backend Engineer"), title_normalized="backend engineer", **kw
    )
    session.add(job)
    await session.flush()
    return job


async def test_posting_identity_is_unique(session):
    company = await _company(session)
    job = await _job(session, company)
    src = await session.scalar(select(Source).where(Source.key == "career_site"))
    session.add(
        Posting(job_id=job.id, source_id=src.id, company_id=company.id, external_id="JR1", source_url="https://x/1")
    )
    await session.flush()
    session.add(
        Posting(job_id=job.id, source_id=src.id, company_id=company.id, external_id="JR1", source_url="https://x/1")
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_one_application_per_job_and_timeline_append_only(session):
    from datetime import UTC, datetime

    company = await _company(session)
    job = await _job(session, company)
    app = await create_tracking_record(session, job.id, discovered_at=datetime.now(UTC), summary="Discovered")
    event = await record_event(session, app.id, EventType.manual, "hello")
    with pytest.raises(DBAPIError):
        async with session.begin_nested():
            await session.execute(text("update application_events set summary='x' where id=:id"), {"id": event.id})
    session.add(Application(job_id=job.id, status_id=app.status_id))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_search_vector_trigger_includes_company_name(session):
    company = await _company(session, "Nvidia")
    job = await _job(session, company, title="CUDA Engineer", description_text="GPU kernels")
    hit = await session.scalar(
        select(Job.id).where(Job.search_vector.op("@@")(text("websearch_to_tsquery('simple', 'nvidia cuda')")))
    )
    assert hit == job.id


@pytest.mark.parametrize(
    ("codes", "arrangement", "cfg", "expected"),
    [
        (["IN"], WorkArrangement.onsite, {}, ScopeMatch.in_scope),
        (["US", "IN"], WorkArrangement.hybrid, {}, ScopeMatch.in_scope),
        (["US"], WorkArrangement.onsite, {}, ScopeMatch.out_of_scope),
        ([], WorkArrangement.unknown, {}, ScopeMatch.unknown),
        (["US"], WorkArrangement.remote, {}, ScopeMatch.out_of_scope),
        (["US"], WorkArrangement.remote, {"include_remote_anywhere": True}, ScopeMatch.in_scope),
    ],
)
def test_location_scope_policy(codes, arrangement, cfg, expected):
    assert scope.evaluate(codes, arrangement, LocationScopeSettings(**cfg)) == expected


async def test_scope_reevaluation_respects_overrides(session):
    company = await _company(session)
    j_in = await _job(session, company, country_codes=["IN"])
    j_us = await _job(session, company, country_codes=["US"])
    j_unknown = await _job(session, company)
    j_forced = await _job(
        session, company, country_codes=["US"], location_scope=ScopeMatch.in_scope, scope_overridden=True
    )
    await scope.reevaluate_all(session, LocationScopeSettings())
    for j in (j_in, j_us, j_unknown, j_forced):
        await session.refresh(j)
    assert (j_in.location_scope, j_us.location_scope, j_unknown.location_scope, j_forced.location_scope) == (
        ScopeMatch.in_scope,
        ScopeMatch.out_of_scope,
        ScopeMatch.unknown,
        ScopeMatch.in_scope,
    )


async def test_auth_required_and_csrf(anon_client, client):
    assert (await anon_client.get("/api/v1/auth/me")).status_code == 401
    r = await anon_client.post("/api/v1/auth/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_credentials"
    assert (await client.get("/api/v1/auth/me")).json() == {"username": "admin"}
    # cookie present but CSRF header missing on a mutating request -> 403
    r = await client.post("/api/v1/jobs/review", json={"job_ids": [1]}, headers={"X-JobWatch-Client": ""})
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf"


async def test_probes(anon_client):
    assert (await anon_client.get("/healthz")).json() == {"status": "ok"}
    assert (await anon_client.get("/readyz")).json() == {"status": "ready"}
