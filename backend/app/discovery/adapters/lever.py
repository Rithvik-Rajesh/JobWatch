"""Lever postings adapter (public Postings API, one request).

GET https://api.lever.co/v0/postings/{company}?mode=json (api.eu.lever.co for the EU region) returns every
published posting including its description, so `list()` is exhaustive and `detail()` makes no request.

`categories.location` / `categories.allLocations` are free text; the posting-level `country` code is
structured and is the only source of `country_codes`.
"""

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
from app.discovery.adapters.greenhouse import dedupe_texts, fingerprint, mentions_remote, remote_countries
from app.discovery.configs import LeverConfig
from app.discovery.normalize import country_for, html_to_text, parse_employment_type, text_to_html
from app.models.enums import DatePrecision, WorkArrangement

_URL_RE = re.compile(r"^https?://(?:jobs|api)(?:\.eu)?\.lever\.co/(?:v0/postings/)?[A-Za-z0-9_.-]+", re.I)
_ARRANGEMENT = {
    "remote": WorkArrangement.remote,
    "hybrid": WorkArrangement.hybrid,
    "onsite": WorkArrangement.onsite,
    "on-site": WorkArrangement.onsite,
}


def _description_html(job: dict[str, Any]) -> str | None:
    parts: list[str] = []
    body = job.get("description")
    if isinstance(body, str) and body.strip():
        parts.append(body)
    elif isinstance(job.get("descriptionPlain"), str) and job["descriptionPlain"].strip():
        parts.append(text_to_html(job["descriptionPlain"]) or "")
    for section in job.get("lists") or []:
        if isinstance(section, dict) and section.get("content"):
            title = section.get("text")
            parts.append((f"<h3>{title}</h3>" if title else "") + f"<ul>{section['content']}</ul>")
    additional = job.get("additional")
    if isinstance(additional, str) and additional.strip():
        parts.append(additional)
    return "\n".join(p for p in parts if p) or None


def _salary_text(job: dict[str, Any]) -> str | None:
    desc = job.get("salaryDescriptionPlain") or job.get("salaryDescription")
    if isinstance(desc, str) and desc.strip():
        return html_to_text(desc)
    rng = job.get("salaryRange")
    if isinstance(rng, dict) and (rng.get("min") or rng.get("max")):
        interval = str(rng.get("interval") or "").replace("-", " ")
        return f"{rng.get('currency') or ''} {rng.get('min')}-{rng.get('max')} {interval}".strip()
    return None


class LeverAdapter:
    platform: ClassVar[str] = "lever"
    version: ClassVar[str] = "1"
    Config: ClassVar[type[LeverConfig]] = LeverConfig

    @staticmethod
    def jobs_url(config: LeverConfig) -> str:
        host = "api.eu.lever.co" if config.region == "eu" else "api.lever.co"
        return f"https://{host}/v0/postings/{config.company}?mode=json"

    async def list(self, config: LeverConfig, india: IndiaQuery, http: Fetcher) -> ListingResult:  # type: ignore[override]
        data = await http.get_json(self.jobs_url(config))
        if not isinstance(data, list):
            raise SchemaChanged("Lever response is not a list of postings")
        postings: list[ListedPosting] = []
        warnings: list[dict[str, Any]] = []
        for job in data:
            if not isinstance(job, dict) or not job.get("id") or not job.get("text") or not job.get("hostedUrl"):
                warnings.append({"code": "malformed_posting", "detail": str(job)[:200]})
                continue
            postings.append(self._to_posting(job))
        return ListingResult(
            postings=postings, complete=not warnings, pages_fetched=1, source_total=len(data), warnings=warnings
        )

    def _to_posting(self, job: dict[str, Any]) -> ListedPosting:
        cats = job.get("categories") if isinstance(job.get("categories"), dict) else {}
        texts = dedupe_texts([cats.get("location"), *(cats.get("allLocations") or [])])
        arrangement = _ARRANGEMENT.get(str(job.get("workplaceType") or "").lower())
        if arrangement is not None:
            is_remote: bool | None = arrangement == WorkArrangement.remote
        else:
            is_remote = True if mentions_remote(texts) else None
        country = job.get("country")
        code = country_for(country) if isinstance(country, str) and len(country.strip()) == 2 else None
        created = job.get("createdAt")
        posted_at = datetime.fromtimestamp(created / 1000, tz=UTC) if isinstance(created, int | float) else None
        description_html = _description_html(job)
        raw = {
            "description_html": description_html,
            "apply_url": job.get("applyUrl"),
            "work_arrangement": arrangement.value if arrangement else None,
            "salary_text": _salary_text(job),
            "team": cats.get("team"),
            "workplaceType": job.get("workplaceType"),
        }
        return ListedPosting(
            external_id=str(job["id"]),
            title=str(job["text"]).strip(),
            url=job["hostedUrl"],
            location_texts=texts,
            country_codes=[code] if code else [],
            remote=is_remote,
            remote_eligibility=remote_countries(texts) if is_remote else [],
            department=cats.get("department") or cats.get("team") or None,
            employment_type=parse_employment_type(cats.get("commitment")),
            posted_at=posted_at,
            posted_at_precision=DatePrecision.day if posted_at else DatePrecision.unknown,
            posted_at_raw=str(created) if created is not None else None,
            fingerprint=fingerprint(job.get("text"), texts, cats.get("commitment"), len(description_html or "")),
            has_full_detail=True,
            raw=raw,
        )

    async def detail(self, config: LeverConfig, posting: ListedPosting, http: Fetcher) -> DetailRecord:  # type: ignore[override]
        """Built from the listing's raw data. No request."""
        description_html = posting.raw.get("description_html")
        arrangement = posting.raw.get("work_arrangement")
        return DetailRecord(
            description_html=description_html,
            description_text=html_to_text(description_html),
            apply_url=posting.raw.get("apply_url"),
            work_arrangement=WorkArrangement(arrangement) if arrangement else None,
            salary_text=posting.raw.get("salary_text"),
            raw=posting.raw,
        )

    @classmethod
    def matches_url(cls, url: str) -> bool:
        return bool(_URL_RE.match(url.strip()))
