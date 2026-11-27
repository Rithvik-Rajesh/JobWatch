"""Ashby job-board adapter (public Posting API, one request).

GET https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true returns every listed job with
its description and compensation, so `list()` is exhaustive and `detail()` makes no request.

Structured country codes come only from `address.postalAddress.addressCountry` (primary and secondary
locations). `location` / `secondaryLocations[].location` are free text ('IN-Pune', 'IN-Bangalore-MSO').

Remote: Ashby's `isRemote` is true for hybrid postings too, so `workplaceType` wins when present
(Remote -> remote, Hybrid/OnSite -> not remote); `isRemote` alone is used only when workplaceType is absent.
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
from app.discovery.configs import AshbyConfig
from app.discovery.normalize import country_for, html_to_text, parse_employment_type
from app.models.enums import DatePrecision, WorkArrangement

_URL_RE = re.compile(
    r"^https?://(?:jobs\.ashbyhq\.com/[A-Za-z0-9_.%-]+|api\.ashbyhq\.com/posting-api/job-board/[A-Za-z0-9_.%-]+)", re.I
)
_ARRANGEMENT = {
    "remote": WorkArrangement.remote,
    "hybrid": WorkArrangement.hybrid,
    "onsite": WorkArrangement.onsite,
    "on-site": WorkArrangement.onsite,
}


def _address_country(address: Any) -> str | None:
    if not isinstance(address, dict):
        return None
    postal = address.get("postalAddress")
    country = postal.get("addressCountry") if isinstance(postal, dict) else None
    return country_for(country) if isinstance(country, str) and country.strip() else None


def _salary_text(job: dict[str, Any]) -> str | None:
    if job.get("shouldDisplayCompensationOnJobPostings") is False:
        return None
    comp = job.get("compensation")
    if not isinstance(comp, dict):
        return None
    for key in ("compensationTierSummary", "scrapeableCompensationSalarySummary"):
        if isinstance(comp.get(key), str) and comp[key].strip():
            return comp[key].strip()
    return None


class AshbyAdapter:
    platform: ClassVar[str] = "ashby"
    version: ClassVar[str] = "1"
    Config: ClassVar[type[AshbyConfig]] = AshbyConfig

    @staticmethod
    def jobs_url(config: AshbyConfig) -> str:
        return f"https://api.ashbyhq.com/posting-api/job-board/{config.org}?includeCompensation=true"

    async def list(self, config: AshbyConfig, india: IndiaQuery, http: Fetcher) -> ListingResult:  # type: ignore[override]
        data = await http.get_json(self.jobs_url(config))
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise SchemaChanged("Ashby response has no top-level 'jobs' list")
        postings: list[ListedPosting] = []
        warnings: list[dict[str, Any]] = []
        for job in data["jobs"]:
            if not isinstance(job, dict) or not job.get("id") or not job.get("title") or not job.get("jobUrl"):
                warnings.append({"code": "malformed_posting", "detail": str(job)[:200]})
                continue
            if job.get("isListed") is False:
                continue
            postings.append(self._to_posting(job))
        return ListingResult(
            postings=postings,
            complete=not warnings,
            pages_fetched=1,
            source_total=len(data["jobs"]),
            warnings=warnings,
        )

    def _to_posting(self, job: dict[str, Any]) -> ListedPosting:
        secondary = [s for s in job.get("secondaryLocations") or [] if isinstance(s, dict)]
        texts = dedupe_texts([job.get("location"), *[s.get("location") for s in secondary]])
        codes: list[str] = []
        for address in [job.get("address"), *[s.get("address") for s in secondary]]:
            code = _address_country(address)
            if code and code not in codes:
                codes.append(code)

        arrangement = _ARRANGEMENT.get(str(job.get("workplaceType") or "").lower())
        if arrangement is not None:
            is_remote: bool | None = arrangement == WorkArrangement.remote
        elif job.get("isRemote") is True or mentions_remote(texts):
            is_remote = True
        else:
            is_remote = job["isRemote"] if isinstance(job.get("isRemote"), bool) else None

        published = None
        if isinstance(job.get("publishedAt"), str):
            try:
                published = datetime.fromisoformat(job["publishedAt"].replace("Z", "+00:00"))
                if published.tzinfo is None:
                    published = published.replace(tzinfo=UTC)
            except ValueError:
                published = None
        description_html = job.get("descriptionHtml") if isinstance(job.get("descriptionHtml"), str) else None
        raw = {
            "description_html": description_html,
            "description_plain": job.get("descriptionPlain"),
            "apply_url": job.get("applyUrl"),
            "work_arrangement": arrangement.value if arrangement else None,
            "salary_text": _salary_text(job),
            "team": job.get("team"),
            "workplaceType": job.get("workplaceType"),
            "isRemote": job.get("isRemote"),
        }
        return ListedPosting(
            external_id=str(job["id"]),
            title=str(job["title"]).strip(),
            url=job["jobUrl"],
            location_texts=texts,
            country_codes=codes,
            remote=is_remote,
            remote_eligibility=remote_countries(texts) if is_remote else [],
            department=job.get("department") or job.get("team") or None,
            employment_type=parse_employment_type(job.get("employmentType")),
            posted_at=published,
            posted_at_precision=DatePrecision.day if published else DatePrecision.unknown,
            posted_at_raw=job.get("publishedAt"),
            fingerprint=fingerprint(job.get("title"), texts, job.get("employmentType"), len(description_html or "")),
            has_full_detail=True,
            raw=raw,
        )

    async def detail(self, config: AshbyConfig, posting: ListedPosting, http: Fetcher) -> DetailRecord:  # type: ignore[override]
        """Built from the listing's raw data. No request."""
        description_html = posting.raw.get("description_html")
        arrangement = posting.raw.get("work_arrangement")
        return DetailRecord(
            description_html=description_html,
            description_text=posting.raw.get("description_plain") or html_to_text(description_html),
            apply_url=posting.raw.get("apply_url"),
            work_arrangement=WorkArrangement(arrangement) if arrangement else None,
            salary_text=posting.raw.get("salary_text"),
            raw=posting.raw,
        )

    @classmethod
    def matches_url(cls, url: str) -> bool:
        return bool(_URL_RE.match(url.strip()))
