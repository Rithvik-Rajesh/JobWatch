import json
from datetime import UTC

import pytest

from app.discovery.adapters import oracle_hcm as mod
from app.discovery.adapters.base import IndiaQuery, PortalBlocked, RobotsDisallowed, SchemaChanged
from app.discovery.adapters.oracle_hcm import OracleHcmAdapter, _search_params
from app.discovery.configs import OracleHcmConfig
from app.models.enums import DatePrecision
from tests.discovery_support import FIXTURES, FixtureFetcher

INDIA = IndiaQuery()
LIST_URL_TMPL = "https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
DETAIL_URL_TMPL = "https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"

# name -> (host, site number, India locationId)
TENANTS = {
    "dell": ("enterpriseplatform.dell.com", "CX_1001", "300000000471053"),
    "oracle": ("eeho.fa.us2.oraclecloud.com", "CX_45001", "300000000106947"),
    "jpmc": ("jpmc.fa.oraclecloud.com", "CX_1001", "300000000289360"),
}


def cfg(name: str, *, with_location: bool = True) -> OracleHcmConfig:
    host, site, loc = TENANTS[name]
    values = {"host": host, "site_number": site, "language": "en", "india_location_id": loc if with_location else None}
    return OracleHcmConfig(**values)


def recorded(name: str, kind: str) -> dict:
    return json.loads((FIXTURES / "oracle_hcm" / f"{name}_{kind}.json").read_text())["response"]["json"]


def envelope(reqs: list[dict], total: int | None, facet: list | None = "default") -> dict:
    if facet == "default":
        facet = [{"Id": 300000000000001, "Name": "India", "TotalCount": total or 0}]
    env: dict = {"requisitionList": reqs, "TotalJobsCount": total}
    if facet is not None:
        env["locationsFacet"] = facet
    return {"items": [env], "count": 1, "hasMore": False}


def req(i: int, **extra) -> dict:
    return {
        "Id": str(i),
        "Title": f"Job {i}",
        "PostedDate": "2026-11-01",
        "PrimaryLocation": "Pune, Maharashtra, India",
        "PrimaryLocationCountry": "IN",
        "secondaryLocations": [],
        **extra,
    }


def page(config: OracleHcmConfig, offset: int, payload: dict, f: FixtureFetcher) -> None:
    f.add(
        "GET",
        LIST_URL_TMPL.format(host=config.host),
        payload,
        params=_search_params(config, offset, config.india_location_id),
    )


@pytest.mark.parametrize("name", list(TENANTS))
async def test_recorded_tenant_list_parses(name):
    config = cfg(name)
    f = FixtureFetcher().load(f"oracle_hcm/{name}_list.json")
    page(config, 25, envelope([], recorded(name, "list")["items"][0]["TotalJobsCount"]), f)  # terminating empty page
    res = await OracleHcmAdapter().list(config, INDIA, f)

    raw = recorded(name, "list")["items"][0]
    assert len(res.postings) == 25 == len(raw["requisitionList"])
    assert res.complete and res.used_source_india_filter
    assert res.source_total == raw["TotalJobsCount"]
    for p in res.postings:
        assert p.external_id == f"{config.site_number}:{p.requisition_id}"
        assert p.title
        assert p.country_codes == ["IN"]
        assert p.location_texts and p.location_texts[0].endswith("India")
        assert (
            p.url
            == f"https://{config.host}/hcmUI/CandidateExperience/en/sites/{config.site_number}/job/{p.requisition_id}"
        )
        assert p.posted_at is not None and p.posted_at.tzinfo == UTC
        assert p.posted_at_precision == DatePrecision.day
        assert not p.has_full_detail


async def test_dell_remote_and_secondary_locations():
    config = cfg("dell")
    f = FixtureFetcher().load("oracle_hcm/dell_list.json")
    page(config, 25, envelope([], 85), f)
    res = await OracleHcmAdapter().list(config, INDIA, f)
    assert any(p.remote is True for p in res.postings)  # WorkplaceTypeCode ORA_REMOTE
    assert any(p.remote is False for p in res.postings)  # ORA_ON_SITE
    multi = [p for p in res.postings if len(p.location_texts) > 1]
    assert multi and all(p.country_codes == ["IN"] for p in multi)


