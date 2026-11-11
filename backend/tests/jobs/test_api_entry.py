"""HTTP tests: manual entry, bookmarklet capture, detail, edit, versions."""

from datetime import timedelta

from sqlalchemy import func, select

from app.discovery.ingest import ingest_service
from app.models import ApplicationEvent, Company, CompanyAlias, Job, Posting
from app.models.enums import EventType, ScopeMatch
from tests.factories import application_for, make_company, status_id
from tests.jobs.helpers import T0, posting

API = "/api/v1"
BEARER = {"Authorization": "Bearer test-capture-token"}


async def test_manual_entry_creates_unmonitored_company(client, session):
    r = await client.post(
        f"{API}/jobs",
        json={
            "company_name": "Brand New Co",
            "title": "Staff Engineer",
            "location_text": "Chennai",
            "description": "Own the platform. 8+ years of experience.\n\nUse Python and Kubernetes.",
            "salary_text": "₹40-50 LPA",
            "origin_platform": "LinkedIn",
            "source_url": "https://www.linkedin.com/jobs/view/123",
        },
    )
    assert r.status_code == 201, r.text
    job = r.json()
    assert job["title"] == "Staff Engineer" and job["company"]["name"] == "Brand New Co"
    assert job["country_codes"] == ["IN"] and job["location_scope"] == "in_scope"
    assert job["skills"] == ["Python", "Kubernetes"] and job["experience_min_years"] == "8.0"
    assert job["salary"]["provenance"] == "employer_provided"
    assert job["sources"] == ["manual"] and job["status"]["key"] == "new" and job["reviewed"] is False
    assert job["postings"][0]["external_id"].startswith("manual-") and job["postings"][0]["is_primary"]
    assert "<p>Own the platform" in job["description_html"]
    company = await session.scalar(select(Company).where(Company.name == "Brand New Co"))
    assert company is not None and company.monitoring_enabled is False and company.slug == "brand-new-co"
    posting_row = await session.scalar(select(Posting).where(Posting.job_id == job["id"]))
    assert posting_row.source_url == "https://www.linkedin.com/jobs/view/123"
    from app.models import JobVersion

    version = await session.get(JobVersion, posting_row.current_version_id)
    assert version.raw_payload["origin_platform"] == "LinkedIn"
    discovered = await session.scalar(
        select(ApplicationEvent).where(ApplicationEvent.event_type == EventType.discovered)
    )
    assert discovered.payload["origin_platform"] == "LinkedIn"


