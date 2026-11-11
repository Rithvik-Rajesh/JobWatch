# ruff: noqa: RUF001
import pytest

from app.discovery.adapters.ashby import AshbyAdapter
from app.discovery.adapters.base import IndiaQuery, PortalBlocked, SchemaChanged
from app.discovery.configs import AshbyConfig
from app.models.enums import EmploymentType, WorkArrangement
from tests.discovery_support import FixtureFetcher

INDIA = IndiaQuery()
adapter = AshbyAdapter()


async def listed(org: str, fixture: str):
    http = FixtureFetcher().load(fixture)
    return await adapter.list(AshbyConfig(org=org), INDIA, http), http


async def test_snowflake_parses():
    result, http = await listed("snowflake", "ashby/snowflake.json")
    assert result.complete and result.pages_fetched == 1
    assert http.calls == [
        ("GET", "https://api.ashbyhq.com/posting-api/job-board/snowflake?includeCompensation=true", None, None)
    ]
    ids = [p.external_id for p in result.postings]
    assert ids and len(set(ids)) == len(ids)
    for p in result.postings:
        assert p.location_texts and p.url.startswith("https://jobs.ashbyhq.com/snowflake/") and p.has_full_detail
        assert p.posted_at is not None and p.posted_at_precision.value == "day"
    texts = {t for p in result.postings for t in p.location_texts}
    assert any(t.startswith("IN-") for t in texts)  # 'IN-Pune' style preserved verbatim
    # structured codes come from address.postalAddress.addressCountry only
    us = next(p for p in result.postings if p.location_texts[0].startswith("US-"))
    assert "US" in us.country_codes
    india = next(p for p in result.postings if p.location_texts[0].startswith("IN-"))
    assert india.country_codes == ["IN"] or "IN" in india.country_codes


async def test_snowflake_hybrid_is_not_remote_despite_isremote_flag():
    result, _ = await listed("snowflake", "ashby/snowflake.json")
    hybrid = [p for p in result.postings if p.raw["workplaceType"] == "Hybrid"]
    assert hybrid and all(p.raw["isRemote"] is True and p.remote is False for p in hybrid)
    d = await adapter.detail(AshbyConfig(org="snowflake"), hybrid[0], FixtureFetcher())
    assert d.work_arrangement == WorkArrangement.hybrid


async def test_snowflake_compensation_and_detail_no_requests():
    result, http = await listed("snowflake", "ashby/snowflake.json")
    p = next(p for p in result.postings if p.raw["salary_text"])
    before = len(http.calls)
    d = await adapter.detail(AshbyConfig(org="snowflake"), p, http)
    assert len(http.calls) == before
    assert d.salary_text == p.raw["salary_text"] and d.description_html and d.description_text and d.apply_url
    assert p.employment_type == EmploymentType.full_time


async def test_synthetic_variants():
    result, _ = await listed("synthetic-example", "ashby/synthetic_ashby_variants.json")
    by_title = {p.title: p for p in result.postings}
    assert len(result.postings) == 5  # unlisted job skipped
    assert "Unlisted Internal Role" not in by_title

    india_remote = by_title["Senior Backend Engineer (India Remote)"]
    assert india_remote.remote is True and india_remote.remote_eligibility == ["India"]
    assert india_remote.country_codes == ["IN"] and india_remote.raw["salary_text"] == "₹40L – ₹60L"

    staff = by_title["Staff Engineer"]  # hybrid with secondary locations
    assert staff.remote is False and staff.remote_eligibility == []
    assert staff.location_texts == ["Bengaluru, India", "Remote - India", "Singapore"]
    assert staff.country_codes == ["IN", "SG"] or staff.country_codes == ["IN"]  # Singapore may be unmapped

    bare = by_title["Developer Advocate"]
    assert bare.remote is True and bare.remote_eligibility == [] and bare.country_codes == []

    onsite = by_title["Data Analyst"]
    assert onsite.remote is False and onsite.country_codes == ["US"]
    assert onsite.raw["salary_text"] is None  # compensation hidden
    assert onsite.employment_type == EmploymentType.internship

    sre = by_title["Site Reliability Engineer"]  # India only as a secondary location
    assert sre.location_texts == ["London", "Pune"] and sre.country_codes == ["GB", "IN"]
    assert sre.employment_type == EmploymentType.contract


@pytest.mark.parametrize("payload", [[], {}, {"jobs": None}, "oops"])
async def test_schema_changed(payload):
    http = FixtureFetcher().add(
        "GET", "https://api.ashbyhq.com/posting-api/job-board/x?includeCompensation=true", payload
    )
    with pytest.raises(SchemaChanged):
        await adapter.list(AshbyConfig(org="x"), INDIA, http)


async def test_blocked_propagates():
    http = FixtureFetcher().fail(
        "GET", "https://api.ashbyhq.com/posting-api/job-board/x?includeCompensation=true", PortalBlocked("HTTP 429")
    )
    with pytest.raises(PortalBlocked):
        await adapter.list(AshbyConfig(org="x"), INDIA, http)


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://jobs.ashbyhq.com/snowflake", True),
        ("https://jobs.ashbyhq.com/snowflake/db1375f0-ea5d-404a-b640-259f94dbc995", True),
        ("https://api.ashbyhq.com/posting-api/job-board/snowflake", True),
        ("https://jobs.lever.co/snowflake", False),
        ("https://ashbyhq.com.evil.io/x", False),
    ],
)
def test_matches_url(url, ok):
    assert AshbyAdapter.matches_url(url) is ok
