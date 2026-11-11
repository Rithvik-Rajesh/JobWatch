import json
from datetime import UTC

import pytest

from app.discovery.adapters import eightfold as mod
from app.discovery.adapters.base import IndiaQuery, PortalBlocked, RobotsDisallowed, SchemaChanged
from app.discovery.adapters.eightfold import EightfoldAdapter
from app.discovery.configs import EightfoldConfig
from tests.discovery_support import FIXTURES, FixtureFetcher

INDIA = IndiaQuery()

# name -> (host, domain, variant)
TENANTS = {
    "microsoft": ("apply.careers.microsoft.com", "microsoft.com", "pcsx"),
    "qualcomm": ("careers.qualcomm.com", "qualcomm.com", "pcsx"),
    "netapp": ("netapp.eightfold.ai", "netapp.com", "apply_v2"),
}


def cfg(name: str) -> EightfoldConfig:
    host, domain, variant = TENANTS[name]
    return EightfoldConfig(host=host, domain=domain, variant=variant)


def url_for(config: EightfoldConfig) -> str:
    path = "/api/pcsx/search" if config.variant == "pcsx" else "/api/apply/v2/jobs"
    return f"https://{config.host}{path}"


def params_for(config: EightfoldConfig, start: int) -> dict:
    p = {"domain": config.domain}
    if config.variant == "pcsx":
        p["query"] = ""
    return {**p, "location": "India", "start": str(start), "num": "10"}


def body(config: EightfoldConfig, positions: list, count: int | None) -> dict:
    inner = {"positions": positions} | ({"count": count} if count is not None else {})
    return {"status": 200, "data": inner} if config.variant == "pcsx" else inner


def pos(i: int, **extra) -> dict:
    return {
        "id": 1000 + i,
        "name": f"Role {i}",
        "locations": ["Pune, India"],
        "postedTs": 1762800000,
        "t_create": 1762800000,
        **extra,
    }


def add(f: FixtureFetcher, config: EightfoldConfig, start: int, payload) -> None:
    f.add("GET", url_for(config), payload, params=params_for(config, start))


def fixture_json(name: str) -> dict:
    return json.loads((FIXTURES / "eightfold" / f"{name}_list.json").read_text())["response"]["json"]


@pytest.mark.parametrize("name", list(TENANTS))
async def test_recorded_tenant_list_parses(name):
    config = cfg(name)
    raw = fixture_json(name)
    positions = (raw["data"] if config.variant == "pcsx" else raw)["positions"]
    total = (raw["data"] if config.variant == "pcsx" else raw)["count"]
    f = FixtureFetcher().load(f"eightfold/{name}_list.json")
    add(f, config, len(positions), body(config, [], total))  # next page empty -> stop
    res = await EightfoldAdapter().list(config, INDIA, f)

    assert len(res.postings) == len(positions) and res.complete and res.source_total == total
    assert res.used_source_india_filter
    for p in res.postings:
        assert p.external_id.isdigit() and p.title
        assert p.url.startswith(f"https://{config.host}/") or p.url.startswith("https://")
        assert p.location_texts  # reported as the source gives them
        assert p.posted_at is not None and p.posted_at.tzinfo == UTC


async def _recorded(name):
    config = cfg(name)
    raw = fixture_json(name)
    inner = raw["data"] if config.variant == "pcsx" else raw
    f = FixtureFetcher().load(f"eightfold/{name}_list.json")
    add(f, config, len(inner["positions"]), body(config, [], inner["count"]))
    return config, (await EightfoldAdapter().list(config, INDIA, f)), inner


async def test_microsoft_pcsx_real_mapping():
    _config, res, raw = await _recorded("microsoft")
    first = res.postings[0]
    assert first.external_id == str(raw["positions"][0]["id"]) == "1970393556991445"
    assert first.requisition_id == "200054309"
    assert first.url == "https://apply.careers.microsoft.com/careers/job/1970393556991445"
    assert first.department == "Cloud Solution Architecture"
    assert first.location_texts == ["India"] and first.country_codes == ["IN"]
    assert first.remote is False and not first.has_full_detail
    # multi-country positions report every structured code; the pipeline's India gate decides
    multi = [p for p in res.postings if len(p.country_codes) > 1]
    assert multi and all("IN" in p.country_codes for p in multi)


async def test_qualcomm_pcsx_real_mapping():
    _config, res, _raw = await _recorded("qualcomm")
    assert all(p.country_codes == ["IN"] for p in res.postings)  # parsed from "Hyderabad, TS, IN"
    assert all(p.location_texts[0].endswith("India") for p in res.postings)
    assert res.postings[0].url == f"https://careers.qualcomm.com/careers/job/{res.postings[0].external_id}"


async def test_netapp_apply_v2_real_mapping_and_listing_only_detail():
    config, res, _raw = await _recorded("netapp")
    first = res.postings[0]
    assert first.external_id == "43057794" and first.requisition_id == "135670"
    assert first.url == "https://netapp.eightfold.ai/careers/job/43057794"
    assert first.location_texts == ["Bangalore, India Office (BANGALORE)"]
    assert first.country_codes == []  # apply/v2 has no structured country
    assert first.remote is False and first.department == "IC 4"
    assert not first.has_full_detail  # job_description is empty in the listing
    f = FixtureFetcher()
    d = await EightfoldAdapter().detail(config, first, f)  # no network for detail
    assert f.calls == [] and d.description_html is None and d.apply_url == first.url


