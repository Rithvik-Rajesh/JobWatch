"""Oracle HCM Cloud (Candidate Experience) adapter.

Uses the public REST API that the career site itself calls:
  list:   GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?finder=findReqs;...
  detail: GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?finder=ById;...

India is requested via `locationId` (keyword search undercounts badly). When the config has no
`india_location_id`, it is resolved from the LOCATIONS facet of an unfiltered first response and cached on the
config object (the caller may persist it).
"""

import re
from datetime import UTC, datetime
from typing import Any, ClassVar

from pydantic import BaseModel

from app.discovery.adapters.base import (
    DetailRecord,
    Fetcher,
    IndiaQuery,
    ListedPosting,
    ListingResult,
    SchemaChanged,
)
from app.discovery.configs import OracleHcmConfig
from app.discovery.normalize.html import html_to_text, sanitize_html
from app.models.enums import DatePrecision, WorkArrangement

PAGE_SIZE = 25
MAX_PAGES = 200


def _base(config: OracleHcmConfig) -> str:
    return f"https://{config.host}/hcmRestApi/resources/latest"


def _site_url(config: OracleHcmConfig, req_id: str) -> str:
    return f"https://{config.host}/hcmUI/CandidateExperience/{config.language}/sites/{config.site_number}/job/{req_id}"


def _search_params(config: OracleHcmConfig, offset: int, location_id: str | None) -> dict[str, str]:
    finder = f"findReqs;siteNumber={config.site_number},facetsList=LOCATIONS,limit={PAGE_SIZE},offset={offset}"
    if location_id:
        finder += f",locationId={location_id}"
    finder += ",sortBy=POSTING_DATES_DESC"
    return {
        "onlyData": "true",
        "expand": "requisitionList.secondaryLocations,flexFieldsFacet.values",
        "finder": finder,
    }


def _search_envelope(payload: Any) -> dict[str, Any]:
    """Return items[0] of a findReqs response (holds TotalJobsCount, requisitionList, facets)."""
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list) or not payload["items"]:
        raise SchemaChanged("oracle_hcm: response has no 'items'")
    env = payload["items"][0]
    if not isinstance(env, dict) or not isinstance(env.get("requisitionList"), list):
        raise SchemaChanged("oracle_hcm: items[0].requisitionList missing")
    return env


def _parse_date(value: Any) -> tuple[datetime | None, DatePrecision]:
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None, DatePrecision.unknown
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        precision = DatePrecision.day if len(value) <= 10 else DatePrecision.exact
        return dt, precision
    return None, DatePrecision.unknown


def _is_remote(item: dict[str, Any]) -> bool | None:
    code = str(item.get("WorkplaceTypeCode") or "").upper()
    name = str(item.get("WorkplaceType") or "").lower()
    if "REMOTE" in code or "remote" in name:
        return True
    if code in ("ORA_ON_SITE", "ORA_HYBRID") or name in ("on-site", "onsite", "hybrid"):
        return False
    return None


def _locations(item: dict[str, Any]) -> tuple[list[str], list[str]]:
    texts: list[str] = []
    codes: list[str] = []

    def add_text(t: Any) -> None:
        if isinstance(t, str) and t.strip() and t.strip() not in texts:
            texts.append(t.strip())

    def add_code(c: Any) -> None:
        if isinstance(c, str) and len(c) == 2 and c.upper() not in codes:
            codes.append(c.upper())

    add_text(item.get("PrimaryLocation"))
    add_code(item.get("PrimaryLocationCountry"))
    for sec in item.get("secondaryLocations") or []:
        if isinstance(sec, dict):
            add_text(sec.get("Name"))
            add_code(sec.get("CountryCode"))
    return texts, codes


def _find_india_id(envelope: dict[str, Any], india: IndiaQuery) -> str | None:
    facet = envelope.get("locationsFacet")
    if facet is None:
        raise SchemaChanged("oracle_hcm: LOCATIONS facet missing from first response")
    if not isinstance(facet, list):
        raise SchemaChanged("oracle_hcm: LOCATIONS facet is not a list")
    wanted = {n.strip().lower() for n in india.country_names}
    for entry in facet:
        if (
            isinstance(entry, dict)
            and str(entry.get("Name", "")).strip().lower() in wanted
            and entry.get("Id") is not None
        ):
            return str(entry["Id"])
    return None


