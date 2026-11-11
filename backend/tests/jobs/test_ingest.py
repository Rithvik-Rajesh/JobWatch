"""Integration tests for the single write path (real PostgreSQL)."""

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select

from app.discovery.ingest import Ingest, canonical_apply_url, ingest_service
from app.models import (
    Application,
    ApplicationEvent,
    Company,
    DuplicateSuggestion,
    Job,
    JobVersion,
    Posting,
    SalaryRecord,
)
from app.models.enums import (
    Availability,
    DatePrecision,
    DuplicateState,
    EventType,
    SalaryPeriod,
    SalaryProvenance,
    ScopeMatch,
    WorkArrangement,
)
from app.settings.registry import put_setting
from tests.factories import application_for, make_company, status_id
from tests.jobs.helpers import T0, posting


async def count(session, model, *where) -> int:
    return await session.scalar(select(func.count()).select_from(model).where(*where))


async def events(session, job_id, event_type=None) -> list[ApplicationEvent]:
    app = await session.scalar(select(Application).where(Application.job_id == job_id))
    q = select(ApplicationEvent).where(ApplicationEvent.application_id == app.id).order_by(ApplicationEvent.id)
    if event_type:
        q = q.where(ApplicationEvent.event_type == event_type)
    return list((await session.scalars(q)).all())


def test_canonical_apply_url():
    url = "HTTPS://Careers.Example.com/apply/123/?utm_source=x&gclid=1&fbclid=2&ref=li&source=a&id=7#frag"
    assert canonical_apply_url(url) == "https://careers.example.com/apply/123?id=7"
    assert canonical_apply_url(None) is None


async def test_ingest_is_idempotent(session):
    company = await make_company(session)
    data = posting(company.id)
    first = await ingest_service.ingest(session, data, seen_at=T0)
    second = await ingest_service.ingest(session, data, seen_at=T0 + timedelta(hours=1))
    assert first.created_job and first.created_posting and first.new_version
    assert not second.created_job and not second.created_posting and not second.new_version
    assert first.job_id == second.job_id and first.posting_id == second.posting_id
    assert await count(session, Job, Job.company_id == company.id) == 1
    assert await count(session, Posting, Posting.company_id == company.id) == 1
    assert await count(session, JobVersion, JobVersion.posting_id == first.posting_id) == 1
    assert await count(session, Application, Application.job_id == first.job_id) == 1
    assert len(await events(session, first.job_id, EventType.discovered)) == 1
    p = await session.get(Posting, first.posting_id)
    assert p.first_seen_at == T0 and p.last_seen_at == T0 + timedelta(hours=1)
    job = await session.get(Job, first.job_id)
    assert job.first_seen_at == T0 and job.last_seen_at == T0 + timedelta(hours=1)


async def test_normalization_fills_job_view(session):
    company = await make_company(session)
    out = await ingest_service.ingest(
        session,
        posting(
            company.id,
            title="Sr. Backend Engg",
            description_html="<p>Python, Django and PostgreSQL. 3-5 years of experience.</p><script>x</script>",
            description_text=None,
        ),
        seen_at=T0,
    )
    job = await session.get(Job, out.job_id)
    assert job.title_normalized == "senior backend engineer"
    assert job.country_codes == ["IN"] and job.location_scope == ScopeMatch.in_scope
    assert job.skills == ["Python", "Django", "PostgreSQL"]
    assert (job.experience_min_years, job.experience_max_years) == (Decimal("3"), Decimal("5"))
    assert (
        "script"
        not in (
            await session.get(JobVersion, (await session.get(Posting, out.posting_id)).current_version_id)
        ).description_html
    )
    assert job.description_text.startswith("Python, Django")
    assert job.primary_posting_id == out.posting_id
    assert job.apply_url == f"https://careers.example.com/job/{'JR1001'}"


async def test_new_version_only_on_content_change(session):
    company = await make_company(session)
    first = await ingest_service.ingest(session, posting(company.id), seen_at=T0)
    same = await ingest_service.ingest(session, posting(company.id), seen_at=T0 + timedelta(days=1))
    assert not same.new_version
    changed = await ingest_service.ingest(
        session,
        posting(company.id, title="Senior Backend Engineer", salary_text="₹20-30 LPA"),
        seen_at=T0 + timedelta(days=2),
    )
    assert changed.new_version and set(changed.changed_fields) == {"title", "salary_text"}
    versions = (
        await session.scalars(
            select(JobVersion).where(JobVersion.posting_id == first.posting_id).order_by(JobVersion.version_no)
        )
    ).all()
    assert [v.version_no for v in versions] == [1, 2]
    assert versions[1].changed_fields and set(versions[1].changed_fields) == {"title", "salary_text"}
    p = await session.get(Posting, first.posting_id)
    assert p.current_version_id == versions[1].id and p.content_hash == versions[1].content_hash
    job = await session.get(Job, first.job_id)
    assert job.title == "Senior Backend Engineer" and job.last_changed_at == T0 + timedelta(days=2)
    (evt,) = await events(session, job.id, EventType.job_updated)
    assert evt.actor.value == "system" and set(evt.payload["changed_fields"]) == {"title", "salary_text"}


