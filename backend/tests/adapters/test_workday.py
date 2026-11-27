"""Workday adapter tests: recorded fixtures (NVIDIA, Airbus, Barclays, Accenture) plus synthetic pages built
from the recorded item shape. Pure unit tests; no network, no DB."""

import copy
import json
import re

import pytest

from app.discovery.adapters.base import ConfigError, IndiaQuery, ListedPosting, PortalBlocked, SchemaChanged
from app.discovery.adapters.workday import WorkdayAdapter
from app.discovery.configs import WorkdayConfig
from app.models.enums import DatePrecision, EmploymentType
from tests.discovery_support import FIXTURES, FixtureFetcher

INDIA = IndiaQuery()
ADAPTER = WorkdayAdapter()
INDIA_ID = "c4f78be1a8f14da0ab49ce1162348a5e"
NVIDIA_URL = "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"
NVIDIA_API = "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite"
NVIDIA_INDIA_ID = "2fcb99c455831013ea52b82135ba3266"
BARCLAYS_URL = "https://barclays.wd3.myworkdayjobs.com/External_Career_Site_Barclays"
BARCLAYS_API = "https://barclays.wd3.myworkdayjobs.com/wday/cxs/barclays/External_Career_Site_Barclays"
AIRBUS_URL = "https://ag.wd3.myworkdayjobs.com/Airbus"
AIRBUS_API = "https://ag.wd3.myworkdayjobs.com/wday/cxs/ag/Airbus"
ACCENTURE_URL = "https://accenture.wd103.myworkdayjobs.com/AccentureCareers"
ACCENTURE_API = "https://accenture.wd103.myworkdayjobs.com/wday/cxs/accenture/AccentureCareers"


def body(applied: dict, offset: int = 0) -> dict:
    return {"appliedFacets": applied, "limit": 20, "offset": offset, "searchText": ""}


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / "workday" / f"{name}.json").read_text())["response"]["json"]


def page(items: list[dict], total: int = 0, facets: list | None = None) -> dict:
    return {"total": total, "jobPostings": items, "facets": facets or [], "userAuthenticated": False}


def synth_items(start: int, n: int, tag: str = "Pune") -> list[dict]:
    """Items shaped like the recorded ones."""
    return [
        {
            "title": f"Engineer {i}",
            "externalPath": f"/job/India-{tag}/Engineer-{i}_JR{9000 + i}",
            "locationsText": f"India, {tag}",
            "postedOn": "Posted 3 Days Ago",
            "bulletFields": [f"JR{9000 + i}"],
        }
        for i in range(start, start + n)
    ]


def cfg(url: str, **kw) -> WorkdayConfig:
    return WorkdayConfig(url=url, **kw)


def search_texts(f: FixtureFetcher) -> list[str]:
    return [c[3]["searchText"] for c in f.calls if c[0] == "POST"]


def assert_no_india_search(f: FixtureFetcher) -> None:
    assert all(t == "" for t in search_texts(f)), "searchText must stay empty (India matches Indiana)"


def nvidia_fetcher() -> FixtureFetcher:
    f = FixtureFetcher().load("workday/nvidia_list_unfiltered.json", "workday/nvidia_list_india.json")
    return f.add("POST", f"{NVIDIA_API}/jobs", page([]), json_body=body({"locationHierarchy1": [NVIDIA_INDIA_ID]}, 20))


# ───────────────────────────── recorded tenants ─────────────────────────────


