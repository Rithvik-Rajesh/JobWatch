# ruff: noqa: RUF001
"""Greenhouse job-board adapter (public Job Board API, one request, `content=true`).

GET https://{api_host}/v1/boards/{token}/jobs?content=true returns every job with its (HTML-escaped) body, so
`list()` is exhaustive in a single request and `detail()` never touches the network.

Location handling: Greenhouse gives free-text location strings only (no structured country), so
`country_codes` stays empty. `location.name` is preserved verbatim (trailing spaces, 'Bangalore, IND', ...);
the pipeline's India gate interprets it. `remote_eligibility` is filled only from an explicit country written
in the same string ("Remote - India" -> ["India"]); a bare "Remote" gives [].
"""

import hashlib
import html
import re
from datetime import UTC, datetime
from typing import Any, ClassVar

from app.discovery.adapters.base import (
    DetailRecord,
    Fetcher,
    IndiaQuery,
    ListedPosting,
    ListingResult,
    SchemaChanged,
)
from app.discovery.configs import GreenhouseConfig
from app.discovery.normalize import country_for, html_to_text, parse_employment_type
from app.discovery.normalize.location import COUNTRY_BY_NAME
from app.models.enums import DatePrecision

# ───────────────────────── shared helpers (also imported by the Lever and Ashby adapters) ─────────────────────────

_REMOTE_RE = re.compile(r"\b(?:remote|wfh|work\s+from\s+home)\b", re.I)
_PIECE_SPLIT = re.compile(r"\s*[;|]\s*|\s+\bor\b\s+", re.I)
_TOKEN_SPLIT = re.compile(r"\s*[-–—,:()/]\s*")
_LEAD_WORDS = re.compile(r"^(?:(?:in|within|from|the|anywhere in)\s+)+", re.I)
# Two-letter forms are accepted only for these (others, e.g. 'CA', 'IN', collide with US state codes).
_SAFE_ALPHA2 = {"US", "UK"}
_DISPLAY_NAME: dict[str, str] = {}
for _name, _code in COUNTRY_BY_NAME.items():
    if len(_name) > 3 and _code not in _DISPLAY_NAME:
        _DISPLAY_NAME[_code] = _name.title()


def mentions_remote(texts: list[str]) -> bool:
    return any(_REMOTE_RE.search(t) for t in texts)


def remote_countries(texts: list[str]) -> list[str]:
    """Countries written explicitly in the same location string as a 'remote' marker.

    'Remote - India' -> ['India']; 'Remote - Tamil Nadu, India' -> ['India']; 'Remote' -> []; 'Remote - Pune' -> [].
    """
    found: list[str] = []
    for text in texts:
        for piece in _PIECE_SPLIT.split(text):
            if not _REMOTE_RE.search(piece):
                continue
            for token in _TOKEN_SPLIT.split(_REMOTE_RE.sub(" ", piece)):
                token = _LEAD_WORDS.sub("", token.strip()).strip()
                if len(token) < 2 or (len(token) == 2 and token not in _SAFE_ALPHA2):
                    continue
                code = country_for(token)
                if code:
                    name = _DISPLAY_NAME.get(code, token)
                    if name not in found:
                        found.append(name)
    return found


def dedupe_texts(values: list[str | None]) -> list[str]:
    """Drop empty values and case/whitespace-insensitive duplicates, otherwise preserve text verbatim."""
    out: list[str] = []
    seen: set[str] = set()
    for v in values:
        if not isinstance(v, str) or not v.strip():
            continue
        key = " ".join(v.split()).casefold()
        if key not in seen:
            seen.add(key)
            out.append(v)
    return out


def fingerprint(*parts: Any) -> str:
    return hashlib.sha1("\x1f".join("" if p is None else str(p) for p in parts).encode()).hexdigest()


# ───────────────────────── adapter ─────────────────────────

_URL_RE = re.compile(
    r"^https?://(?:(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/[A-Za-z0-9_-]+|boards-api\.greenhouse\.io/v1/boards/[A-Za-z0-9_-]+)",
    re.I,
)
_REQ_ID_NAME = re.compile(r"req(?:uisition)?\W*(?:id|number)|job\s*id", re.I)
_EMPLOYMENT_NAME = re.compile(r"employment\s*type|job\s*type|worker\s*type", re.I)
_JUNK_REQ_ID = re.compile(r"see\s+opening|^n/?a$|^none$|^tbd$", re.I)


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _metadata(job: dict[str, Any], name_re: re.Pattern[str]) -> str | None:
    for item in job.get("metadata") or []:
        if isinstance(item, dict) and name_re.search(str(item.get("name") or "")):
            value = item.get("value")
            if isinstance(value, list):
                value = value[0] if value else None
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


