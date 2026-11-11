"""Cross-module user journey over the real HTTP stack and database (lead-owned integration test).

company -> manual job + bookmarklet capture -> inbox -> duplicate suggestion -> merge ->
status lifecycle -> follow-up reminder -> Kanban move -> employer closes the job ->
dashboard, tracker and timeline all stay consistent.
"""

from sqlalchemy import select

from app.discovery.ingest import ingest_service
from app.models import Posting


async def test_full_job_search_journey(client, session):
    # 1. Add a company (Workday config validated, no network)
    r = await client.post(
        "/api/v1/companies",
        json={
            "name": "Nvidia",
            "monitoring_enabled": True,
            "extractor_config": {
                "kind": "workday",
                "url": "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite",
            },
            "aliases": ["NVIDIA Corporation"],
        },
    )
    assert r.status_code == 201, r.text
    company = r.json()
    assert company["extractor"] == "workday" and company["next_check_at"] is not None

    # 2. Manual entry (India) and a US-only manual entry (out of scope)
    r = await client.post(
        "/api/v1/jobs",
        json={
            "company_name": "nvidia corporation",
            "title": "Senior CUDA Engineer",
            "location_text": "Bengaluru, India",
            "origin_platform": "LinkedIn",
            "salary_text": "₹40-55 LPA",
        },
    )
    assert r.status_code == 201, r.text
    manual = r.json()
    assert manual["company"]["id"] == company["id"]  # resolved via alias
    assert manual["location_scope"] == "in_scope"
    r = await client.post(
        "/api/v1/jobs", json={"company_id": company["id"], "title": "Sales Lead", "location_text": "Austin, TX"}
    )
    us_job = r.json()
    assert us_job["location_scope"] == "out_of_scope"

    # 3. Inbox honours the India scope by default
    inbox = (await client.get("/api/v1/inbox")).json()
    inbox_ids = {j["id"] for j in inbox["items"]}
    assert manual["id"] in inbox_ids and us_job["id"] not in inbox_ids
    everything = (await client.get("/api/v1/jobs", params={"scope": "all"})).json()
    assert {manual["id"], us_job["id"]} <= {j["id"] for j in everything["items"]}

    # 4. Bookmarklet capture of the same role from another site -> separate job + suggestion (no auto-merge)
    capture = await client.post(
        "/api/v1/jobs/capture",
        headers={"Authorization": "Bearer test-capture-token"},
        json={
            "page_url": "https://www.naukri.com/job-listings-sr-cuda-engineer-123",
            "json_ld": [
                {
                    "@type": "JobPosting",
                    "title": "Sr. CUDA Engineer",
                    "hiringOrganization": {"name": "NVIDIA"},
                    "jobLocation": {"address": {"addressLocality": "Bangalore", "addressCountry": "IN"}},
                    "datePosted": "2026-11-09",
                    "description": "<p>Write <b>CUDA</b> kernels</p>",
                }
            ],
        },
    )
    assert capture.status_code == 200, capture.text
    captured = capture.json()
    assert captured["created"] and captured["job_id"] != manual["id"]
    dupes = (await client.get("/api/v1/duplicates")).json()["items"]
    assert len(dupes) == 1
    detail = (await client.get(f"/api/v1/jobs/{captured['job_id']}")).json()
    assert detail["posted_at_precision"] == "day"  # absolute date preserved

    # 5. Confirm the duplicate -> one job with two postings
    r = await client.post(f"/api/v1/duplicates/{dupes[0]['id']}/confirm")
    assert r.status_code == 200, r.text
    merged = r.json()
    survivor_id = merged["id"]
    assert {p["source_key"] for p in merged["postings"]} == {"manual", "capture"}

    # 6. Status lifecycle with a back-dated application
    async def set_status(key, **extra):
        resp = await client.put(f"/api/v1/jobs/{survivor_id}/status", json={"status": key, **extra})
        assert resp.status_code == 200, resp.text
        return resp.json()

    await set_status("interested")
    await set_status("shortlisted")
    applied = await set_status("applied", occurred_at="2026-11-10T10:00:00+05:30", note="Referral via Priya")
    assert applied["created_reminder_id"] is not None  # auto follow-up
    assert applied["application"]["reviewed_at"] is not None
    assert applied["application"]["next_follow_up_at"] is not None
    assert survivor_id not in {j["id"] for j in (await client.get("/api/v1/inbox")).json()["items"]}

    # 7. Kanban move to Interviewing with optimistic concurrency
    board = (await client.get("/api/v1/applications/board")).json()
    card = next(c for col in board["columns"] for c in col["cards"] if c["job_id"] == survivor_id)
    stale = card["row_version"] - 1
    r = await client.patch(
        f"/api/v1/applications/{card['application_id']}/position",
        json={"status": "interviewing", "expected_row_version": stale},
    )
    assert r.status_code == 409
    r = await client.patch(
        f"/api/v1/applications/{card['application_id']}/position",
        json={"status": "interviewing", "expected_row_version": card["row_version"]},
    )
    assert r.status_code == 200, r.text

    # 8. Employer closes the posting(s): availability changes, my status does NOT
    postings = (await session.scalars(select(Posting).where(Posting.job_id == survivor_id))).all()
    from datetime import UTC, datetime, timedelta

    t0 = datetime.now(UTC)
    for p in postings:
        source_key = "manual" if p.external_id.startswith("manual-") else "capture"
        for hours in (0, 7):
            await ingest_service.apply_misses(
                session,
                source_key=source_key,
                company_id=p.company_id,
                seen_external_ids=set(),
                run_complete=True,
                run_trusted=True,
                now=t0 + timedelta(hours=hours),
            )
    await session.flush()
    job = (await client.get(f"/api/v1/jobs/{survivor_id}")).json()
    assert job["availability"] == "closed"
    assert job["status"]["key"] == "interviewing"

    # 9. Timeline preserved in order, tracker and dashboard consistent
    timeline = (await client.get(f"/api/v1/jobs/{survivor_id}/timeline")).json()
    status_events = [e for e in timeline if e["event_type"] == "status_changed"]
    assert {e["to_status"]["key"] for e in status_events} == {"interested", "shortlisted", "applied", "interviewing"}
    # timeline is ordered by when things happened: the back-dated application sorts before today's events
    applied_event = next(e for e in status_events if e["to_status"]["key"] == "applied")
    assert applied_event["occurred_at"].startswith("2026-11-10T04:30")
    assert timeline == sorted(timeline, key=lambda e: e["occurred_at"], reverse=True)
    assert any(e["event_type"] == "job_closed" for e in timeline)
    assert any(e["event_type"] == "job_merged" for e in timeline)

    tracker = (await client.get("/api/v1/applications")).json()["items"]
    row = next(t for t in tracker if t["job_id"] == survivor_id)
    assert row["availability"] == "closed" and row["status"]["key"] == "interviewing"

    summary = (await client.get("/api/v1/dashboard/summary")).json()
    assert summary["applied"] == 1 and summary["in_process"] == 1
    assert summary["total_discovered"] == 1  # India scope: survivor only (merged + US job excluded)

    salary = (await client.get(f"/api/v1/jobs/{survivor_id}/salary")).json()
    assert salary["available"] and salary["employer_provided"] and not salary["estimates"]
