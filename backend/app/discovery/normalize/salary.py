# ruff: noqa: RUF001
"""Detect employer-provided compensation in text (INR lakh/crore/LPA, USD/GBP/EUR)."""

import re
from dataclasses import dataclass
from decimal import Decimal

from app.models.enums import SalaryPeriod

_NUM = r"\d[\d,]*(?:\.\d+)?"
_CUR = r"(?:₹|INR|Rs\.?|Rupees|US\$|USD|\$|£|GBP|€|EUR)"
_UNIT = r"(?:lpa|lakhs?|lacs?|crores?|cr|k)"
_SEP = r"\s*(?:-|–|—|to)\s*"
_PERIOD = (
    r"(?:\s*(?:per|/|a)\s*(?:annum|year|yr|month|mo|hour|hr)\b"
    r"|\s*p\.?\s?a\.?(?![a-z])"
    r"|\s*(?:monthly|hourly|annually|yearly))"
)

_WITH_CUR = re.compile(
    rf"(?P<cur>{_CUR})\s*(?P<a>{_NUM})(?:\s*(?P<ua>{_UNIT})\b)?"
    rf"(?:{_SEP}(?:{_CUR})?\s*(?P<b>{_NUM})(?:\s*(?P<ub>{_UNIT})\b)?)?(?P<period>{_PERIOD})?",
    re.I,
)
_NO_CUR = re.compile(
    rf"(?<![\w.])(?P<a>\d+(?:\.\d+)?)(?:{_SEP}(?P<b>\d+(?:\.\d+)?))?\s*(?P<ua>lpa|lakhs?|lacs?|crores?|cr)\b"
    rf"(?P<period>{_PERIOD})?",
    re.I,
)
_CONTEXT = re.compile(
    r"salary|compensation|ctc|pay\b|remuneration|package|stipend|wage|earn|per\s+(?:annum|year|month|hour)|"
    r"lpa|annual|base\s+pay|range",
    re.I,
)
_CUR_CODE = {
    "₹": "INR", "inr": "INR", "rs": "INR", "rs.": "INR", "rupees": "INR",
    "$": "USD", "us$": "USD", "usd": "USD", "£": "GBP", "gbp": "GBP", "€": "EUR", "eur": "EUR",
}  # fmt: skip
_MULT = {"lpa": 100_000, "lakh": 100_000, "lakhs": 100_000, "lac": 100_000, "lacs": 100_000,
         "cr": 10_000_000, "crore": 10_000_000, "crores": 10_000_000, "k": 1_000}  # fmt: skip


@dataclass(frozen=True)
class ParsedSalary:
    currency: str
    period: SalaryPeriod
    amount_min: Decimal
    amount_max: Decimal
    raw_text: str


def _num(s: str) -> Decimal:
    return Decimal(s.replace(",", ""))


def _period(text: str | None, unit: str | None) -> SalaryPeriod | None:
    t = (text or "").lower()
    if re.search(r"month|/\s*mo\b|monthly", t):
        return SalaryPeriod.monthly
    if re.search(r"hour|hr|hourly", t):
        return SalaryPeriod.hourly
    if re.search(r"annum|year|yr|annual|yearly|p\.?\s?a", t):
        return SalaryPeriod.annual
    if unit and unit.lower() in ("lpa",):
        return SalaryPeriod.annual
    return None


def _build(m: re.Match[str], *, currency: str | None, whole: str) -> ParsedSalary | None:
    ua = (m.group("ua") or "").lower() or None
    ub = (m.groupdict().get("ub") or "").lower() or None
    unit_a = ua or ub
    a = _num(m.group("a")) * _MULT.get(unit_a or "", 1)
    b_raw = m.group("b")
    b = _num(b_raw) * _MULT.get(ub or unit_a or "", 1) if b_raw else a
    lo, hi = (a, b) if a <= b else (b, a)
    period = _period(m.group("period"), unit_a)
    currency = currency or "INR"
    explicit_scale = bool(unit_a and unit_a != "k")
    if period is None:
        if currency == "INR":
            period = (
                SalaryPeriod.annual if explicit_scale or hi >= 100_000
                else SalaryPeriod.monthly if hi >= 10_000 else None
            )  # fmt: skip
        elif hi >= 20_000:
            period = SalaryPeriod.annual
        elif hi >= 500:
            period = SalaryPeriod.unknown
    # reject implausible magnitudes (e.g. "$5 million funding", "Rs 10", "$3 per word")
    if period is None:
        return None
    floor = {SalaryPeriod.annual: 10_000, SalaryPeriod.monthly: 1_000, SalaryPeriod.hourly: 5}.get(period, 1_000)
    if currency == "INR" and period == SalaryPeriod.annual:
        floor = 50_000
    if hi < floor:
        return None
    return ParsedSalary(currency, period, lo, hi, whole.strip())


def parse_compensation(text: str | None, *, require_context: bool = False) -> ParsedSalary | None:
    """First compensation figure in `text`. With require_context (free descriptions) the figure must sit near
    a salary keyword or carry an explicit period / LPA unit; for dedicated salary fields it may be bare."""
    if not text:
        return None
    candidates: list[tuple[int, re.Match[str], str | None]] = []
    for m in _WITH_CUR.finditer(text):
        candidates.append((m.start(), m, _CUR_CODE.get(m.group("cur").lower())))
    for m in _NO_CUR.finditer(text):
        candidates.append((m.start(), m, None))
    for _, m, cur in sorted(candidates, key=lambda c: c[0]):
        whole = m.group(0)
        if require_context:
            window = text[max(0, m.start() - 120) : m.end() + 60]
            explicit = bool(m.group("period")) or bool(re.search(r"\blpa\b", whole, re.I))
            if not explicit and not _CONTEXT.search(window):
                continue
        parsed = _build(m, currency=cur, whole=whole)
        if parsed:
            return parsed
    return None
