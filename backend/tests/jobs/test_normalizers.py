"""Table-driven unit tests for the pure normalizers."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.discovery.normalize import (
    country_codes,
    extract_skills,
    html_to_text,
    infer_employment_type,
    infer_work_arrangement,
    normalize_title,
    parse_compensation,
    parse_employment_type,
    parse_experience,
    parse_locations,
    parse_relative_posted,
    parse_work_arrangement,
    sanitize_html,
    text_to_html,
)
from app.models.enums import DatePrecision, EmploymentType, SalaryPeriod, WorkArrangement

NOW = datetime(2026, 11, 11, 10, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("text", "cities", "countries"),
    [
        ("Bengaluru, Karnataka, India", ["Bengaluru"], ["IN"]),
        ("Bangalore", ["Bengaluru"], ["IN"]),
        ("Hyderabad", ["Hyderabad"], ["IN"]),
        ("Gurgaon", ["Gurugram"], ["IN"]),
        ("Gurugram, Haryana", ["Gurugram"], ["IN"]),
        ("New Delhi", ["Delhi"], ["IN"]),
        ("Trivandrum", ["Thiruvananthapuram"], ["IN"]),
        ("Mysore", ["Mysuru"], ["IN"]),
        ("Pune, MH", ["Pune"], ["IN"]),
        ("India, Bengaluru", ["Bengaluru"], ["IN"]),
        ("IND-Bangalore", ["Bengaluru"], ["IN"]),
        ("IND - Chennai", ["Chennai"], ["IN"]),
        ("Karnataka, India", [None], ["IN"]),
        ("India", [None], ["IN"]),
        ("IN", [None], ["IN"]),
        ("Hyderabad; Pune | Chennai", ["Hyderabad", "Pune", "Chennai"], ["IN"]),
        ("Mumbai and Kochi", ["Mumbai", "Kochi"], ["IN"]),
        ("Bengaluru / Hyderabad", ["Bengaluru", "Hyderabad"], ["IN"]),
        ("Noida, Kolkata, Ahmedabad", ["Noida", "Kolkata", "Ahmedabad"], ["IN"]),
        ("Coimbatore; Jaipur; Chandigarh; Indore", ["Coimbatore", "Jaipur", "Chandigarh", "Indore"], ["IN"]),
        ("San Francisco, CA", ["San Francisco"], ["US"]),
        ("USA-CA-San Francisco", ["San Francisco"], ["US"]),
        ("Austin, TX, United States", ["Austin"], ["US"]),
        ("London, United Kingdom", ["London"], ["GB"]),
        ("Toronto, Canada", ["Toronto"], ["CA"]),
        ("Berlin, Germany", ["Berlin"], ["DE"]),
        ("Singapore", [None], ["SG"]),
        ("Dublin, Ireland", ["Dublin"], ["IE"]),
        ("Amsterdam", ["Amsterdam"], ["NL"]),
        ("Warsaw, Poland", ["Warsaw"], ["PL"]),
        ("Tel Aviv, Israel", ["Tel Aviv"], ["IL"]),
        ("Tokyo, Japan", ["Tokyo"], ["JP"]),
        ("Sydney, Australia", ["Sydney"], ["AU"]),
        ("Dubai, UAE", ["Dubai"], ["AE"]),
        ("Remote - India", [None], ["IN"]),
        ("Remote", [None], []),
        ("3 Locations", [], []),
        ("Multiple Locations", [], []),
        ("", [], []),
        ("Bengaluru; London", ["Bengaluru", "London"], ["IN", "GB"]),
    ],
)
def test_parse_locations(text, cities, countries):
    locs = parse_locations(text)
    assert [loc.city for loc in locs] == cities
    assert country_codes(locs) == countries


def test_parse_locations_keeps_region_and_raw():
    (loc,) = parse_locations("Bengaluru, Karnataka, India")
    assert (loc.city, loc.region, loc.country_code) == ("Bengaluru", "Karnataka", "IN")
    assert loc.raw == "Bengaluru, Karnataka, India"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Remote", WorkArrangement.remote),
        ("TELECOMMUTE", WorkArrangement.remote),
        ("Hybrid - Bengaluru", WorkArrangement.hybrid),
        ("On-site", WorkArrangement.onsite),
        ("Work from office", WorkArrangement.onsite),
        ("Bengaluru, India", None),
        (None, None),
    ],
)
def test_parse_work_arrangement(value, expected):
    assert parse_work_arrangement(value) == expected


def test_infer_work_arrangement_description_is_conservative():
    assert infer_work_arrangement("SWE", "Pune", description="We collaborate with remote teams.") == (
        WorkArrangement.unknown
    )
    assert infer_work_arrangement("SWE", "Pune", description="This is a fully remote role.") == WorkArrangement.remote
    assert infer_work_arrangement("SWE", "Remote, India") == WorkArrangement.remote


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Full time", EmploymentType.full_time),
        ("FULL_TIME", EmploymentType.full_time),
        ("Part-time", EmploymentType.part_time),
        ("Intern", EmploymentType.internship),
        ("Internship", EmploymentType.internship),
        ("Contract", EmploymentType.contract),
        ("CONTRACTOR", EmploymentType.contract),
        ("Temporary", EmploymentType.temporary),
        ("Apprenticeship", EmploymentType.apprenticeship),
        (["PART_TIME", "CONTRACTOR"], EmploymentType.part_time),
        ("Whatever", None),
        (None, None),
    ],
)
def test_parse_employment_type(value, expected):
    assert parse_employment_type(value) == expected


def test_infer_employment_type_from_title():
    assert infer_employment_type(None, title="Software Engineering Intern") == EmploymentType.internship
    assert infer_employment_type(None, title="Engineer") == EmploymentType.unknown


@pytest.mark.parametrize(
    ("text", "lo", "hi"),
    [
        ("3+ years of experience in Python", "3", None),
        ("2-5 years", "2", "5"),
        ("2 to 5 years of relevant experience", "2", "5"),
        ("Minimum of 4 years in backend development", "4", None),
        ("At least 6 yrs experience with Java", "6", None),
        ("5 years of experience", "5", None),
        ("Looking for 1.5-3 years experience.", "1.5", "3"),
    ],
)
def test_parse_experience(text, lo, hi):
    exp = parse_experience(text)
    assert exp is not None
    assert exp.min_years == Decimal(lo)
    assert exp.max_years == (Decimal(hi) if hi else None)
    assert exp.sentence


def test_parse_experience_sentence_and_none():
    exp = parse_experience("About us. We are great. You have 3+ years of experience. Apply now!")
    assert exp and exp.sentence == "You have 3+ years of experience."
    assert parse_experience("No numbers here") is None
    assert parse_experience(None) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Experience with Python, Java and JavaScript", ["Python", "Java", "JavaScript"]),
        ("Strong C++ and C# skills, .NET a plus", ["C++", "C#", ".NET"]),
        ("PostgreSQL and NoSQL databases", ["PostgreSQL"]),  # SQL must not match inside either word
        ("We use Node.js, React and k8s", ["Node.js", "React", "Kubernetes"]),
        ("Let's go and swift movers", []),  # ambiguous words only match as proper skills
        ("Golang services on AWS", ["Go", "AWS"]),
        ("Knowledge of SQL, Docker", ["SQL", "Docker"]),
        ("", []),
    ],
)
def test_extract_skills(text, expected):
    assert extract_skills(text) == expected


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Sr. Software Engineer", "senior software engineer"),
        ("Jr. Developer (JR2029481)", "junior developer"),
        ("Software Engg - II", "software engineer ii"),
        ("  Data   Engineer,  Platform!! ", "data engineer platform"),
        ("Backend Engineer [REQ-12345]", "backend engineer"),
        ("SDE-2 #4411", "sde 2"),
        ("C++ Developer", "c++ developer"),
    ],
)
def test_normalize_title(title, expected):
    assert normalize_title(title) == expected


@pytest.mark.parametrize(
    ("text", "currency", "period", "lo", "hi", "raw"),
    [
        ("₹12-18 LPA", "INR", SalaryPeriod.annual, 1_200_000, 1_800_000, "₹12-18 LPA"),
        ("12 LPA", "INR", SalaryPeriod.annual, 1_200_000, 1_200_000, "12 LPA"),
        ("INR 15,00,000", "INR", SalaryPeriod.annual, 1_500_000, 1_500_000, "INR 15,00,000"),
        ("Rs. 80,000 per month", "INR", SalaryPeriod.monthly, 80_000, 80_000, "Rs. 80,000 per month"),
        ("₹1.2 Cr per annum", "INR", SalaryPeriod.annual, 12_000_000, 12_000_000, "₹1.2 Cr per annum"),
        ("12-18 lakhs per annum", "INR", SalaryPeriod.annual, 1_200_000, 1_800_000, "12-18 lakhs per annum"),
        ("$120,000 - $150,000", "USD", SalaryPeriod.annual, 120_000, 150_000, "$120,000 - $150,000"),
        ("USD 120,000 to 150,000 per year", "USD", SalaryPeriod.annual, 120_000, 150_000, None),
        ("$50/hr", "USD", SalaryPeriod.hourly, 50, 50, "$50/hr"),
        ("$8,000 per month", "USD", SalaryPeriod.monthly, 8_000, 8_000, "$8,000 per month"),
        ("£60k - £80k", "GBP", SalaryPeriod.annual, 60_000, 80_000, None),
    ],
)
def test_parse_compensation(text, currency, period, lo, hi, raw):
    parsed = parse_compensation(text)
    assert parsed is not None, text
    assert (parsed.currency, parsed.period) == (currency, period)
    assert (parsed.amount_min, parsed.amount_max) == (Decimal(lo), Decimal(hi))
    if raw:
        assert parsed.raw_text == raw


@pytest.mark.parametrize("text", ["Competitive salary", "We raised $5 million in funding", "Team of 12 people", None])
def test_parse_compensation_none(text):
    assert parse_compensation(text) is None


def test_parse_compensation_in_description_needs_context():
    assert parse_compensation("Join 3 lakh users. Ship fast.", require_context=True) is None
    found = parse_compensation("The salary range for this role is $120,000 - $150,000.", require_context=True)
    assert found and found.amount_max == Decimal(150_000)
    assert parse_compensation("We have 500 employees and $200 million revenue", require_context=True) is None


@pytest.mark.parametrize(
    ("text", "delta"),
    [
        ("Posted Today", timedelta(0)),
        ("Posted Yesterday", timedelta(days=1)),
        ("Posted 3 Days Ago", timedelta(days=3)),
        ("Posted 30+ Days Ago", timedelta(days=30)),
        ("Posted 2 Weeks Ago", timedelta(weeks=2)),
    ],
)
def test_parse_relative_posted(text, delta):
    assert parse_relative_posted(text, NOW) == (NOW - delta, DatePrecision.approximate)


def test_parse_relative_posted_unknown():
    assert parse_relative_posted("Open until filled", NOW) is None
    assert parse_relative_posted(None, NOW) is None


def test_sanitize_html_strips_scripts_keeps_formatting():
    dirty = '<p onclick="x()">Hi <strong>there</strong><script>alert(1)</script> <a href="javascript:evil()">x</a></p>'
    clean = sanitize_html(dirty)
    assert "script" not in clean and "onclick" not in clean and "javascript:" not in clean
    assert "<strong>there</strong>" in clean
    good = sanitize_html('<a href="https://example.com/x">link</a>')
    assert 'href="https://example.com/x"' in good and "noopener" in good
    assert sanitize_html(None) is None


def test_html_text_roundtrip_helpers():
    assert html_to_text("<p>Fish &amp; chips</p><ul><li>a</li><li>b</li></ul>") == "Fish & chips\n\n- a\n- b"
    assert html_to_text(None) is None
    assert text_to_html("a<b>\n\nsecond") == "<p>a&lt;b&gt;</p><p>second</p>"