async def test_nvidia_resolves_nested_country_facet_and_applies_it():
    f = nvidia_fetcher()
    config = cfg(NVIDIA_URL)  # auto
    res = await ADAPTER.list(config, INDIA, f)

    assert config.facet_cache == {"locationHierarchy1": [NVIDIA_INDIA_ID]}
    assert res.used_source_india_filter and res.complete
    assert res.source_total == 241
    assert len(res.postings) == 20
    # unfiltered probe, filtered page 1, then the empty page that ends pagination
    assert [c[3]["appliedFacets"] for c in f.calls] == [
        {},
        {"locationHierarchy1": [NVIDIA_INDIA_ID]},
        {"locationHierarchy1": [NVIDIA_INDIA_ID]},
    ]
    assert_no_india_search(f)

    p = res.postings[0]
    assert (
        p.external_id
        == "nvidia/NVIDIAExternalCareerSite:/job/India-Bengaluru/ASIC-Design-Engineer--GPU-Core-Pipeline-IP_JR2029760"
    )
    assert p.url == f"{NVIDIA_URL}{p.external_id.split(':', 1)[1]}"
    assert p.location_texts == ["India, Bengaluru"]
    assert p.posted_at_raw and p.fingerprint
    assert all(re.fullmatch(r"JR\d+", x.requisition_id or "") for x in res.postings)


async def test_ids_and_fingerprints_are_stable_across_runs():
    a = await ADAPTER.list(cfg(NVIDIA_URL), INDIA, nvidia_fetcher())
    b = await ADAPTER.list(cfg(NVIDIA_URL), INDIA, nvidia_fetcher())
    assert [p.external_id for p in a.postings] == [p.external_id for p in b.postings]
    assert [p.fingerprint for p in a.postings] == [p.fingerprint for p in b.postings]
    assert len({p.external_id for p in a.postings}) == len(a.postings)


async def test_facet_cache_skips_the_discovery_request():
    f = nvidia_fetcher()
    config = cfg(NVIDIA_URL, india_strategy="facet", facet_cache={"locationHierarchy1": [NVIDIA_INDIA_ID]})
    res = await ADAPTER.list(config, INDIA, f)
    assert len(res.postings) == 20
    assert all(c[3]["appliedFacets"] for c in f.calls)  # never an unfiltered request


async def test_nvidia_multi_location_posting_gets_locations_from_detail():
    res = await ADAPTER.list(cfg(NVIDIA_URL), INDIA, nvidia_fetcher())
    multi = next(p for p in res.postings if p.requisition_id == "JR2026183")
    assert multi.location_texts == []  # "2 Locations" is not a location

    f = FixtureFetcher().load("workday/nvidia_detail.json")
    d = await ADAPTER.detail(cfg(NVIDIA_URL), multi, f)
    assert d.location_texts == ["India, Bengaluru", "India, Hyderabad"]
    assert d.country_codes == ["IN"]  # structured only
    assert d.requisition_id == "JR2026183"
    assert d.employment_type == EmploymentType.full_time
    assert d.posted_at is not None and d.posted_at.date().isoformat() == "2026-11-11"
    assert d.posted_at_precision == DatePrecision.day
    assert d.apply_url == f"{NVIDIA_URL}/job/India-Bengaluru/Senior-Physical-Design-Engineer_JR2026183"
    assert d.description_html and d.description_html.startswith("<p>")
    assert f.calls[0][1] == f"{NVIDIA_API}/job/India-Bengaluru/Senior-Physical-Design-Engineer_JR2026183"


async def test_airbus_facet_param_override_resolves_group_value():
    f = FixtureFetcher().load("workday/airbus_list_unfiltered.json", "workday/airbus_list_india.json")
    f.add("POST", f"{AIRBUS_API}/jobs", page([]), json_body=body({"locationCountry": [INDIA_ID]}, 20))
    config = cfg(AIRBUS_URL, india_strategy="facet", facet_param="locationCountry")
    res = await ADAPTER.list(config, INDIA, f)
    assert config.facet_cache == {"locationCountry": [INDIA_ID]}
    assert res.source_total == 213 and len(res.postings) == 20 and res.used_source_india_filter
    assert {p.requisition_id for p in res.postings} >= {"JR10454601", "JR10456296"}  # bullet id, not path "-1" suffix
    assert_no_india_search(f)

    d = await ADAPTER.detail(config, res.postings[4], FixtureFetcher().load("workday/airbus_detail.json"))
    assert d.country_codes == ["IN"] and d.location_texts == ["Bangalore Area"]
    assert d.requisition_id == "JR10455480"


