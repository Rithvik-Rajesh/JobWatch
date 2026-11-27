"""Eightfold career-site adapter (two public endpoint variants, chosen per tenant in config).

  pcsx      GET https://{host}/api/pcsx/search?domain=&query=&location=&start=&num=      (Microsoft, Qualcomm,
            Eightfold AI)       response: {"data": {"positions": [...], "count": N}}
  apply_v2  GET https://{host}/api/apply/v2/jobs?domain=&location=&start=&num=            (NetApp)
            response: {"positions": [...], "count": N}

`location=<location_query>` is a fuzzy source-side filter; the pipeline's India gate still evaluates every
posting, so location strings are reported exactly as the source gives them.

No position-detail endpoint is verified in the registry evidence, so `detail()` performs NO network request:
it returns whatever the listing already carried (the v2 listing may include `job_description`).
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
from app.discovery.configs import EightfoldConfig
from app.discovery.normalize.html import html_to_text, sanitize_html
from app.models.enums import DatePrecision

PAGE_SIZE_REQUEST = "10"
MAX_PAGES = 200
_CC_RE = re.compile(r"^[A-Za-z]{2}$")


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    seconds = value / 1000 if value > 1e11 else value  # tolerate millisecond epochs
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for v in value:
        if isinstance(v, str) and v.strip() and v.strip() not in out:
            out.append(v.strip())
    return out


def _remote_flag(option: Any) -> bool | None:
    if not isinstance(option, str):
        return None
    o = option.lower()
    if "remote" in o:
        return True
    if o in ("onsite", "on-site", "on_site", "hybrid"):
        return False
    return None


def _extract(variant: str, payload: Any) -> tuple[list[Any], int | None]:
    body = (payload.get("data") if isinstance(payload, dict) else None) if variant == "pcsx" else payload
    if not isinstance(body, dict) or not isinstance(body.get("positions"), list):
        raise SchemaChanged(f"eightfold/{variant}: 'positions' list missing")
    count = body.get("count")
    return body["positions"], count if isinstance(count, int) and not isinstance(count, bool) else None


class EightfoldAdapter:
    platform: ClassVar[str] = "eightfold"
    version: ClassVar[str] = "1"
    Config: ClassVar[type[BaseModel]] = EightfoldConfig

    async def list(self, config: BaseModel, india: IndiaQuery, http: Fetcher) -> ListingResult:
        assert isinstance(config, EightfoldConfig)
        variant = config.variant
        url = (
            f"https://{config.host}/api/pcsx/search"
            if variant == "pcsx"
            else f"https://{config.host}/api/apply/v2/jobs"
        )

        postings: list[ListedPosting] = []
        seen: set[str] = set()
        warnings: list[dict[str, Any]] = []
        total: int | None = None
        start = 0
        pages = 0
        complete = True
        while True:
            if pages >= getattr(config, "max_pages", MAX_PAGES):
                complete = False
                warnings.append(
                    {"code": "page_cap", "detail": f"stopped after {getattr(config, 'max_pages', MAX_PAGES)} pages"}
                )
                break
            params: dict[str, str] = {"domain": config.domain}
            if variant == "pcsx":
                params["query"] = ""
            params.update({"location": config.location_query, "start": str(start), "num": PAGE_SIZE_REQUEST})
            payload = await http.get_json(url, params=params)
            pages += 1
            positions, count = _extract(variant, payload)
            if count is not None:
                total = count
            if not positions:
                break
            for pos in positions:
                posting = self._to_posting(config, pos)
                if posting.external_id not in seen:
                    seen.add(posting.external_id)
                    postings.append(posting)
            start += len(positions)  # the server decides the real page size
            if total is not None and start >= total:
                break

        return ListingResult(
            postings=postings,
            complete=complete,
            pages_fetched=pages,
            source_total=total,
            used_source_india_filter=True,
            warnings=warnings,
        )

    def _to_posting(self, config: EightfoldConfig, pos: Any) -> ListedPosting:
        if not isinstance(pos, dict) or pos.get("id") in (None, "") or not isinstance(pos.get("name"), str):
            raise SchemaChanged("eightfold: position without id/name")
        pid = str(pos["id"])
        locations = _str_list(pos.get("locations"))
        if not locations:
            locations = _str_list(pos.get("location"))
        codes: list[str] = []
        for c in _str_list(pos.get("standardizedLocations")):
            tail = c.rsplit(",", 1)[-1].strip()  # "IN" or "Hyderabad, TS, IN"
            if _CC_RE.match(tail) and tail.upper() not in codes:
                codes.append(tail.upper())

        if config.variant == "pcsx":
            rel = pos.get("positionUrl")
            posted = _timestamp(pos.get("postedTs"))
            remote = _remote_flag(pos.get("workLocationOption"))
            req = pos.get("displayJobId") or pos.get("atsJobId")
            description = None
        else:
            rel = pos.get("canonicalPositionUrl")
            posted = _timestamp(pos.get("t_create"))
            remote = _remote_flag(pos.get("work_location_option"))
            req = pos.get("display_job_id") or pos.get("ats_job_id")
            description = pos.get("job_description") if isinstance(pos.get("job_description"), str) else None

        if isinstance(rel, str) and rel.startswith("http"):
            url = rel
        elif isinstance(rel, str) and rel.startswith("/"):
            url = f"https://{config.host}{rel}"
        else:
            url = f"https://{config.host}/careers/job/{pid}?domain={config.domain}"

        department = pos.get("department")
        return ListedPosting(
            external_id=pid,
            title=pos["name"].strip(),
            url=url,
            requisition_id=str(req) if req not in (None, "") else None,
            location_texts=locations,
            country_codes=codes,
            remote=remote,
            department=department if isinstance(department, str) and department.strip() else None,
            posted_at=posted,
            posted_at_precision=DatePrecision.exact if posted else DatePrecision.unknown,
            has_full_detail=bool(description and description.strip()),
            raw={"id": pid, "job_description": description} if description else {"id": pid},
        )

    async def detail(self, config: BaseModel, posting: ListedPosting, http: Fetcher) -> DetailRecord:
        description = posting.raw.get("job_description")
        html = sanitize_html(description) if isinstance(description, str) else None
        return DetailRecord(
            description_html=html,
            description_text=html_to_text(html) if html else None,
            apply_url=posting.url,
            requisition_id=posting.requisition_id,
        )

    @classmethod
    def matches_url(cls, url: str) -> bool:
        m = re.match(r"^https?://([^/:?#]+)", url.strip(), re.I)
        if not m:
            return False
        host = m.group(1).lower()
        return host.endswith(".eightfold.ai") or host == "eightfold.ai" or host == "apply.careers.microsoft.com"
