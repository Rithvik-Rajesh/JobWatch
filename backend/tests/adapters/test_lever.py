import pytest

from app.discovery.adapters.base import IndiaQuery, PortalBlocked, SchemaChanged, TransientError
from app.discovery.adapters.lever import LeverAdapter
from app.discovery.configs import LeverConfig
from app.models.enums import EmploymentType, WorkArrangement
from tests.discovery_support import FixtureFetcher

COMPANIES = ["meesho", "cred", "spotify"]
INDIA = IndiaQuery()
adapter = LeverAdapter()


async def listed(company: str):
    http = FixtureFetcher().load(f"lever/{company}.json")
    return await adapter.list(LeverConfig(company=company), INDIA, http), http


@pytest.mark.parametrize("company", COMPANIES)
async def test_company_parses(company):
    result, http = await listed(company)
    assert result.complete and result.pages_fetched == 1
    assert http.calls == [("GET", f"https://api.lever.co/v0/postings/{company}?mode=json", None, None)]
    ids = [p.external_id for p in result.postings]
    assert ids and len(set(ids)) == len(ids)
    for p in result.postings:
        assert p.title and p.url.startswith(f"https://jobs.lever.co/{company}/")
        assert p.location_texts and p.has_full_detail
        assert p.posted_at is not None and p.posted_at_precision.value == "day"
        assert len(p.country_codes) <= 1 and all(len(c) == 2 and c.isupper() for c in p.country_codes)


async def test_indian_companies_structured_country_and_raw_locations():
    for company, expected in (("meesho", "Bangalore, Karnataka"), ("cred", "bengaluru")):
        result, _ = await listed(company)
        assert all(p.country_codes == ["IN"] for p in result.postings)
        assert expected in {t for p in result.postings for t in p.location_texts}  # verbatim, case preserved
        assert all(p.remote is False for p in result.postings)  # workplaceType onsite


async def test_spotify_remote_hybrid_and_countries():
    result, _ = await listed("spotify")
    assert {"GB", "US", "SE"} <= {c for p in result.postings for c in p.country_codes}
    remote = [p for p in result.postings if p.raw["workplaceType"] == "remote"]
    hybrid = [p for p in result.postings if p.raw["workplaceType"] == "hybrid"]
    assert remote and all(p.remote is True for p in remote)
    assert hybrid and all(p.remote is False for p in hybrid)
    # bare location like 'New York, NY' carries no explicit eligibility country
    assert all(p.remote_eligibility == [] for p in remote)
    assert any(p.employment_type == EmploymentType.full_time for p in result.postings)


async def test_detail_no_request_and_content():
    result, http = await listed("meesho")
    before = len(http.calls)
    p = result.postings[0]
    d = await adapter.detail(LeverConfig(company="meesho"), p, http)
    assert len(http.calls) == before
    assert d.description_html and d.description_text
    assert d.apply_url == f"{p.url}/apply"
    assert d.work_arrangement == WorkArrangement.onsite


async def test_cred_lists_only_description_still_has_content():
    result, _ = await listed("cred")
    assert all(p.raw["description_html"] for p in result.postings)


async def test_eu_region_url():
    cfg = LeverConfig(company="acme", region="eu")
    assert LeverAdapter.jobs_url(cfg) == "https://api.eu.lever.co/v0/postings/acme?mode=json"
    http = FixtureFetcher().add("GET", "https://api.eu.lever.co/v0/postings/acme?mode=json", [])
    result = await adapter.list(cfg, INDIA, http)
    assert result.postings == [] and result.complete


async def test_remote_text_without_workplace_type():
    job = {
        "id": "a1",
        "text": "Engineer",
        "hostedUrl": "https://jobs.lever.co/x/a1",
        "categories": {"location": "Remote - India", "allLocations": ["Remote - India"]},
        "createdAt": 1700000000000,
        "country": "IN",
    }
    http = FixtureFetcher().add("GET", "https://api.lever.co/v0/postings/x?mode=json", [job])
    p = (await adapter.list(LeverConfig(company="x"), INDIA, http)).postings[0]
    assert p.remote is True and p.remote_eligibility == ["India"] and p.country_codes == ["IN"]
    job2 = {**job, "id": "a2", "categories": {"location": "Remote"}, "country": None}
    http = FixtureFetcher().add("GET", "https://api.lever.co/v0/postings/x?mode=json", [job2])
    p2 = (await adapter.list(LeverConfig(company="x"), INDIA, http)).postings[0]
    assert p2.remote is True and p2.remote_eligibility == [] and p2.country_codes == []


@pytest.mark.parametrize("payload", [{}, {"ok": False}, "oops", None])
async def test_schema_changed(payload):
    http = FixtureFetcher().add("GET", "https://api.lever.co/v0/postings/x?mode=json", payload)
    with pytest.raises(SchemaChanged):
        await adapter.list(LeverConfig(company="x"), INDIA, http)


async def test_malformed_item_makes_run_incomplete():
    http = FixtureFetcher().add("GET", "https://api.lever.co/v0/postings/x?mode=json", [{"id": "z"}])
    result = await adapter.list(LeverConfig(company="x"), INDIA, http)
    assert result.postings == [] and not result.complete and result.warnings


@pytest.mark.parametrize("exc", [PortalBlocked("HTTP 403"), TransientError("timeout")])
async def test_errors_propagate(exc):
    http = FixtureFetcher().fail("GET", "https://api.lever.co/v0/postings/x?mode=json", exc)
    with pytest.raises(type(exc)):
        await adapter.list(LeverConfig(company="x"), INDIA, http)


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://jobs.lever.co/meesho", True),
        ("https://jobs.lever.co/spotify/2a55d6c9-c564-443a-be3e-a3851db5a93b", True),
        ("https://jobs.eu.lever.co/acme", True),
        ("https://api.lever.co/v0/postings/cred?mode=json", True),
        ("https://boards.greenhouse.io/x", False),
        ("https://lever.co.evil.com/x", False),
    ],
)
def test_matches_url(url, ok):
    assert LeverAdapter.matches_url(url) is ok
