import pytest

from app.discovery.adapters.base import IndiaQuery, PortalBlocked, SchemaChanged
from app.discovery.adapters.greenhouse import GreenhouseAdapter, remote_countries
from app.discovery.configs import GreenhouseConfig
from tests.discovery_support import FixtureFetcher

BOARDS = ["zscaler", "stripe", "razorpaysoftwareprivatelimited"]
INDIA = IndiaQuery()
adapter = GreenhouseAdapter()


def cfg(token: str) -> GreenhouseConfig:
    return GreenhouseConfig(board_token=token)


async def listed(token: str):
    http = FixtureFetcher().load(f"greenhouse/{token}.json")
    return await adapter.list(cfg(token), INDIA, http), http


@pytest.mark.parametrize("token", BOARDS)
async def test_board_parses(token):
    result, http = await listed(token)
    assert result.complete and result.pages_fetched == 1
    assert len(http.calls) == 1 and http.calls[0][1].endswith(f"/v1/boards/{token}/jobs?content=true")
    ids = [p.external_id for p in result.postings]
    assert ids and len(set(ids)) == len(ids)
    assert all(i.startswith(f"{token}:") for i in ids)
    assert result.source_total == len(ids)
    for p in result.postings:
        assert p.title and p.url.startswith("http") and p.location_texts
        assert p.country_codes == []  # Greenhouse has no structured country
        assert p.has_full_detail and p.raw["description_html"]
        assert p.posted_at is not None and p.posted_at_precision.value == "day"


async def test_zscaler_location_texts_and_remote_eligibility():
    result, _ = await listed("zscaler")
    by_loc = {}
    for p in result.postings:
        by_loc.setdefault(p.location_texts[0], p)
    assert "Bangalore, IND" in by_loc
    assert by_loc["Bangalore, IND"].remote is None and by_loc["Bangalore, IND"].remote_eligibility == []
    assert by_loc["Remote - India"].remote is True
    assert by_loc["Remote - India"].remote_eligibility == ["India"]
    assert by_loc["Remote - Tamil Nadu, India"].remote_eligibility == ["India"]
    assert by_loc["Remote - Perth, Australia"].remote_eligibility == ["Australia"]
    assert by_loc["Remote - Maryland, USA; Remote - Pennsylvania, USA; Remote - Virginia, USA"].remote_eligibility == [
        "United States"
    ]
    assert by_loc["Bangalore, IND"].requisition_id


async def test_stripe_bare_remote_and_trailing_space_preserved():
    result, _ = await listed("stripe")
    texts = {t for p in result.postings for t in p.location_texts}
    assert "Bangalore " in texts  # trailing space preserved verbatim
    bare = [p for p in result.postings if "SF, NYC, remote" in p.location_texts]
    assert bare and bare[0].remote is True and bare[0].remote_eligibility == []
    multi = [p for p in result.postings if "Chicago, Seattle, NYC, San Francisco, Remote " in p.location_texts]
    assert multi and multi[0].remote_eligibility == []
    # Stripe's placeholder requisition id is not propagated
    assert all(p.requisition_id is None for p in result.postings)
    # offices contribute extra location evidence without duplicating the main string
    blr = next(p for p in result.postings if p.location_texts[0].strip() == "Bangalore")
    assert "India Locations" in blr.location_texts


async def test_razorpay_india_only():
    result, _ = await listed("razorpaysoftwareprivatelimited")
    firsts = {p.location_texts[0] for p in result.postings}
    assert {"Bengaluru", "Mumbai"} <= firsts
    assert all(p.remote is None for p in result.postings)
    assert all(p.requisition_id for p in result.postings)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Remote - India", ["India"]),
        ("Remote, India", ["India"]),
        ("India (Remote)", ["India"]),
        ("Remote in the US", ["United States"]),
        ("US-Remote", ["United States"]),
        ("Remote", []),
        ("Remote - Pune", []),
        ("Remote, CA", []),
        ("Dublin or Germany (Berlin or Remote)", []),
        ("Poland - Remote OR Romania - Remote", ["Poland", "Romania"]),
        ("Bangalore, IND", []),
    ],
)
def test_remote_countries(text, expected):
    assert remote_countries([text]) == expected


async def test_html_unescaped():
    result, _ = await listed("zscaler")
    for p in result.postings:
        html = p.raw["description_html"]
        assert "&lt;" not in html[:200] and "&quot;" not in html[:200]
        assert html.lstrip().startswith("<")


async def test_detail_makes_no_request():
    result, http = await listed("razorpaysoftwareprivatelimited")
    before = len(http.calls)
    p = result.postings[0]
    d = await adapter.detail(cfg("razorpaysoftwareprivatelimited"), p, http)
    assert len(http.calls) == before
    assert d.description_html == p.raw["description_html"] and d.description_text
    assert d.apply_url == p.url and d.requisition_id == p.requisition_id


async def test_total_mismatch_marks_incomplete():
    http = FixtureFetcher().add(
        "GET",
        "https://boards-api.greenhouse.io/v1/boards/x/jobs?content=true",
        {"jobs": [], "meta": {"total": 3}},
    )
    result = await adapter.list(cfg("x"), INDIA, http)
    assert not result.complete and result.warnings


async def test_custom_api_host():
    http = FixtureFetcher().add(
        "GET", "https://boards-api.eu.greenhouse.io/v1/boards/x/jobs?content=true", {"jobs": [], "meta": {"total": 0}}
    )
    result = await adapter.list(GreenhouseConfig(board_token="x", api_host="boards-api.eu.greenhouse.io"), INDIA, http)
    assert result.complete and result.postings == []


@pytest.mark.parametrize("payload", [[], {}, {"jobs": None}, {"jobs": {"a": 1}}, "oops"])
async def test_schema_changed(payload):
    http = FixtureFetcher().add("GET", "https://boards-api.greenhouse.io/v1/boards/x/jobs?content=true", payload)
    with pytest.raises(SchemaChanged):
        await adapter.list(cfg("x"), INDIA, http)


async def test_malformed_item_skipped_and_incomplete():
    http = FixtureFetcher().add(
        "GET",
        "https://boards-api.greenhouse.io/v1/boards/x/jobs?content=true",
        {"jobs": [{"id": 1}, {"id": 2, "title": "T", "absolute_url": "https://x/2", "location": {"name": "Pune"}}]},
    )
    result = await adapter.list(cfg("x"), INDIA, http)
    assert [p.external_id for p in result.postings] == ["x:2"] and not result.complete


async def test_blocked_propagates():
    http = FixtureFetcher().fail(
        "GET", "https://boards-api.greenhouse.io/v1/boards/x/jobs?content=true", PortalBlocked("HTTP 403")
    )
    with pytest.raises(PortalBlocked):
        await adapter.list(cfg("x"), INDIA, http)


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://boards.greenhouse.io/rubrik", True),
        ("https://job-boards.greenhouse.io/zscaler/jobs/5280084007", True),
        ("https://job-boards.eu.greenhouse.io/groww", True),
        ("https://boards-api.greenhouse.io/v1/boards/airbnb/jobs", True),
        ("https://jobs.lever.co/meesho", False),
        ("https://example.com/greenhouse.io/x", False),
    ],
)
def test_matches_url(url, ok):
    assert GreenhouseAdapter.matches_url(url) is ok