async def test_facet_param_without_india_value_yields_empty_complete_result():
    unfiltered = fixture("airbus_list_unfiltered")
    for g in unfiltered["facets"][-1]["values"]:
        if g["facetParameter"] == "locationCountry":
            g["values"] = [v for v in g["values"] if v["descriptor"] != "India"]
    f = FixtureFetcher().add("POST", f"{AIRBUS_API}/jobs", unfiltered, json_body=body({}))
    res = await ADAPTER.list(cfg(AIRBUS_URL, india_strategy="facet", facet_param="locationCountry"), INDIA, f)
    assert res.postings == [] and res.complete


async def test_barclays_location_text_enumerates_everything_without_facets():
    f = FixtureFetcher().load("workday/barclays_list.json")
    f.add("POST", f"{BARCLAYS_API}/jobs", page([]), json_body=body({}, 20))
    res = await ADAPTER.list(cfg(BARCLAYS_URL, india_strategy="location_text"), INDIA, f)

    assert res.complete and not res.used_source_india_filter
    assert res.source_total == 622 and len(res.postings) == 20
    assert all(c[3]["appliedFacets"] == {} for c in f.calls)
    assert_no_india_search(f)
    first = res.postings[0]
    assert first.location_texts == ["Canary Wharf, 1 Churchill Place"]
    assert first.requisition_id == "JR-0000137565"
    multi = next(p for p in res.postings if p.requisition_id == "JR-0000087621")
    assert multi.location_texts == []

    d = await ADAPTER.detail(cfg(BARCLAYS_URL), multi, FixtureFetcher().load("workday/barclays_detail.json"))
    assert d.location_texts == ["Dublin, Molesworth Street", "Paris, 52 avenue Hoche"]
    assert d.country_codes == ["IE"]
    assert d.requisition_id == "JR-0000087621"


async def test_auto_falls_back_to_location_text_when_no_country_facet():
    f = FixtureFetcher().load("workday/barclays_list.json")  # facets are office-level: no "India" value
    f.add("POST", f"{BARCLAYS_API}/jobs", page(synth_items(0, 3)), json_body=body({}, 20))
    f.add("POST", f"{BARCLAYS_API}/jobs", page([]), json_body=body({}, 23))
    config = cfg(BARCLAYS_URL, india_strategy="auto")
    res = await ADAPTER.list(config, INDIA, f)

    assert not res.used_source_india_filter and res.complete
    assert config.facet_cache == {}
    assert len(res.postings) == 23  # unfiltered page 1 was reused, not re-requested
    assert [c[3]["offset"] for c in f.calls] == [0, 20, 23]
    assert any(w["code"] == "no_country_facet" for w in res.warnings)
    assert_no_india_search(f)


async def test_facet_strategy_without_country_facet_is_a_config_error():
    f = FixtureFetcher().load("workday/barclays_list.json")
    with pytest.raises(ConfigError):
        await ADAPTER.list(cfg(BARCLAYS_URL, india_strategy="facet"), INDIA, f)


# ───────────────────────────── pagination ─────────────────────────────


def paged_fetcher(pages: list[list[dict]], applied: dict) -> FixtureFetcher:
    f = FixtureFetcher()
    offset = 0
    for i, items in enumerate(pages):
        f.add("POST", f"{NVIDIA_API}/jobs", page(items, total=45 if i == 0 else 0), json_body=body(applied, offset))
        offset += len(items)
    return f.add("POST", f"{NVIDIA_API}/jobs", page([]), json_body=body(applied, offset))


async def test_pagination_runs_until_an_empty_page_and_dedupes():
    items = synth_items(0, 45)
    pages = [items[:20], [*items[20:40], items[3]], items[40:]]  # page 2 repeats a posting from page 1
    f = paged_fetcher(pages, {})
    res = await ADAPTER.list(cfg(NVIDIA_URL, india_strategy="location_text"), INDIA, f)
    assert res.complete and res.pages_fetched == 4
    assert len(res.postings) == 45
    assert len({p.external_id for p in res.postings}) == 45
    assert [c[3]["offset"] for c in f.calls] == [0, 20, 41, 46]  # offset follows items actually returned
    assert res.source_total == 45