@pytest.mark.parametrize("name", list(TENANTS))
async def test_recorded_tenant_detail(name):
    config = cfg(name)
    detail_fx = json.loads((FIXTURES / "oracle_hcm" / f"{name}_detail.json").read_text())
    req_id = detail_fx["request"]["params"]["finder"].split('"')[1]
    f = FixtureFetcher().load(f"oracle_hcm/{name}_detail.json")
    listing = next(p for p in _postings_from_fixture(name, config) if p.requisition_id == req_id)
    d = await OracleHcmAdapter().detail(config, listing, f)
    assert d.description_html and d.description_text and len(d.description_text) > 50
    assert d.apply_url == listing.url
    assert d.requisition_id == req_id
    assert d.country_codes == ["IN"]
    assert d.location_texts


def _postings_from_fixture(name: str, config: OracleHcmConfig):
    adapter = OracleHcmAdapter()
    return [adapter._to_posting(config, r) for r in recorded(name, "list")["items"][0]["requisitionList"]]


async def test_india_location_id_resolved_from_facet_when_unset():
    config = cfg("dell", with_location=False)
    resolved_cfg = cfg("dell")  # the recorded request carries locationId
    first = recorded("dell", "list")
    unfiltered = envelope([req(1)], 999, facet=first["items"][0]["locationsFacet"])
    f = FixtureFetcher().load("oracle_hcm/dell_list.json")
    f.add("GET", LIST_URL_TMPL.format(host=config.host), unfiltered, params=_search_params(config, 0, None))
    page(resolved_cfg, 25, envelope([], 85), f)

    res = await OracleHcmAdapter().list(config, INDIA, f)

    assert config.india_location_id == "300000000471053"  # cached on the config
    assert len(res.postings) == 25  # postings come from the filtered query, not the unfiltered probe
    assert "locationId" not in f.calls[0][2]["finder"] and "locationId=300000000471053" in f.calls[1][2]["finder"]


async def test_india_location_id_unresolvable_returns_empty_with_warning():
    config = cfg("oracle", with_location=False)
    f = FixtureFetcher().add(
        "GET",
        LIST_URL_TMPL.format(host=config.host),
        envelope([], 10, facet=[{"Id": 1, "Name": "United States", "TotalCount": 10}]),
        params=_search_params(config, 0, None),
    )
    res = await OracleHcmAdapter().list(config, INDIA, f)
    assert res.postings == [] and res.complete
    assert res.warnings[0]["code"] == "india_location_not_found"
    assert config.india_location_id is None


async def test_missing_facet_when_resolving_is_schema_change():
    config = cfg("oracle", with_location=False)
    f = FixtureFetcher().add(
        "GET",
        LIST_URL_TMPL.format(host=config.host),
        envelope([req(1)], 1, facet=None),
        params=_search_params(config, 0, None),
    )
    with pytest.raises(SchemaChanged):
        await OracleHcmAdapter().list(config, INDIA, f)


async def test_pagination_stops_at_total_jobs_count():
    config = cfg("jpmc")
    f = FixtureFetcher()
    page(config, 0, envelope([req(i) for i in range(25)], 30), f)
    page(config, 25, envelope([req(i) for i in range(25, 30)], 30), f)
    res = await OracleHcmAdapter().list(config, INDIA, f)
    assert len(res.postings) == 30 and res.complete and res.pages_fetched == 2 and len(f.calls) == 2
    assert res.source_total == 30


async def test_pagination_stops_on_empty_page_without_total():
    config = cfg("jpmc")
    f = FixtureFetcher()
    page(config, 0, envelope([req(1), req(2)], None), f)
    page(config, 25, envelope([], None), f)
    res = await OracleHcmAdapter().list(config, INDIA, f)
    assert [p.requisition_id for p in res.postings] == ["1", "2"] and res.complete and res.pages_fetched == 2


