"""Workday "cxs" adapter (platform "workday").

List:   POST {api_base}/jobs  {"appliedFacets": {...}, "limit": 20, "offset": N, "searchText": ""}
Detail: GET  {api_base}{externalPath}

India targeting (see WorkdayConfig.india_strategy):
  * facet / auto: resolve the tenant's country facet (a facet value whose descriptor equals one of
    `india.country_names`), cache the ids in `config.facet_cache`, and apply them as `appliedFacets`.
  * location_text (and `auto` when no country facet exists): enumerate everything; the pipeline's India gate
    decides from location strings.
  * `searchText` is ALWAYS empty: searching "India" matches Indiana.

Workday reports `total` capped at 2000 and only on page 1, so we paginate until an empty page, and split a
capped India result set by a sub-facet (city / location) when one is available.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any, ClassVar

from app.discovery.adapters.base import (
    ConfigError,
    DetailRecord,
    Fetcher,
    IndiaQuery,
    ListedPosting,
    ListingResult,
    SchemaChanged,
)
from app.discovery.configs import WorkdayConfig
from app.discovery.normalize.dates import parse_relative_posted
from app.discovery.normalize.employment_type import parse_employment_type
from app.discovery.normalize.work_arrangement import parse_work_arrangement
from app.models.enums import DatePrecision, WorkArrangement

PAGE_SIZE = 20  # Workday rejects larger pages with HTTP 400
TOTAL_CAP = 2000  # Workday's reported `total` ceiling
_URL_RE = re.compile(r"^https?://[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com(?:[/?#]|$)", re.I)
_N_LOCATIONS_RE = re.compile(r"^\s*\d+\s+locations?\s*$", re.I)
_REQ_PATH_RE = re.compile(r"_([A-Za-z]{1,5}-?\d+)(?:-\d+)?$")
_SPLIT_PARAMS = ("locations", "locationCity", "locationHierarchy2")
_SPLIT_DESCRIPTORS = {"city", "locations", "location", "sites", "site"}


def _as_dict(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SchemaChanged(f"Workday {what}: expected an object, got {type(value).__name__}")
    return value


def _walk_facets(facets: Any):
    """Yield every facet dict (with `facetParameter` and `values`), descending into nested groups such as
    `locationMainGroup`, whose values are themselves facets."""
    for f in facets or []:
        if not isinstance(f, dict):
            continue
        values = f.get("values") or []
        if f.get("facetParameter"):
            yield f
        nested = [v for v in values if isinstance(v, dict) and "values" in v]
        if nested:
            yield from _walk_facets(nested)


def _find_country_facet(facets: Any, country_names: tuple[str, ...]) -> tuple[str, list[str]] | None:
    wanted = {n.strip().lower() for n in country_names}
    candidates: list[tuple[str, list[str], bool]] = []
    for f in _walk_facets(facets):
        ids = [
            v["id"]
            for v in f.get("values") or []
            if isinstance(v, dict) and v.get("id") and str(v.get("descriptor", "")).strip().lower() in wanted
        ]
        if ids:
            hint = "country" in f["facetParameter"].lower() or "country" in str(f.get("descriptor", "")).lower()
            candidates.append((f["facetParameter"], ids, hint))
    if not candidates:
        return None
    candidates.sort(key=lambda c: not c[2])  # stable: prefer facets that say "country"
    return candidates[0][0], candidates[0][1]


def _find_split_facet(facets: Any, param: str | None) -> tuple[str, list[dict[str, Any]]] | None:
    found = list(_walk_facets(facets))
    chosen = None
    if param:
        chosen = next((f for f in found if f["facetParameter"] == param), None)
    else:
        for name in _SPLIT_PARAMS:
            chosen = next((f for f in found if f["facetParameter"] == name), None)
            if chosen:
                break
        if chosen is None:
            chosen = next(
                (f for f in found if str(f.get("descriptor", "")).strip().lower() in _SPLIT_DESCRIPTORS), None
            )
    if chosen is None:
        return None
    values = [v for v in chosen.get("values") or [] if isinstance(v, dict) and v.get("id") and v.get("count") != 0]
    return (chosen["facetParameter"], values) if values else None


def _requisition_from_item(item: dict[str, Any], path: str) -> str | None:
    for b in item.get("bulletFields") or []:
        s = str(b).strip()
        if s and re.search(r"\d", s) and not re.search(r"\s", s) and len(s) <= 40:
            return s
    m = _REQ_PATH_RE.search(path)
    return m.group(1) if m else None


def _fingerprint(item: dict[str, Any]) -> str:
    payload = [item.get("title"), item.get("locationsText"), item.get("bulletFields")]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:32]


class _Enumeration:
    """Mutable state shared by the (possibly several) paginated enumerations of one list() call."""

    def __init__(self, max_pages: int) -> None:
        self.max_pages = max_pages
        self.pages = 0
        self.complete = True
        self.postings: dict[str, ListedPosting] = {}
        self.warnings: list[dict[str, Any]] = []

    def warn(self, code: str, message: str, **extra: Any) -> None:
        self.warnings.append({"code": code, "message": message, **extra})


class WorkdayAdapter:
    platform: ClassVar[str] = "workday"
    version: ClassVar[str] = "1"
    Config: ClassVar[type[WorkdayConfig]] = WorkdayConfig

    # ───────────────────────────── list ─────────────────────────────

    async def list(self, config: WorkdayConfig, india: IndiaQuery, http: Fetcher) -> ListingResult:
        site = config.site
        url = f"{site.api_base}/jobs"
        state = _Enumeration(config.max_pages)
        strategy = config.india_strategy

        applied: dict[str, list[str]] | None = None
        first_page: dict[str, Any] | None = None  # unfiltered page 1, reused if we fall back to location_text
        if strategy != "location_text":
            if config.facet_cache:
                applied = {k: list(v) for k, v in config.facet_cache.items() if v}
            else:
                first_page = await self._page(http, url, {}, 0)
                state.pages += 1
                found = _find_country_facet(first_page.get("facets"), india.country_names)
                if config.facet_param:
                    # Explicit key: only accept a value from that facet.
                    in_param = [
                        f for f in _walk_facets(first_page.get("facets")) if f["facetParameter"] == config.facet_param
                    ]
                    found = _find_country_facet(in_param, india.country_names) if in_param else None
                    if found is None and in_param:
                        # The facet exists but has no India value: nothing to list for India.
                        return ListingResult([], True, state.pages, 0, True, [])
                if found is not None:
                    applied = {found[0]: found[1]}
                    config.facet_cache = dict(applied)
                elif strategy == "facet":
                    raise ConfigError(
                        f"No country facet with an India value found for {site.tenant}/{site.site}; "
                        "set facet_param or use india_strategy=location_text"
                    )
                else:
                    state.warn("no_country_facet", "No country facet found; enumerating all postings (location_text)")

        if applied is None:
            total, _ = await self._enumerate(http, url, {}, state, first_page=first_page)
            return ListingResult(
                list(state.postings.values()), state.complete, state.pages, total, False, state.warnings
            )

        result_total, facets = await self._enumerate(http, url, applied, state)
        if result_total is not None and result_total >= TOTAL_CAP:
            await self._split(http, url, applied, facets, config, state)
        return ListingResult(
            list(state.postings.values()), state.complete, state.pages, result_total, True, state.warnings
        )

    async def _page(self, http: Fetcher, url: str, applied: dict[str, list[str]], offset: int) -> dict[str, Any]:
        body = {"appliedFacets": applied, "limit": PAGE_SIZE, "offset": offset, "searchText": ""}
        data = _as_dict(await http.post_json(url, json=body), "list response")
        if not isinstance(data.get("jobPostings"), list):
            raise SchemaChanged("Workday list response has no 'jobPostings' array")
        return data

    async def _enumerate(
        self,
        http: Fetcher,
        url: str,
        applied: dict[str, list[str]],
        state: _Enumeration,
        *,
        first_page: dict[str, Any] | None = None,
    ) -> tuple[int | None, Any]:
        """Paginate one filter combination until an empty page. Returns (total from page 1, page-1 facets)."""
        total: int | None = None
        facets: Any = None
        offset = 0
        data = first_page
        page_no = 0
        while True:
            if data is None:
                if state.pages >= state.max_pages:
                    state.complete = False
                    state.warn("max_pages", f"Stopped at max_pages={state.max_pages}; listing incomplete")
                    break
                data = await self._page(http, url, applied, offset)
                state.pages += 1
            if page_no == 0:  # `total` is only populated on page 1
                raw_total = data.get("total")
                total = raw_total if isinstance(raw_total, int) else None
                facets = data.get("facets")
            items = data["jobPostings"]
            if not items:
                break
            for item in items:
                posting = self._to_posting(item, url)
                if posting is not None:
                    state.postings.setdefault(posting.external_id, posting)
            offset += len(items)
            page_no += 1
            data = None
        return total, facets

    async def _split(
        self,
        http: Fetcher,
        url: str,
        applied: dict[str, list[str]],
        facets: Any,
        config: WorkdayConfig,
        state: _Enumeration,
    ) -> None:
        split = _find_split_facet(facets, config.split_facet_param)
        if split is None:
            state.complete = False
            state.warn(
                "capped_unsplittable",
                f"India result total reached Workday's {TOTAL_CAP} cap and no sub-facet is available to split it",
            )
            return
        param, values = split
        for i, value in enumerate(values):
            sub_applied = {**applied, param: [value["id"]]}
            sub_total, _ = await self._enumerate(http, url, sub_applied, state)
            if sub_total is not None and sub_total >= TOTAL_CAP:
                state.complete = False
                state.warn(
                    "capped_after_split",
                    f"Sub-facet {param}={value.get('descriptor')!r} still reaches the {TOTAL_CAP} cap",
                    facet=param,
                    value=value.get("descriptor"),
                )
            if state.pages >= state.max_pages and i < len(values) - 1:
                state.complete = False  # page budget spent; remaining sub-facets are skipped
                state.warn("max_pages", "max_pages reached before all sub-facets were listed")
                break

    def _to_posting(self, item: Any, list_url: str) -> ListedPosting | None:
        if not isinstance(item, dict):
            return None
        path = item.get("externalPath")
        title = item.get("title")
        if not path or not title:
            return None  # e.g. placeholder rows without a posting page
        site_part = list_url.split("/wday/cxs/", 1)[1].rsplit("/jobs", 1)[0]  # "{tenant}/{site}"
        host = list_url.split("/")[2]
        site = site_part.split("/", 1)[1]
        loc = (item.get("locationsText") or "").strip()
        posted_raw = item.get("postedOn")
        parsed = parse_relative_posted(posted_raw, datetime.now(UTC))
        return ListedPosting(
            external_id=f"{site_part}:{path}",
            title=str(title).strip(),
            url=f"https://{host}/{site}{path}",
            requisition_id=_requisition_from_item(item, path),
            location_texts=[loc] if loc and not _N_LOCATIONS_RE.match(loc) else [],
            posted_at=parsed[0] if parsed else None,
            posted_at_precision=parsed[1] if parsed else DatePrecision.unknown,
            posted_at_raw=posted_raw,
            fingerprint=_fingerprint(item),
            raw={"externalPath": path, "locationsText": loc, "bulletFields": item.get("bulletFields") or []},
        )

    # ───────────────────────────── detail ─────────────────────────────

    async def detail(self, config: WorkdayConfig, posting: ListedPosting, http: Fetcher) -> DetailRecord:
        path = posting.raw.get("externalPath") or posting.external_id.split(":", 1)[1]
        data = _as_dict(await http.get_json(f"{config.site.api_base}{path}"), "detail response")
        info = data.get("jobPostingInfo")
        if not isinstance(info, dict) or not info.get("title"):
            raise SchemaChanged("Workday detail response lacks jobPostingInfo.title")

        locations: list[str] = []
        for loc in [info.get("location"), *(info.get("additionalLocations") or [])]:
            if isinstance(loc, str) and loc.strip() and loc.strip() not in locations:
                locations.append(loc.strip())

        codes: list[str] = []
        for holder in (info.get("jobRequisitionLocation"), info):
            country = holder.get("country") if isinstance(holder, dict) else None
            code = country.get("alpha2Code") if isinstance(country, dict) else None
            if isinstance(code, str) and code and code.upper() not in codes:
                codes.append(code.upper())

        posted_at = None
        start = info.get("startDate")
        if isinstance(start, str):
            try:
                posted_at = datetime.fromisoformat(start[:10]).replace(tzinfo=UTC)
            except ValueError:
                posted_at = None

        arrangement = parse_work_arrangement(info.get("remoteType"))
        remote = True if arrangement == WorkArrangement.remote else False if arrangement else None

        return DetailRecord(
            description_html=info.get("jobDescription"),
            apply_url=info.get("externalUrl"),
            location_texts=locations or None,
            country_codes=codes or None,
            remote=remote,
            employment_type=parse_employment_type(info.get("timeType")),
            work_arrangement=arrangement,
            posted_at=posted_at,
            posted_at_precision=DatePrecision.day if posted_at else None,
            requisition_id=info.get("jobReqId"),
            raw={
                "jobPostingId": info.get("jobPostingId"),
                "country": info.get("country"),
                "hiringOrganization": (data.get("hiringOrganization") or {}).get("name"),
            },
        )

    @classmethod
    def matches_url(cls, url: str) -> bool:
        return bool(_URL_RE.match((url or "").strip()))