async def test_max_pages_cap_marks_listing_incomplete():
    items = synth_items(0, 60)
    f = paged_fetcher([items[:20], items[20:40], items[40:]], {})
    res = await ADAPTER.list(cfg(NVIDIA_URL, india_strategy="location_text", max_pages=2), INDIA, f)
    assert not res.complete and res.pages_fetched == 2
    assert len(res.postings) == 40
    assert any(w["code"] == "max_pages" for w in res.warnings)


# ───────────────────────────── the 2000 cap ─────────────────────────────


def accenture_fetcher(*, with_facets: bool = True, big_city: str | None = None) -> tuple[FixtureFetcher, list[dict]]:
    first = fixture("accenture_list_india")
    assert first["total"] == 2000
    cities = next(g for g in first["facets"][3]["values"] if g["facetParameter"] == "locations")["values"]
    if not with_facets:
        first["facets"] = []
    f = FixtureFetcher().add("POST", f"{ACCENTURE_API}/jobs", first, json_body=body({"locationCountry": [INDIA_ID]}))
    f.add("POST", f"{ACCENTURE_API}/jobs", page([]), json_body=body({"locationCountry": [INDIA_ID]}, 20))
    for n, city in enumerate(cities):
        applied = {"locationCountry": [INDIA_ID], "locations": [city["id"]]}
        items = synth_items(n * 10, 2, tag=city["descriptor"].replace(" ", "-"))
        total = 2000 if city["descriptor"] == big_city else 2
        f.add("POST", f"{ACCENTURE_API}/jobs", page(items, total=total), json_body=body(applied))
        f.add("POST", f"{ACCENTURE_API}/jobs", page([]), json_body=body(applied, 2))
    return f, cities


async def test_accenture_capped_india_result_is_split_by_city_facet():
    f, cities = accenture_fetcher()
    config = cfg(ACCENTURE_URL, facet_param="locationCountry", facet_cache={"locationCountry": [INDIA_ID]})
    res = await ADAPTER.list(config, INDIA, f)  # split facet auto-detected ("locations" / City)

    assert res.complete and res.used_source_india_filter
    assert res.source_total == 2000
    assert len(res.postings) == 20 + 2 * len(cities)  # first capped page + every city, deduped union
    assert any(c[3]["appliedFacets"].get("locations") for c in f.calls)
    assert_no_india_search(f)


async def test_explicit_split_facet_param_is_honoured():
    f, cities = accenture_fetcher()
    config = cfg(ACCENTURE_URL, facet_cache={"locationCountry": [INDIA_ID]}, split_facet_param="locations")
    res = await ADAPTER.list(config, INDIA, f)
    assert res.complete and len(res.postings) == 20 + 2 * len(cities)


async def test_split_value_that_is_still_capped_is_incomplete():
    f, _ = accenture_fetcher(big_city="Bengaluru")
    res = await ADAPTER.list(cfg(ACCENTURE_URL, facet_cache={"locationCountry": [INDIA_ID]}), INDIA, f)
    assert not res.complete
    assert any(w["code"] == "capped_after_split" and w["value"] == "Bengaluru" for w in res.warnings)


async def test_capped_result_without_split_facet_is_incomplete():
    f, _ = accenture_fetcher(with_facets=False)
    res = await ADAPTER.list(cfg(ACCENTURE_URL, facet_cache={"locationCountry": [INDIA_ID]}), INDIA, f)
    assert not res.complete and len(res.postings) == 20
    assert any(w["code"] == "capped_unsplittable" for w in res.warnings)


# ───────────────────────────── errors ─────────────────────────────


async def test_portal_blocked_and_config_error_propagate():
    config = cfg(NVIDIA_URL, india_strategy="location_text")
    blocked = FixtureFetcher().fail("POST", f"{NVIDIA_API}/jobs", PortalBlocked("HTTP 403"), json_body=body({}))
    with pytest.raises(PortalBlocked):
        await ADAPTER.list(config, INDIA, blocked)
    wrong = FixtureFetcher().fail("POST", f"{NVIDIA_API}/jobs", ConfigError("HTTP 422"), json_body=body({}))
    with pytest.raises(ConfigError):
        await ADAPTER.list(config, INDIA, wrong)

    p = ListedPosting(external_id="nvidia/NVIDIAExternalCareerSite:/job/x/y_JR1", title="t", url="u")
    f = FixtureFetcher().fail("GET", f"{NVIDIA_API}/job/x/y_JR1", PortalBlocked("HTTP 429"))
    with pytest.raises(PortalBlocked):
        await ADAPTER.detail(config, p, f)