async def test_absolute_date_preserved_over_approximate(session):
    company = await make_company(session)
    approx = dict(posted_at=T0 - timedelta(days=3), posted_at_precision=DatePrecision.approximate)
    out = await ingest_service.ingest(
        session, posting(company.id, posted_at_raw="Posted 3 Days Ago", **approx), seen_at=T0
    )
    job = await session.get(Job, out.job_id)
    assert job.posted_at_precision == DatePrecision.approximate

    exact_at = T0 - timedelta(days=5)
    await ingest_service.ingest(
        session,
        posting(company.id, posted_at=exact_at, posted_at_precision=DatePrecision.day),
        seen_at=T0 + timedelta(hours=1),
    )
    await session.refresh(job)
    assert (job.posted_at, job.posted_at_precision) == (exact_at, DatePrecision.day)

    await ingest_service.ingest(
        session,
        posting(
            company.id,
            posted_at=T0 - timedelta(days=1),
            posted_at_precision=DatePrecision.approximate,
            posted_at_raw="Posted Yesterday",
        ),
        seen_at=T0 + timedelta(days=1),
    )
    await session.refresh(job)
    assert (job.posted_at, job.posted_at_precision) == (exact_at, DatePrecision.day)
    assert job.first_seen_at == T0


async def test_relative_posted_text_is_parsed_as_approximate(session):
    company = await make_company(session)
    out = await ingest_service.ingest(session, posting(company.id, posted_at_raw="Posted 3 Days Ago"), seen_at=T0)
    job = await session.get(Job, out.job_id)
    assert job.posted_at == T0 - timedelta(days=3) and job.posted_at_precision == DatePrecision.approximate


async def test_requisition_id_attaches_across_sources(session):
    company = await make_company(session)
    first = await ingest_service.ingest(session, posting(company.id, requisition_id="JR2029481"), seen_at=T0)
    other = await ingest_service.ingest(
        session,
        posting(
            company.id, "cap-1", source_key="capture", requisition_id="jr2029481", title="Backend Engineer (JR2029481)"
        ),
        seen_at=T0 + timedelta(hours=1),
    )
    assert other.attached_to_existing_job and not other.created_job and other.created_posting
    assert other.job_id == first.job_id
    assert await count(session, Job, Job.company_id == company.id) == 1
    assert await count(session, Posting, Posting.job_id == first.job_id) == 2
    assert await count(session, Application, Application.job_id == first.job_id) == 1
    # career_site stays primary
    assert (await session.get(Job, first.job_id)).primary_posting_id == first.posting_id


async def test_canonical_apply_url_attaches_and_other_company_does_not(session):
    company, other_company = await make_company(session), await make_company(session)
    a = await ingest_service.ingest(
        session, posting(company.id, "A", apply_url="https://apply.example.com/j/9?utm_source=li"), seen_at=T0
    )
    b = await ingest_service.ingest(
        session,
        posting(
            company.id,
            "B",
            source_key="manual",
            title="Totally different",
            apply_url="https://APPLY.example.com/j/9/#x",
        ),
        seen_at=T0,
    )
    c = await ingest_service.ingest(
        session, posting(other_company.id, "C", apply_url="https://apply.example.com/j/9"), seen_at=T0
    )
    assert b.attached_to_existing_job and b.job_id == a.job_id
    assert c.created_job and c.job_id != a.job_id
    p = await session.get(Posting, a.posting_id)
    assert p.canonical_apply_url == "https://apply.example.com/j/9"


