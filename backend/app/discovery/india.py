"""India gate: decides whether a discovered posting is in scope (default: India). Positive evidence only.

Order of evidence (first match wins):
  1. structured country codes from the source (never guessed)
  2. any location string that resolves to an in-scope country, city or state through the location normalizer
     (multi-location postings are in scope if ANY location qualifies)
  3. explicit remote eligibility ("Remote - India", `remote_eligibility=["India"]`)
Otherwise: resolved to other countries only -> out_of_scope; nothing usable -> unknown.
Bare "Remote", "Remote - Remote" and "Anywhere" are never in scope (unless `include_remote_anywhere`).
There is no substring matching on "India": "Indiana", "Indianapolis" and "Indian Wells" are US places and are
resolved (or left unresolved) by the normalizer, never to India.
"""

import re

from app.discovery.adapters.base import DetailRecord, ListedPosting
from app.discovery.contracts import ScopeDecision
from app.discovery.normalize.location import country_for, country_name, parse_locations
from app.models.enums import ScopeMatch
from app.settings.registry import LocationScopeSettings

_REMOTE_RE = re.compile(r"\b(?:fully\s+)?(?:remote|wfh|work\s+from\s+home|virtual|telecommute)\b", re.I)


_LABEL_SUFFIX_RE = re.compile(r"\s+(?:locations?|offices?|hubs?)\s*$", re.I)


def _parse(text: str):
    """parse_locations, plus org labels like 'India Locations' (suffix dropped only when nothing else resolves)."""
    locs = parse_locations(text)
    if not any(loc.country_code for loc in locs):
        stripped = _LABEL_SUFFIX_RE.sub("", text)
        if stripped != text:
            locs = parse_locations(stripped)
    return locs


def _merge(primary: list[str], extra: list[str] | None) -> list[str]:
    out = list(primary)
    for item in extra or []:
        if item not in out:
            out.append(item)
    return out


def evaluate_listing(posting: ListedPosting, detail: DetailRecord | None, cfg: LocationScopeSettings) -> ScopeDecision:
    in_scope = set(cfg.countries)
    codes = [c.upper() for c in posting.country_codes if c]
    texts = list(posting.location_texts)
    eligibility = list(posting.remote_eligibility)
    remote = bool(posting.remote)
    if detail is not None:
        codes = _merge(codes, [c.upper() for c in (detail.country_codes or []) if c])
        texts = _merge(texts, detail.location_texts)
        eligibility = _merge(eligibility, detail.remote_eligibility)
        remote = remote or bool(detail.remote)
    texts = [t.strip() for t in texts if t and t.strip()]

    # 1. structured country codes
    for code in codes:
        if code in in_scope:
            return ScopeDecision(ScopeMatch.in_scope, f"country {code} (structured)")

    # 2. location strings
    other_countries: list[str] = []
    saw_remote_text = False
    for text in texts:
        is_remote = bool(_REMOTE_RE.search(text))
        saw_remote_text = saw_remote_text or is_remote
        for loc in _parse(text):
            cc = loc.country_code
            if not cc:
                continue
            if cc in in_scope:
                if loc.city:
                    return ScopeDecision(ScopeMatch.in_scope, f"city: {loc.city}")
                if loc.region:
                    return ScopeDecision(ScopeMatch.in_scope, f"state: {loc.region}")
                if is_remote:
                    return ScopeDecision(ScopeMatch.in_scope, f"remote: {country_name(cc)} listed")
                return ScopeDecision(ScopeMatch.in_scope, f"country {cc} (text)")
            if cc not in other_countries:
                other_countries.append(cc)

    # 3. explicit remote eligibility
    for item in eligibility:
        cc = country_for(item)
        if cc is None:
            parsed = [loc.country_code for loc in parse_locations(item) if loc.country_code]
            cc = parsed[0] if parsed else None
        if cc in in_scope:
            return ScopeDecision(ScopeMatch.in_scope, f"remote: {country_name(cc)} listed")
        if cc and cc not in other_countries:
            other_countries.append(cc)

    for code in codes:
        if code not in other_countries:
            other_countries.append(code)

    if (remote or saw_remote_text) and cfg.include_remote_anywhere:
        return ScopeDecision(ScopeMatch.in_scope, "remote: anywhere allowed")
    if other_countries:
        return ScopeDecision(ScopeMatch.out_of_scope, f"country {other_countries[0]}")
    if remote or saw_remote_text:
        return ScopeDecision(ScopeMatch.unknown, "remote: India not listed")
    return ScopeDecision(ScopeMatch.unknown, "no location")