class OracleHcmAdapter:
    platform: ClassVar[str] = "oracle_hcm"
    version: ClassVar[str] = "1"
    Config: ClassVar[type[BaseModel]] = OracleHcmConfig

    async def list(self, config: BaseModel, india: IndiaQuery, http: Fetcher) -> ListingResult:
        assert isinstance(config, OracleHcmConfig)
        url = f"{_base(config)}/recruitingCEJobRequisitions"
        warnings: list[dict[str, Any]] = []
        pages = 0

        location_id = config.india_location_id
        if not location_id:
            first = await http.get_json(url, params=_search_params(config, 0, None))
            pages += 1
            env = _search_envelope(first)
            location_id = _find_india_id(env, india)
            if location_id is None:
                warnings.append({"code": "india_location_not_found", "detail": "no India entry in LOCATIONS facet"})
                return ListingResult(
                    postings=[],
                    complete=True,
                    pages_fetched=pages,
                    source_total=0,
                    used_source_india_filter=True,
                    warnings=warnings,
                )
            config.india_location_id = location_id  # cached for the caller to persist

        postings: list[ListedPosting] = []
        seen: set[str] = set()
        total: int | None = None
        offset = 0
        complete = True
        fetched = 0
        while True:
            if fetched >= getattr(config, "max_pages", MAX_PAGES):
                complete = False
                warnings.append(
                    {"code": "page_cap", "detail": f"stopped after {getattr(config, 'max_pages', MAX_PAGES)} pages"}
                )
                break
            payload = await http.get_json(url, params=_search_params(config, offset, location_id))
            pages += 1
            fetched += 1
            env = _search_envelope(payload)
            reqs = env["requisitionList"]
            if isinstance(env.get("TotalJobsCount"), int):
                total = env["TotalJobsCount"]
            if not reqs:
                break
            for item in reqs:
                posting = self._to_posting(config, item)
                if posting.external_id not in seen:
                    seen.add(posting.external_id)
                    postings.append(posting)
            offset += PAGE_SIZE
            if total is not None and offset >= total:
                break

        return ListingResult(
            postings=postings,
            complete=complete,
            pages_fetched=pages,
            source_total=total,
            used_source_india_filter=True,
            warnings=warnings,
        )

    def _to_posting(self, config: OracleHcmConfig, item: Any) -> ListedPosting:
        if not isinstance(item, dict) or not item.get("Id") or not isinstance(item.get("Title"), str):
            raise SchemaChanged("oracle_hcm: requisition without Id/Title")
        req_id = str(item["Id"])
        texts, codes = _locations(item)
        posted_at, precision = _parse_date(item.get("PostedDate"))
        return ListedPosting(
            external_id=f"{config.site_number}:{req_id}",
            title=item["Title"].strip(),
            url=_site_url(config, req_id),
            requisition_id=req_id,
            location_texts=texts,
            country_codes=codes,
            remote=_is_remote(item),
            department=item.get("JobFamily") or item.get("JobFunction") or None,
            posted_at=posted_at,
            posted_at_precision=precision,
            posted_at_raw=item.get("PostedDate"),
            has_full_detail=False,
            raw={
                "Id": req_id,
                "WorkplaceType": item.get("WorkplaceType"),
                "ShortDescriptionStr": item.get("ShortDescriptionStr"),
            },
        )

    async def detail(self, config: BaseModel, posting: ListedPosting, http: Fetcher) -> DetailRecord:
        assert isinstance(config, OracleHcmConfig)
        req_id = posting.requisition_id or posting.external_id.split(":", 1)[-1]
        payload = await http.get_json(
            f"{_base(config)}/recruitingCEJobRequisitionDetails",
            params={
                "expand": "all",
                "onlyData": "true",
                "finder": f'ById;Id="{req_id}",siteNumber={config.site_number}',
            },
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise SchemaChanged("oracle_hcm: detail response has no 'items'")
        if not payload["items"]:
            raise SchemaChanged(f"oracle_hcm: detail for {req_id} returned no items")
        item = payload["items"][0]
        if not isinstance(item, dict):
            raise SchemaChanged("oracle_hcm: detail item is not an object")

        parts = [
            item.get(k)
            for k in ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr")
            if isinstance(item.get(k), str) and item.get(k).strip()
        ]
        html = sanitize_html("\n".join(parts)) if parts else None
        texts, codes = _locations(item)
        wl = item.get("workLocation")
        if isinstance(wl, list):
            for w in wl:
                if isinstance(w, dict):
                    bits = [w.get("TownOrCity"), w.get("Region2"), w.get("Country")]
                    label = ", ".join(str(b) for b in bits if b)
                    if label and label not in texts:
                        texts.append(label)
                    if isinstance(w.get("Country"), str) and len(w["Country"]) == 2:
                        c = w["Country"].upper()
                        if c not in codes:
                            codes.append(c)
        remote = _is_remote(item)
        posted_at, precision = _parse_date(item.get("ExternalPostedStartDate"))
        return DetailRecord(
            description_html=html,
            description_text=html_to_text(html) if html else None,
            apply_url=_site_url(config, str(item.get("Id") or req_id)),
            location_texts=texts or None,
            country_codes=codes or None,
            remote=remote,
            work_arrangement=WorkArrangement.remote if remote else None,
            department=item.get("JobFamily") or item.get("Category") or None,
            posted_at=posted_at,
            posted_at_precision=precision if posted_at else None,
            requisition_id=str(item.get("Id") or req_id),
            raw={"RequisitionId": item.get("RequisitionId"), "Category": item.get("Category")},
        )

    @classmethod
    def matches_url(cls, url: str) -> bool:
        # *.oraclecloud.com tenants, and vanity hosts such as enterpriseplatform.dell.com, share this path.
        return re.match(r"^https?://[^/]+/hcmUI/CandidateExperience(/|$)", url.strip(), re.I) is not None