async def test_similar_title_creates_suggestion_but_separate_jobs(session):
    company = await make_company(session)
    a = await ingest_service.ingest(session, posting(company.id, "A", title="Senior Software Engineer"), seen_at=T0)
    b = await ingest_service.ingest(
        session,
        posting(company.id, "B", source_key="capture", title="Sr. Software Engineer", location_text="Pune, India"),
        seen_at=T0 + timedelta(days=1),
    )
    assert b.created_job and not b.attached_to_existing_job and b.job_id != a.job_id
    assert len(b.duplicate_suggestion_ids) == 1
    s = await session.get(DuplicateSuggestion, b.duplicate_suggestion_ids[0])
    assert (s.job_a_id, s.job_b_id) == (min(a.job_id, b.job_id), max(a.job_id, b.job_id))
    assert s.state == DuplicateState.pending and s.score >= 0.6
    assert s.reasons[0].startswith("title_similarity:") and "same_location" in s.reasons
    assert (await session.get(Job, a.job_id)).merged_into_job_id is None
    assert (await session.get(Job, b.job_id)).merged_into_job_id is None


async def test_same_source_similar_titles_are_not_suggested(session):
    """Distinct external ids on one career site are distinct requisitions -> never a suggestion."""
    company = await make_company(session)
    await ingest_service.ingest(session, posting(company.id, "A", title="Software Engineer"), seen_at=T0)
    b = await ingest_service.ingest(session, posting(company.id, "B", title="Software Engineer II"), seen_at=T0)
    assert b.created_job and b.duplicate_suggestion_ids == []


async def test_suggestions_require_same_company_country_and_recency(session):
    company, other = await make_company(session), await make_company(session)
    await ingest_service.ingest(session, posting(company.id, "A", title="Data Engineer"), seen_at=T0)
    us = await ingest_service.ingest(
        session, posting(company.id, "US", title="Data Engineer", location_text="Austin, TX"), seen_at=T0
    )
    other_co = await ingest_service.ingest(session, posting(other.id, "O", title="Data Engineer"), seen_at=T0)
    old = await ingest_service.ingest(
        session, posting(company.id, "OLD", title="Data Engineer"), seen_at=T0 + timedelta(days=90)
    )
    assert us.duplicate_suggestion_ids == [] and other_co.duplicate_suggestion_ids == []
    # the 90-day-old pair is outside the window, only the (recent) US job was seen at T0 -> also outside
    assert old.duplicate_suggestion_ids == []


async def test_both_unknown_location_are_suggested(session):
    company = await make_company(session)
    await ingest_service.ingest(
        session, posting(company.id, "A", title="Data Engineer", location_text=None), seen_at=T0
    )
    b = await ingest_service.ingest(
        session,
        posting(company.id, "B", source_key="capture", title="Data Engineer", location_text="2 Locations"),
        seen_at=T0,
    )
    s = await session.get(DuplicateSuggestion, b.duplicate_suggestion_ids[0])
    assert "location_unknown" in s.reasons


async def test_dismissed_pair_is_never_suggested_again(session):
    company = await make_company(session)
    await ingest_service.ingest(session, posting(company.id, "A", title="Platform Engineer"), seen_at=T0)
    b = await ingest_service.ingest(
        session, posting(company.id, "B", source_key="capture", title="Platform Engineer"), seen_at=T0
    )
    s = await session.get(DuplicateSuggestion, b.duplicate_suggestion_ids[0])
    s.state = DuplicateState.dismissed
    await session.flush()
    job_b = await session.get(Job, b.job_id)
    capture_id = (await session.get(Posting, b.posting_id)).source_id
    assert await Ingest._suggest_duplicates(session, job_b, T0, source_id=capture_id) == []
    await session.refresh(s)
    assert s.state == DuplicateState.dismissed
    assert await count(session, DuplicateSuggestion) == 1


async def test_misses_ignored_unless_complete_and_trusted(session):
    company = await make_company(session)
    out = await ingest_service.ingest(session, posting(company.id), seen_at=T0)
    for complete, trusted in [(False, True), (True, False), (False, False)]:
        for i in range(3):
            res = await ingest_service.apply_misses(
                session,
                source_key="career_site",
                company_id=company.id,
                seen_external_ids=set(),
                run_complete=complete,
                run_trusted=trusted,
                now=T0 + timedelta(hours=10 * (i + 1)),
            )
            assert res.misses_recorded == 0 and res.closed_posting_ids == []
    p = await session.get(Posting, out.posting_id)
    assert p.consecutive_misses == 0 and p.missing_since is None and p.availability == Availability.open