async def test_page_cap_marks_incomplete(monkeypatch):
    config = cfg("jpmc")
    config = config.model_copy(update={"max_pages": 3})  # per-site cap (dry runs use it)
    f = FixtureFetcher()
    for n in range(3):
        page(config, n * 25, envelope([req(i) for i in range(n * 25, n * 25 + 25)], 10_000), f)
    res = await OracleHcmAdapter().list(config, INDIA, f)
    assert not res.complete and len(res.postings) == 75
    assert res.warnings[0]["code"] == "page_cap"


async def test_duplicate_ids_are_deduplicated():
    config = cfg("jpmc")
    f = FixtureFetcher()
    page(config, 0, envelope([req(1), req(1)], 2), f)
    res = await OracleHcmAdapter().list(config, INDIA, f)
    assert len(res.postings) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"items": []},
        {"items": [{"TotalJobsCount": 1}]},
        {"items": ["x"]},
        {"items": [{"requisitionList": "no"}]},
        [],
    ],
)
async def test_malformed_list_payload_raises_schema_changed(payload):
    config = cfg("jpmc")
    f = FixtureFetcher()
    page(config, 0, payload, f)
    with pytest.raises(SchemaChanged):
        await OracleHcmAdapter().list(config, INDIA, f)


@pytest.mark.parametrize("bad", [{"Title": "x"}, {"Id": "1"}, {"Id": "", "Title": "x"}])
async def test_requisition_missing_required_fields_raises(bad):
    config = cfg("jpmc")
    f = FixtureFetcher()
    page(config, 0, envelope([bad], 1), f)
    with pytest.raises(SchemaChanged):
        await OracleHcmAdapter().list(config, INDIA, f)


@pytest.mark.parametrize("payload", [{}, {"items": []}, {"items": ["x"]}])
async def test_malformed_detail_raises_schema_changed(payload):
    config = cfg("jpmc")
    posting = OracleHcmAdapter()._to_posting(config, req(7))
    f = FixtureFetcher().add(
        "GET",
        DETAIL_URL_TMPL.format(host=config.host),
        payload,
        params={"expand": "all", "onlyData": "true", "finder": 'ById;Id="7",siteNumber=CX_1001'},
    )
    with pytest.raises(SchemaChanged):
        await OracleHcmAdapter().detail(config, posting, f)


@pytest.mark.parametrize("exc", [PortalBlocked("HTTP 403"), RobotsDisallowed("robots")])
async def test_fetcher_errors_propagate(exc):
    config = cfg("jpmc")
    f = FixtureFetcher().fail(
        "GET", LIST_URL_TMPL.format(host=config.host), exc, params=_search_params(config, 0, config.india_location_id)
    )
    with pytest.raises(type(exc)):
        await OracleHcmAdapter().list(config, INDIA, f)


async def test_blocked_fixture_status_propagates_as_portal_blocked():
    config = cfg("jpmc")
    f = FixtureFetcher().fail(
        "GET",
        DETAIL_URL_TMPL.format(host=config.host),
        PortalBlocked("403"),
        params={"expand": "all", "onlyData": "true", "finder": 'ById;Id="7",siteNumber=CX_1001'},
    )
    with pytest.raises(PortalBlocked):
        await OracleHcmAdapter().detail(config, OracleHcmAdapter()._to_posting(config, req(7)), f)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001", True),
        ("https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_45001/jobs", True),
        ("https://enterpriseplatform.dell.com/hcmUI/CandidateExperience/en/sites/careers", True),
        ("https://jpmc.fa.oraclecloud.com/other", False),
        ("https://boards.greenhouse.io/x", False),
        ("not a url", False),
    ],
)
def test_matches_url(url, expected):
    assert OracleHcmAdapter.matches_url(url) is expected


def test_config_model_and_registry():
    from app.discovery.adapters import get_adapter

    assert OracleHcmAdapter.Config is OracleHcmConfig
    assert isinstance(get_adapter("oracle_hcm"), OracleHcmAdapter)