async def test_v2_listing_with_description_and_remote_option():
    config = cfg("netapp")
    p = {
        "id": 5,
        "name": "X",
        "locations": ["Remote, India"],
        "t_create": 1762800000,
        "work_location_option": "remote",
        "job_description": "<p>Build <b>storage</b> software.</p>",
        "canonicalPositionUrl": "https://netapp.eightfold.ai/careers/job/5",
    }
    f = FixtureFetcher()
    add(f, config, 0, body(config, [p], 1))
    res = await EightfoldAdapter().list(config, INDIA, f)
    assert res.postings[0].remote is True and res.postings[0].has_full_detail
    d = await EightfoldAdapter().detail(config, res.postings[0], FixtureFetcher())
    assert "storage" in d.description_text


async def test_missing_position_url_falls_back_to_careers_path():
    config = cfg("microsoft")
    f = FixtureFetcher()
    add(f, config, 0, body(config, [pos(1)], 1))
    res = await EightfoldAdapter().list(config, INDIA, f)
    assert res.postings[0].url == "https://apply.careers.microsoft.com/careers/job/1001?domain=microsoft.com"


@pytest.mark.parametrize("name", ["microsoft", "netapp"])
async def test_pagination_uses_server_page_size_until_count(name):
    config = cfg(name)
    f = FixtureFetcher()
    add(f, config, 0, body(config, [pos(i) for i in range(4)], 7))  # server returns 4, not the 10 asked for
    add(f, config, 4, body(config, [pos(i) for i in range(4, 7)], 7))
    res = await EightfoldAdapter().list(config, INDIA, f)
    assert len(res.postings) == 7 and res.complete and res.pages_fetched == 2 and len(f.calls) == 2


async def test_pagination_stops_on_empty_page_without_count():
    config = cfg("qualcomm")
    f = FixtureFetcher()
    add(f, config, 0, body(config, [pos(1)], None))
    add(f, config, 1, body(config, [], None))
    res = await EightfoldAdapter().list(config, INDIA, f)
    assert len(res.postings) == 1 and res.complete and res.pages_fetched == 2


async def test_page_cap_marks_incomplete(monkeypatch):
    config = cfg("microsoft")
    config = config.model_copy(update={"max_pages": 3})  # per-site cap (dry runs use it)
    f = FixtureFetcher()
    for n in range(3):
        add(f, config, n * 2, body(config, [pos(n * 2), pos(n * 2 + 1)], 10_000))
    res = await EightfoldAdapter().list(config, INDIA, f)
    assert not res.complete and len(res.postings) == 6 and res.warnings[0]["code"] == "page_cap"


async def test_timestamps_in_milliseconds_and_missing_are_handled():
    config = cfg("microsoft")
    f = FixtureFetcher()
    add(f, config, 0, body(config, [pos(1, postedTs=1762800000000), pos(2, postedTs=None)], 2))
    res = await EightfoldAdapter().list(config, INDIA, f)
    assert res.postings[0].posted_at.year == 2025 and res.postings[1].posted_at is None


@pytest.mark.parametrize("name", ["microsoft", "netapp"])
@pytest.mark.parametrize("payload", [{}, [], {"data": {}}, {"positions": "x"}, {"data": {"positions": None}}])
async def test_malformed_payload_raises_schema_changed(name, payload):
    config = cfg(name)
    f = FixtureFetcher()
    add(f, config, 0, payload)
    with pytest.raises(SchemaChanged):
        await EightfoldAdapter().list(config, INDIA, f)


@pytest.mark.parametrize("bad", [{"name": "x"}, {"id": 1}, {"id": 1, "name": 5}, "str"])
async def test_malformed_position_raises_schema_changed(bad):
    config = cfg("microsoft")
    f = FixtureFetcher()
    add(f, config, 0, body(config, [bad], 1))
    with pytest.raises(SchemaChanged):
        await EightfoldAdapter().list(config, INDIA, f)


@pytest.mark.parametrize("exc", [PortalBlocked("HTTP 403 PCSX is not enabled"), RobotsDisallowed("robots")])
async def test_fetcher_errors_propagate(exc):
    config = cfg("netapp")
    f = FixtureFetcher().fail("GET", url_for(config), exc, params=params_for(config, 0))
    with pytest.raises(type(exc)):
        await EightfoldAdapter().list(config, INDIA, f)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://netapp.eightfold.ai/careers?domain=netapp.com", True),
        ("https://app.eightfold.ai/careers?start=0", True),
        ("https://apply.careers.microsoft.com/careers?location=India", True),
        ("https://careers.qualcomm.com/careers", False),  # vanity host: detected via config, not URL
        ("https://eightfold.ai.evil.com/x", False),
        ("https://example.com/careers", False),
        ("garbage", False),
    ],
)
def test_matches_url(url, expected):
    assert EightfoldAdapter.matches_url(url) is expected


def test_config_model_and_registry():
    from app.discovery.adapters import get_adapter

    assert EightfoldAdapter.Config is EightfoldConfig
    assert isinstance(get_adapter("eightfold"), EightfoldAdapter)