async def test_closure_needs_two_misses_spanning_six_hours(session):
    company = await make_company(session)
    out = await ingest_service.ingest(session, posting(company.id), seen_at=T0)
    kw = dict(
        source_key="career_site", company_id=company.id, seen_external_ids=set(), run_complete=True, run_trusted=True
    )

    r1 = await ingest_service.apply_misses(session, now=T0 + timedelta(hours=1), **kw)
    assert r1.misses_recorded == 1 and r1.closed_posting_ids == []
    r2 = await ingest_service.apply_misses(session, now=T0 + timedelta(hours=2), **kw)  # 2 misses but only 1h span
    assert r2.closed_posting_ids == []
    p = await session.get(Posting, out.posting_id)
    assert (
        p.consecutive_misses == 2 and p.missing_since == T0 + timedelta(hours=1) and p.availability == Availability.open
    )
    r3 = await ingest_service.apply_misses(session, now=T0 + timedelta(hours=7, minutes=1), **kw)
    assert r3.closed_posting_ids == [out.posting_id] and r3.closed_job_ids == [out.job_id]
    await session.refresh(p)
    assert p.availability == Availability.closed and p.closed_at is not None
    job = await session.get(Job, out.job_id)
    assert job.availability == Availability.closed and job.closed_at == T0 + timedelta(hours=7, minutes=1)
    assert len(await events(session, job.id, EventType.job_closed)) == 1
    assert (await session.get(Company, company.id)).open_jobs_count == 0


async def test_seen_postings_are_not_missed_and_reset(session):
    company = await make_company(session)
    a = await ingest_service.ingest(session, posting(company.id, "A"), seen_at=T0)
    b = await ingest_service.ingest(session, posting(company.id, "B", title="Other role entirely"), seen_at=T0)
    kw = dict(source_key="career_site", company_id=company.id, run_complete=True, run_trusted=True)
    await ingest_service.apply_misses(session, seen_external_ids={"A"}, now=T0 + timedelta(hours=1), **kw)
    pb = await session.get(Posting, b.posting_id)
    assert pb.consecutive_misses == 1 and (await session.get(Posting, a.posting_id)).consecutive_misses == 0
    await ingest_service.ingest(
        session, posting(company.id, "B", title="Other role entirely"), seen_at=T0 + timedelta(hours=2)
    )
    await session.refresh(pb)
    assert pb.consecutive_misses == 0 and pb.missing_since is None


async def test_reopen_and_application_status_untouched(session):
    company = await make_company(session)
    out = await ingest_service.ingest(session, posting(company.id), seen_at=T0)
    app = await application_for(session, await session.get(Job, out.job_id))
    app.status_id = await status_id(session, "interviewing")
    app.reviewed_at = T0 + timedelta(minutes=5)
    await session.flush()
    before = (app.status_id, app.reviewed_at, app.row_version)

    kw = dict(
        source_key="career_site", company_id=company.id, seen_external_ids=set(), run_complete=True, run_trusted=True
    )
    await ingest_service.apply_misses(session, now=T0 + timedelta(hours=1), **kw)
    closed = await ingest_service.apply_misses(session, now=T0 + timedelta(hours=8), **kw)
    assert closed.closed_job_ids == [out.job_id]
    await session.refresh(app)
    assert (app.status_id, app.reviewed_at, app.row_version) == before

    reopened = await ingest_service.ingest(session, posting(company.id), seen_at=T0 + timedelta(days=1))
    assert reopened.reopened and not reopened.created_job and not reopened.new_version
    await session.refresh(app)
    assert (app.status_id, app.reviewed_at, app.row_version) == before
    job = await session.get(Job, out.job_id)
    assert job.availability == Availability.open and job.closed_at is None
    p = await session.get(Posting, out.posting_id)
    assert p.availability == Availability.open and p.missing_since is None and p.consecutive_misses == 0
    assert len(await events(session, job.id, EventType.job_reopened)) == 1
    assert (await session.get(Company, company.id)).open_jobs_count == 1


async def test_job_stays_open_while_another_posting_is_open(session):
    company = await make_company(session)
    a = await ingest_service.ingest(session, posting(company.id, "A", requisition_id="R1"), seen_at=T0)
    await ingest_service.ingest(
        session, posting(company.id, "B", source_key="capture", requisition_id="R1"), seen_at=T0
    )
    kw = dict(
        source_key="career_site", company_id=company.id, seen_external_ids=set(), run_complete=True, run_trusted=True
    )
    await ingest_service.apply_misses(session, now=T0 + timedelta(hours=1), **kw)
    res = await ingest_service.apply_misses(session, now=T0 + timedelta(hours=9), **kw)
    assert res.closed_posting_ids == [a.posting_id] and res.closed_job_ids == []
    job = await session.get(Job, a.job_id)
    assert job.availability == Availability.open
    assert job.primary_posting_id != a.posting_id  # the open capture posting takes over