async def test_manual_entry_resolves_company_by_name_alias_and_id(client, session):
    acme = await make_company(session, "Acme Corp")
    session.add(CompanyAlias(company_id=acme.id, alias="Acme Industries"))
    await session.flush()
    ids = []
    for body in (
        {"company_name": "acme corp", "title": "A1"},
        {"company_name": "ACME INDUSTRIES", "title": "A2"},
        {"company_id": acme.id, "title": "A3"},
    ):
        r = await client.post(f"{API}/jobs", json=body)
        assert r.status_code == 201, r.text
        ids.append(r.json()["company"]["id"])
    assert ids == [acme.id] * 3
    assert await session.scalar(select(func.count()).select_from(Company).where(Company.name == "Acme Corp")) == 1
    r = await client.post(f"{API}/jobs", json={"title": "x"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "company_required"
    r = await client.post(f"{API}/jobs", json={"company_id": 99999999, "title": "x"})
    assert r.status_code == 404


async def test_manual_entry_country_code_and_unknown_status(client, session):
    r = await client.post(
        f"{API}/jobs",
        json={"company_name": "Foreign Co", "title": "Dev", "location_text": "Somewhere", "country_code": "us"},
    )
    assert r.status_code == 201
    assert r.json()["country_codes"] == ["US"] and r.json()["location_scope"] == "out_of_scope"
    r = await client.post(
        f"{API}/jobs", json={"company_name": "Foreign Co", "title": "Dev2", "initial_status": "bogus"}
    )
    assert r.status_code == 404 and r.json()["error"]["code"] == "unknown_status"


async def test_manual_entry_with_initial_status(client, session):
    r = await client.post(
        f"{API}/jobs", json={"company_name": "Status Co", "title": "Dev", "initial_status": "applied"}
    )
    assert r.status_code == 201, r.text
    job = r.json()
    assert job["status"]["key"] == "applied" and job["reviewed"] is True


JSON_LD = {
    "@context": "https://schema.org",
    "@graph": [
        {"@type": "WebSite", "name": "Careers"},
        {
            "@type": "JobPosting",
            "title": "Senior Data Engineer",
            "description": (
                "<p>Build <b>pipelines</b> with Spark and Airflow. 5+ years of experience.</p><script>x()</script>"
            ),
            "datePosted": "2026-10-28",
            "validThrough": "2026-12-31T00:00:00Z",
            "employmentType": ["FULL_TIME"],
            "hiringOrganization": {"@type": "Organization", "name": "Initech"},
            "identifier": {"@type": "PropertyValue", "name": "Initech", "value": "REQ-77"},
            "jobLocation": [
                {
                    "@type": "Place",
                    "address": {"addressLocality": "Pune", "addressRegion": "MH", "addressCountry": "IN"},
                },
                {"@type": "Place", "address": {"addressLocality": "Hyderabad", "addressCountry": {"name": "India"}}},
            ],
            "baseSalary": {
                "@type": "MonetaryAmount",
                "currency": "INR",
                "value": {"@type": "QuantitativeValue", "minValue": 2500000, "maxValue": 3500000, "unitText": "YEAR"},
            },
        },
    ],
}


async def test_capture_json_ld_and_dedupe(client, session):
    body = {"page_url": "https://boards.example.com/jobs/77?utm_source=x#top", "page_title": "x", "json_ld": [JSON_LD]}
    r = await client.post(f"{API}/jobs/capture", json=body)
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["created"] is True and res["title"] == "Senior Data Engineer" and res["company_name"] == "Initech"
    assert res["needs_review"] == [] and res["app_url"] == f"/jobs/{res['job_id']}"

    job = (await client.get(f"{API}/jobs/{res['job_id']}")).json()
    assert job["sources"] == ["capture"] and job["employment_type"] == "full_time"
    assert job["posted_at_precision"] == "day" and job["posted_at"].startswith("2026-10-28")
    assert job["deadline_at"].startswith("2026-12-31")
    assert job["country_codes"] == ["IN"] and len(job["locations"]) == 2
    assert job["postings"][0]["requisition_id"] == "REQ-77"
    assert job["salary"]["provenance"] == "employer_provided" and job["salary"]["amount_max"] == "3500000.00"
    assert "script" not in job["description_html"] and "<b>pipelines</b>" in job["description_html"]
    assert job["skills"][:2] == ["Spark", "Airflow"]

    # same URL (tracking params differ) -> same job, not created again
    again = await client.post(
        f"{API}/jobs/capture", json={**body, "page_url": "https://boards.example.com/jobs/77?utm_campaign=y"}
    )
    assert again.status_code == 200 and again.json()["created"] is False
    assert again.json()["job_id"] == res["job_id"]
    assert await session.scalar(select(func.count()).select_from(Job)) == 1


async def test_capture_with_bearer_token_only(anon_client, session):
    body = {"page_url": "https://x.example.com/j/1", "json_ld": [JSON_LD]}
    assert (await anon_client.post(f"{API}/jobs/capture", json=body)).status_code == 401
    r = await anon_client.post(f"{API}/jobs/capture", json=body, headers=BEARER)
    assert r.status_code == 200 and r.json()["created"] is True
    bad = await anon_client.post(f"{API}/jobs/capture", json=body, headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401


async def test_capture_fallbacks_and_needs_review(client, session):
    r = await client.post(
        f"{API}/jobs/capture",
        json={
            "page_url": "https://jobs.example.com/view/9",
            "page_title": "Platform Engineer | Example Jobs",
            "meta": {"og:title": "Platform Engineer", "og:description": "Run clusters"},
            "selected_text": "We need a platform engineer who knows Kubernetes. 4-6 years of experience.",
        },
    )
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["title"] == "Platform Engineer" and res["company_name"] == "Unknown company"
    assert res["needs_review"] == ["company", "location"]
    job = (await client.get(f"{API}/jobs/{res['job_id']}")).json()
    assert job["description_text"].startswith("We need a platform engineer")
    assert job["location_scope"] == "unknown" and job["skills"] == ["Kubernetes"]

    r = await client.post(f"{API}/jobs/capture", json={"page_url": "https://jobs.example.com/view/10"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "capture_no_title"
    r = await client.post(
        f"{API}/jobs/capture",
        json={
            "page_url": "https://jobs.example.com/view/11",
            "page_title": "Some Job",
            "meta": {"og:site_name": "Foo Inc"},
        },
    )
    assert r.json()["company_name"] == "Foo Inc"
    assert set(r.json()["needs_review"]) == {"company", "location", "description"}


async def test_capture_remote_telecommute_and_salary_variants(client, session):
    ld = {
        "@type": "JobPosting",
        "title": "Remote Dev",
        "description": "Plain text description",
        "hiringOrganization": "Remote Inc",
        "jobLocationType": "TELECOMMUTE",
        "applicantLocationRequirements": {"@type": "Country", "name": "India"},
        "baseSalary": {"currency": "USD", "value": {"value": 90000, "unitText": "YEAR"}},
    }
    r = await client.post(f"{API}/jobs/capture", json={"page_url": "https://r.example.com/1", "json_ld": [ld]})
    assert r.status_code == 200, r.text
    job = (await client.get(f"{API}/jobs/{r.json()['job_id']}")).json()
    assert job["work_arrangement"] == "remote" and job["country_codes"] == ["IN"]
    assert job["company"]["name"] == "Remote Inc" and r.json()["needs_review"] == []
    assert job["salary"]["currency"] == "USD" and job["salary"]["amount_min"] == "90000.00"


async def test_capture_attaches_to_existing_job_by_apply_url(client, session):
    company = await make_company(session, "Initech")
    out = await ingest_service.ingest(
        session, posting(company.id, apply_url="https://boards.example.com/jobs/77"), seen_at=T0
    )
    r = await client.post(
        f"{API}/jobs/capture",
        json={"page_url": "https://boards.example.com/jobs/77?ref=li", "json_ld": [JSON_LD]},
    )
    assert r.status_code == 200
    assert r.json()["job_id"] == out.job_id and r.json()["created"] is False


async def test_get_job_detail(client, session):
    company = await make_company(session)
    out = await ingest_service.ingest(
        session, posting(company.id, requisition_id="JR9", salary_text="₹10-12 LPA"), seen_at=T0
    )
    await ingest_service.ingest(
        session, posting(company.id, "CAP1", source_key="capture", requisition_id="JR9"), seen_at=T0
    )
    r = await client.get(f"{API}/jobs/{out.job_id}")
    assert r.status_code == 200, r.text
    j = r.json()
    assert [p["source_key"] for p in j["postings"]] == ["career_site", "capture"]
    assert [p["is_primary"] for p in j["postings"]] == [True, False]
    assert [p["version_count"] for p in j["postings"]] == [1, 1]
    assert j["pending_duplicate_count"] == 0 and j["merged_into_job_id"] is None
    assert j["salary_text"] == "₹10-12 LPA" and j["experience_min_years"] == "3.0"
    assert j["experience_text"] == "3+ years of experience required."
    assert (await client.get(f"{API}/jobs/99999999")).status_code == 404


async def test_patch_manual_job_content_creates_version(client, session):
    created = (
        await client.post(
            f"{API}/jobs",
            json={"company_name": "Edit Co", "title": "Dev", "location_text": "Pune", "description": "Old"},
        )
    ).json()
    r = await client.patch(
        f"{API}/jobs/{created['id']}",
        json={"title": "Senior Dev", "location_text": "Austin, TX", "description": "New text with Python"},
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["title"] == "Senior Dev" and j["country_codes"] == ["US"] and j["location_scope"] == "out_of_scope"
    assert j["skills"] == ["Python"] and j["description_text"] == "New text with Python"
    assert [p["version_count"] for p in j["postings"]] == [2]
    versions = (await client.get(f"{API}/jobs/{created['id']}/versions")).json()
    assert [v["version_no"] for v in versions] == [2, 1] and "title" in versions[0]["changed_fields"]
    # clearing a field with an explicit null
    r = await client.patch(f"{API}/jobs/{created['id']}", json={"department": None, "deadline_at": None})
    assert r.status_code == 200


async def test_patch_discovered_job_content_is_locked_but_scope_override_works(client, session):
    company = await make_company(session)
    out = await ingest_service.ingest(
        session, posting(company.id, location_text="Austin, TX, United States"), seen_at=T0
    )
    r = await client.patch(f"{API}/jobs/{out.job_id}", json={"title": "Hacked"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "content_locked"

    r = await client.patch(f"{API}/jobs/{out.job_id}", json={"location_scope_override": "in_scope"})
    assert r.status_code == 200 and r.json()["location_scope"] == "in_scope" and r.json()["scope_overridden"] is True
    # ingest keeps the override
    await ingest_service.ingest(session, posting(company.id, location_text="Austin, TX, United States"), seen_at=T0)
    assert (await session.get(Job, out.job_id)).location_scope == ScopeMatch.in_scope

    r = await client.patch(f"{API}/jobs/{out.job_id}", json={"clear_scope_override": True})
    assert r.json()["location_scope"] == "out_of_scope" and r.json()["scope_overridden"] is False
    assert (await client.patch(f"{API}/jobs/9999999", json={"clear_scope_override": True})).status_code == 404


async def test_versions_and_diff(client, session):
    company = await make_company(session)
    out = await ingest_service.ingest(session, posting(company.id, salary_text="₹10 LPA"), seen_at=T0)
    await ingest_service.ingest(
        session,
        posting(company.id, title="Backend Engineer II", salary_text="₹12 LPA", location_text="Pune, India"),
        seen_at=T0 + timedelta(days=1),
    )
    versions = (await client.get(f"{API}/jobs/{out.job_id}/versions")).json()
    assert [v["version_no"] for v in versions] == [2, 1]
    v2, v1 = versions
    diff = (await client.get(f"{API}/jobs/{out.job_id}/versions/{v2['id']}/diff")).json()
    assert diff["previous_version_id"] == v1["id"]
    by_field = {d["field"]: (d["before"], d["after"]) for d in diff["diffs"]}
    assert by_field["title"] == ("Backend Engineer", "Backend Engineer II")
    assert by_field["salary_text"] == ("₹10 LPA", "₹12 LPA")
    assert by_field["location_text"] == ("Bengaluru, Karnataka, India", "Pune, India")
    first = (await client.get(f"{API}/jobs/{out.job_id}/versions/{v1['id']}/diff")).json()
    assert first["previous_version_id"] is None and first["diffs"] == []
    assert (await client.get(f"{API}/jobs/{out.job_id}/versions/99999/diff")).status_code == 404
    other = await ingest_service.ingest(session, posting(company.id, "OTHER", title="Zzz unrelated"), seen_at=T0)
    assert (await client.get(f"{API}/jobs/{other.job_id}/versions/{v2['id']}/diff")).status_code == 404


async def test_application_untouched_by_edits(client, session):
    created = (await client.post(f"{API}/jobs", json={"company_name": "Keep Co", "title": "Dev"})).json()
    app = await application_for(session, await session.get(Job, created["id"]))
    app.status_id = await status_id(session, "interviewing")
    await session.flush()
    await client.patch(f"{API}/jobs/{created['id']}", json={"title": "Dev 2"})
    await session.refresh(app)
    assert app.status_id == await status_id(session, "interviewing")
