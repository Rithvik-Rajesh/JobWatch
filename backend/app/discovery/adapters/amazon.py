"""Public Amazon Jobs feed. The listing already contains the full description."""

from contextlib import suppress
from datetime import UTC, datetime
from typing import ClassVar

from app.discovery.adapters.base import DetailRecord, Fetcher, IndiaQuery, ListedPosting, ListingResult, SchemaChanged
from app.discovery.configs import CustomConfig
from app.discovery.normalize import parse_employment_type, text_to_html
from app.models.enums import DatePrecision

BASE_URL = "https://www.amazon.jobs"
PAGE_SIZE = 50


class AmazonAdapter:
    platform: ClassVar[str] = "custom:amazon"
    version: ClassVar[str] = "1"
    Config: ClassVar[type[CustomConfig]] = CustomConfig

    @classmethod
    def matches_url(cls, url: str) -> bool:
        return url.startswith(f"{BASE_URL}/")

    async def list(self, config: CustomConfig, india: IndiaQuery, http: Fetcher) -> ListingResult:
        postings: dict[str, ListedPosting] = {}
        warnings = []
        india_only = india.country_codes == ("IN",)
        total = 0
        for page in range(config.max_pages):
            offset = page * PAGE_SIZE
            params = {"offset": offset, "result_limit": PAGE_SIZE, "sort": "recent"}
            if india_only:
                params["normalized_country_code[]"] = "IND"
            data = await http.get_json(f"{BASE_URL}/en/search.json", params=params)
            if not isinstance(data, dict) or data.get("error") or not isinstance(data.get("jobs"), list):
                raise SchemaChanged("Amazon Jobs response did not contain a jobs list")
            rows = data["jobs"]
            total = data.get("hits")
            if not isinstance(total, int) or total < 0:
                raise SchemaChanged("Amazon Jobs response did not contain a valid result count")
            for row in rows:
                posting = self._posting(row)
                if posting is None or posting.external_id in postings:
                    warnings.append({"code": "invalid_or_duplicate_listing"})
                else:
                    postings[posting.external_id] = posting
            if offset + len(rows) >= total or len(rows) < PAGE_SIZE:
                return ListingResult(
                    list(postings.values()),
                    offset + len(rows) >= total and not warnings,
                    page + 1,
                    total,
                    india_only,
                    warnings,
                )
        return ListingResult(list(postings.values()), False, config.max_pages, total, india_only, warnings)

    @staticmethod
    def _posting(row: object) -> ListedPosting | None:
        if not isinstance(row, dict):
            return None
        external_id = row.get("id_icims") or row.get("id")
        path, title = row.get("job_path"), row.get("title")
        if not external_id or not isinstance(title, str) or not title.strip():
            return None
        if not isinstance(path, str) or not path.startswith("/en/jobs/"):
            return None
        posted_at = None
        with suppress(ValueError, TypeError):
            posted_at = datetime.strptime(row.get("posted_date") or "", "%B %d, %Y").replace(tzinfo=UTC)
        return ListedPosting(
            external_id=str(external_id),
            title=title,
            url=f"{BASE_URL}{path}",
            requisition_id=str(row["id_icims"]) if row.get("id_icims") else None,
            location_texts=[row["location"]] if row.get("location") else [],
            country_codes=["IN"] if row.get("country_code") == "IND" else [],
            posted_at=posted_at,
            posted_at_precision=DatePrecision.day if posted_at else DatePrecision.unknown,
            has_full_detail=True,
            raw=row,
        )

    async def detail(self, config: CustomConfig, posting: ListedPosting, http: Fetcher) -> DetailRecord:
        row = posting.raw
        sections = [
            text_to_html(row.get("description")),
            row.get("basic_qualifications"),
            row.get("preferred_qualifications"),
        ]
        return DetailRecord(
            description_html="<br><br>".join(section for section in sections if section) or None,
            employment_type=parse_employment_type(row.get("job_schedule_type")),
        )