async def test_location_scope_values(session):
    company = await make_company(session)
    india = await ingest_service.ingest(session, posting(company.id, "IN"), seen_at=T0)
    us = await ingest_service.ingest(
        session, posting(company.id, "US", title="Designer", location_text="Austin, TX, United States"), seen_at=T0
    )
    unknown = await ingest_service.ingest(
        session, posting(company.id, "UNK", title="Writer", location_text="3 Locations"), seen_at=T0
    )
    scopes = [(await session.get(Job, o.job_id)).location_scope for o in (india, us, unknown)]
    assert scopes == [ScopeMatch.in_scope, ScopeMatch.out_of_scope, ScopeMatch.unknown]


async def test_scope_follows_setting_and_respects_override(session):
    company = await make_company(session)
    await put_setting(session, "location_scope", {"countries": ["US"], "include_remote_anywhere": True})
    us = await ingest_service.ingest(session, posting(company.id, "US", location_text="Austin, TX"), seen_at=T0)
    remote = await ingest_service.ingest(
        session, posting(company.id, "R", title="Remote Dev", location_text="Remote - Germany"), seen_at=T0
    )
    india = await ingest_service.ingest(session, posting(company.id, "IN", title="Other Dev"), seen_at=T0)
    assert (await session.get(Job, us.job_id)).location_scope == ScopeMatch.in_scope
    assert (await session.get(Job, remote.job_id)).work_arrangement == WorkArrangement.remote
    assert (await session.get(Job, remote.job_id)).location_scope == ScopeMatch.in_scope
    job = await session.get(Job, india.job_id)
    assert job.location_scope == ScopeMatch.out_of_scope
    job.location_scope, job.scope_overridden = ScopeMatch.in_scope, True
    await session.flush()
    await ingest_service.ingest(session, posting(company.id, "IN", title="Other Dev"), seen_at=T0 + timedelta(days=1))
    await session.refresh(job)
    assert job.location_scope == ScopeMatch.in_scope


async def test_employer_salary_record_created_and_replaced(session):
    company = await make_company(session)
    out = await ingest_service.ingest(session, posting(company.id, salary_text="₹12-18 LPA"), seen_at=T0)
    job = await session.get(Job, out.job_id)
    assert job.has_employer_salary
    (rec,) = (await session.scalars(select(SalaryRecord).where(SalaryRecord.job_id == job.id))).all()
    assert rec.provenance == SalaryProvenance.employer_provided and rec.provider == "job_posting" and rec.is_current
    assert (rec.currency, rec.period) == ("INR", SalaryPeriod.annual)
    assert (rec.amount_min, rec.amount_max) == (Decimal(1_200_000), Decimal(1_800_000))
    assert rec.raw_text == "₹12-18 LPA"

    await ingest_service.ingest(session, posting(company.id, salary_text="₹12-18 LPA"), seen_at=T0 + timedelta(hours=1))
    assert await count(session, SalaryRecord, SalaryRecord.job_id == job.id) == 1  # unchanged -> no churn

    await ingest_service.ingest(session, posting(company.id, salary_text="₹20-25 LPA"), seen_at=T0 + timedelta(days=1))
    recs = (
        await session.scalars(select(SalaryRecord).where(SalaryRecord.job_id == job.id).order_by(SalaryRecord.id))
    ).all()
    assert [r.is_current for r in recs] == [False, True] and recs[1].amount_max == Decimal(2_500_000)


async def test_employer_salary_detected_in_description(session):
    company = await make_company(session)
    out = await ingest_service.ingest(
        session,
        posting(company.id, description_text="Great role. The salary range is $120,000 - $150,000 per year."),
        seen_at=T0,
    )
    rec = await session.scalar(select(SalaryRecord).where(SalaryRecord.job_id == out.job_id))
    assert rec.currency == "USD" and rec.amount_min == Decimal(120_000)
    assert (await session.get(Job, out.job_id)).has_employer_salary


async def test_no_salary_no_record(session):
    company = await make_company(session)
    out = await ingest_service.ingest(session, posting(company.id, salary_text="Competitive"), seen_at=T0)
    assert await count(session, SalaryRecord, SalaryRecord.job_id == out.job_id) == 0
    assert not (await session.get(Job, out.job_id)).has_employer_salary


async def test_company_open_jobs_count(session):
    company = await make_company(session)
    await ingest_service.ingest(session, posting(company.id, "A"), seen_at=T0)
    await ingest_service.ingest(session, posting(company.id, "B", title="Different thing"), seen_at=T0)
    assert (await session.get(Company, company.id)).open_jobs_count == 2


async def test_unknown_source_rejected(session):
    import pytest

    from app.core.errors import ValidationFailed

    company = await make_company(session)
    with pytest.raises(ValidationFailed):
        await ingest_service.ingest(session, posting(company.id, source_key="nope"), seen_at=T0)
