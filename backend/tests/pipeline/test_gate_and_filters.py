"""India gate, title filters, anomaly rules, location normalizer additions (pure unit tests)."""

import pytest

from app.discovery import anomaly
from app.discovery.adapters.base import DetailRecord, ListedPosting
from app.discovery.india import evaluate_listing
from app.discovery.normalize.location import parse_locations
from app.discovery.title_filter import passes
from app.models.enums import ScopeMatch
from app.settings.registry import LocationScopeSettings, TitleFilterSettings

CFG = LocationScopeSettings()


def posting(*texts: str, codes=(), eligibility=(), remote=None) -> ListedPosting:
    return ListedPosting(
        external_id="1",
        title="Engineer",
        url="https://x.example/1",
        location_texts=list(texts),
        country_codes=list(codes),
        remote_eligibility=list(eligibility),
        remote=remote,
    )


@pytest.mark.parametrize(
    ("p", "scope", "reason"),
    [
        (posting("Bangalore, IND"), ScopeMatch.in_scope, "city: Bengaluru"),
        (posting("IN-Pune"), ScopeMatch.in_scope, "city: Pune"),
        (posting("IN-Bangalore-MSO"), ScopeMatch.in_scope, "city: Bengaluru"),
        (posting("Bangalore "), ScopeMatch.in_scope, "city: Bengaluru"),
        (posting("Maharashtra"), ScopeMatch.in_scope, "state: Maharashtra"),
        (posting("Indianapolis, IN"), ScopeMatch.out_of_scope, "country US"),
        (posting("Indianapolis, Indiana"), ScopeMatch.out_of_scope, "country US"),
        (posting("Indian Wells, CA"), ScopeMatch.out_of_scope, "country US"),
        (posting("Fort Wayne, IN"), ScopeMatch.out_of_scope, "country US"),
        (posting("Hosur, IN"), ScopeMatch.unknown, "no location"),  # ambiguous: never silently India or US
        (posting("Remote"), ScopeMatch.unknown, "remote: India not listed"),
        (posting("Remote - Remote"), ScopeMatch.unknown, "remote: India not listed"),
        (posting("Anywhere"), ScopeMatch.unknown, "no location"),
        (posting("Remote - India"), ScopeMatch.in_scope, "remote: India listed"),
        (posting("India (Remote)"), ScopeMatch.in_scope, "remote: India listed"),
        (posting("Remote", eligibility=["India"], remote=True), ScopeMatch.in_scope, "remote: India listed"),
        (posting("Remote", eligibility=["United States"], remote=True), ScopeMatch.out_of_scope, "country US"),
        (posting("Austin, TX", "Pune"), ScopeMatch.in_scope, "city: Pune"),  # any location is Indian
        (posting("London", "Berlin"), ScopeMatch.out_of_scope, "country GB"),
        (posting("Remote", codes=["IN"]), ScopeMatch.in_scope, "country IN (structured)"),
        (posting("Pune", codes=["US"]), ScopeMatch.in_scope, "city: Pune"),
        (posting("Somewhere", codes=["US"]), ScopeMatch.out_of_scope, "country US"),
        (posting("2 Locations"), ScopeMatch.unknown, "no location"),
        (posting(), ScopeMatch.unknown, "no location"),
        (posting("India Locations"), ScopeMatch.in_scope, "country IN (text)"),
        (posting("Platforms"), ScopeMatch.unknown, "no location"),
    ],
)
def test_india_gate(p, scope, reason):
    decision = evaluate_listing(p, None, CFG)
    assert (decision.scope, decision.reason) == (scope, reason)


def test_detail_evidence_upgrades_unknown_and_include_remote_anywhere():
    p = posting("2 Locations")
    assert evaluate_listing(p, None, CFG).scope == ScopeMatch.unknown
    d = DetailRecord(location_texts=["Hyderabad, India", "Dublin, Ireland"], country_codes=["IE"])
    assert evaluate_listing(p, d, CFG).reason == "city: Hyderabad"
    anywhere = LocationScopeSettings(include_remote_anywhere=True)
    assert evaluate_listing(posting("Remote", remote=True), None, anywhere).scope == ScopeMatch.in_scope


def test_no_substring_match_on_india():
    for text in ("Indiana", "Indianapolis", "Indian Wells", "Indiana, USA"):
        assert evaluate_listing(posting(text), None, CFG).scope != ScopeMatch.in_scope


@pytest.mark.parametrize(
    ("text", "city", "country"),
    [
        ("IN-Pune", "Pune", "IN"),
        ("IN-Bangalore-MSO", "Bengaluru", "IN"),
        ("Bangalore, IND", "Bengaluru", "IN"),
        ("IND-Bangalore", "Bengaluru", "IN"),
        ("Pune, IN", "Pune", "IN"),
        ("Indianapolis, IN", "Indianapolis", "US"),
        ("Hosur, IN", None, None),
        ("Hyderabad, TG, IND", "Hyderabad", "IN"),
    ],
)
def test_location_normalizer_additions(text, city, country):
    (loc,) = parse_locations(text)
    assert (loc.city, loc.country_code) == (city, country)


def test_title_filters():
    f = TitleFilterSettings(include=["engineer", "data scientist"], exclude=["sales", "intern"])
    assert passes("Senior Software Engineer", f)
    assert passes("Staff DATA   Scientist", f)
    assert not passes("Engineering Manager", f)  # whole words only
    assert not passes("Sales Engineer", f)  # exclude wins
    assert not passes("Software Engineer Intern", f)
    assert not passes("Product Designer", f)
    assert passes("Anything at all", TitleFilterSettings())
    assert not passes("Sales Lead", TitleFilterSettings(exclude=["sales"]))
    assert passes("C++ Developer", TitleFilterSettings(include=["c++"]))


def test_anomaly_rules():
    ok = anomaly.check_run(in_scope_count=100, listed=100, missing_fields=0, baseline=100)
    assert not ok.quarantined
    assert anomaly.check_run(in_scope_count=0, listed=0, missing_fields=0, baseline=5).quarantined
    assert not anomaly.check_run(in_scope_count=0, listed=0, missing_fields=0, baseline=None).quarantined
    assert not anomaly.check_run(in_scope_count=0, listed=0, missing_fields=0, baseline=0).quarantined
    assert anomaly.check_run(in_scope_count=40, listed=40, missing_fields=0, baseline=100).quarantined  # <50%, drop 60
    assert not anomaly.check_run(in_scope_count=4, listed=4, missing_fields=0, baseline=9).quarantined  # drop <= 10
    assert not anomaly.check_run(in_scope_count=60, listed=60, missing_fields=0, baseline=100).quarantined
    assert anomaly.check_run(in_scope_count=10, listed=10, missing_fields=3, baseline=10).quarantined  # 30% missing
    assert not anomaly.check_run(in_scope_count=10, listed=10, missing_fields=2, baseline=10).quarantined  # 20%