class GreenhouseAdapter:
    platform: ClassVar[str] = "greenhouse"
    version: ClassVar[str] = "1"
    Config: ClassVar[type[GreenhouseConfig]] = GreenhouseConfig

    @staticmethod
    def jobs_url(config: GreenhouseConfig) -> str:
        return f"https://{config.api_host}/v1/boards/{config.board_token}/jobs?content=true"

    async def list(self, config: GreenhouseConfig, india: IndiaQuery, http: Fetcher) -> ListingResult:  # type: ignore[override]
        data = await http.get_json(self.jobs_url(config))
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise SchemaChanged("Greenhouse response has no top-level 'jobs' list")
        total = (data.get("meta") or {}).get("total") if isinstance(data.get("meta"), dict) else None
        postings: list[ListedPosting] = []
        warnings: list[dict[str, Any]] = []
        for job in data["jobs"]:
            if (
                not isinstance(job, dict)
                or job.get("id") is None
                or not job.get("title")
                or not job.get("absolute_url")
            ):
                warnings.append({"code": "malformed_posting", "detail": str(job)[:200]})
                continue
            postings.append(self._to_posting(config, job))
        complete = not warnings and (not isinstance(total, int) or total == len(data["jobs"]))
        if isinstance(total, int) and total != len(data["jobs"]):
            warnings.append({"code": "total_mismatch", "detail": f"meta.total={total}, jobs={len(data['jobs'])}"})
        return ListingResult(
            postings=postings,
            complete=complete,
            pages_fetched=1,
            source_total=total if isinstance(total, int) else None,
            warnings=warnings,
        )

    def _to_posting(self, config: GreenhouseConfig, job: dict[str, Any]) -> ListedPosting:
        loc_name = (job.get("location") or {}).get("name") if isinstance(job.get("location"), dict) else None
        office_texts: list[str | None] = []
        for office in job.get("offices") or []:
            if isinstance(office, dict):
                office_texts += [office.get("location"), office.get("name")]
        texts = dedupe_texts([loc_name, *office_texts])
        is_remote = mentions_remote(texts)
        # Only the job's own location string(s) may express eligibility (office names are org-chart labels).
        own_texts = dedupe_texts(
            [loc_name, *[o.get("location") for o in job.get("offices") or [] if isinstance(o, dict)]]
        )

        first_published = _parse_dt(job.get("first_published"))
        updated_at = _parse_dt(job.get("updated_at"))
        if first_published:
            posted_at, precision, raw_date = first_published, DatePrecision.day, job.get("first_published")
        elif updated_at:
            posted_at, precision, raw_date = updated_at, DatePrecision.approximate, job.get("updated_at")
        else:
            posted_at, precision, raw_date = None, DatePrecision.unknown, None

        req = job.get("requisition_id")
        req = req.strip() if isinstance(req, str) else (str(req) if req else None)
        if not req or _JUNK_REQ_ID.search(req):
            req = _metadata(job, _REQ_ID_NAME)
        departments = [d.get("name") for d in job.get("departments") or [] if isinstance(d, dict) and d.get("name")]
        description_html = html.unescape(job["content"]) if isinstance(job.get("content"), str) else None
        raw = {k: v for k, v in job.items() if k not in {"content", "data_compliance"}}
        raw["description_html"] = description_html
        raw["apply_url"] = job["absolute_url"]

        return ListedPosting(
            external_id=f"{config.board_token}:{job['id']}",
            title=str(job["title"]).strip(),
            url=job["absolute_url"],
            requisition_id=req,
            location_texts=texts,
            country_codes=[],
            remote=True if is_remote else None,
            remote_eligibility=remote_countries(own_texts) if is_remote else [],
            department=departments[0] if departments else None,
            employment_type=parse_employment_type(_metadata(job, _EMPLOYMENT_NAME)),
            posted_at=posted_at,
            posted_at_precision=precision,
            posted_at_raw=raw_date,
            fingerprint=fingerprint(job.get("updated_at"), job.get("title"), loc_name, len(description_html or "")),
            has_full_detail=True,
            raw=raw,
        )

    async def detail(self, config: GreenhouseConfig, posting: ListedPosting, http: Fetcher) -> DetailRecord:  # type: ignore[override]
        """Built from the listing's raw data (content=true already delivered the body). No request."""
        description_html = posting.raw.get("description_html")
        return DetailRecord(
            description_html=description_html,
            description_text=html_to_text(description_html),
            apply_url=posting.raw.get("apply_url") or posting.url,
            requisition_id=posting.requisition_id,
            raw=posting.raw,
        )

    @classmethod
    def matches_url(cls, url: str) -> bool:
        return bool(_URL_RE.match(url.strip()))