async def test_blocked_fixture_replays_as_portal_blocked(tmp_path):
    f = FixtureFetcher().fail("POST", f"{NVIDIA_API}/jobs", PortalBlocked("HTTP 403"), json_body=body({}))
    with pytest.raises(PortalBlocked):
        await ADAPTER.list(cfg(NVIDIA_URL, india_strategy="location_text"), INDIA, f)


async def test_malformed_list_response_raises_schema_changed():
    f = FixtureFetcher().add("POST", f"{NVIDIA_API}/jobs", {"total": 3, "oops": []}, json_body=body({}))
    with pytest.raises(SchemaChanged):
        await ADAPTER.list(cfg(NVIDIA_URL, india_strategy="location_text"), INDIA, f)


@pytest.mark.parametrize("mutate", ["no_info", "no_title", "not_dict"])
async def test_malformed_detail_raises_schema_changed(mutate):
    data = fixture("nvidia_detail")
    if mutate == "no_info":
        del data["jobPostingInfo"]
    elif mutate == "no_title":
        del data["jobPostingInfo"]["title"]
    else:
        data = ["unexpected"]
    p = ListedPosting(external_id="nvidia/NVIDIAExternalCareerSite:/job/x/y_JR1", title="t", url="u")
    f = FixtureFetcher().add("GET", f"{NVIDIA_API}/job/x/y_JR1", data)
    with pytest.raises(SchemaChanged):
        await ADAPTER.detail(cfg(NVIDIA_URL), p, f)


async def test_detail_tolerates_missing_optional_fields():
    data = copy.deepcopy(fixture("nvidia_detail"))
    info = data["jobPostingInfo"]
    for k in ("additionalLocations", "startDate", "timeType", "externalUrl", "jobRequisitionLocation", "country"):
        info.pop(k, None)
    info["remoteType"] = "Remote"
    p = ListedPosting(external_id="nvidia/NVIDIAExternalCareerSite:/job/x/y_JR1", title="t", url="u")
    d = await ADAPTER.detail(cfg(NVIDIA_URL), p, FixtureFetcher().add("GET", f"{NVIDIA_API}/job/x/y_JR1", data))
    assert d.location_texts == ["India, Bengaluru"]
    assert d.country_codes is None and d.posted_at is None and d.posted_at_precision is None
    assert d.remote is True and d.work_arrangement is not None


# ───────────────────────────── misc ─────────────────────────────


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (NVIDIA_URL, True),
        ("https://ag.wd3.myworkdayjobs.com/en-US/Airbus/job/x", True),
        ("http://ACCENTURE.wd103.myworkdayjobs.com", True),
        ("https://example.com/careers", False),
        ("https://myworkdayjobs.com/foo", False),
        ("https://evil.com/?u=nvidia.wd5.myworkdayjobs.com", False),
    ],
)
def test_matches_url(url, expected):
    assert WorkdayAdapter.matches_url(url) is expected


def test_adapter_identity_and_registry():
    from app.discovery.adapters import get_adapter

    assert (WorkdayAdapter.platform, WorkdayAdapter.version, WorkdayAdapter.Config) == ("workday", "1", WorkdayConfig)
    assert isinstance(get_adapter("workday"), WorkdayAdapter)


def test_fingerprint_ignores_relative_posted_text():
    from app.discovery.adapters.workday import _fingerprint

    item = synth_items(0, 1)[0]
    later = {**item, "postedOn": "Posted 4 Days Ago"}
    assert _fingerprint(item) == _fingerprint(later)
    assert _fingerprint(item) != _fingerprint({**item, "locationsText": "India, Delhi"})
