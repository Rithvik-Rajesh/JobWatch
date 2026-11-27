"""Public feeds that include job descriptions in the listing response."""

from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any, ClassVar
from urllib.parse import urlparse
from xml.etree import ElementTree

from app.discovery.adapters.base import DetailRecord, Fetcher, IndiaQuery, ListedPosting, ListingResult, SchemaChanged
from app.discovery.configs import CustomConfig
from app.discovery.normalize import country_for, parse_employment_type
from app.models.enums import DatePrecision


def _posting(
    external_id: Any,
    title: Any,
    url: Any,
    locations: list[str],
    description: str,
    *,
    country: str = "",
    posted_at: datetime | None = None,
    employment: str | None = None,
) -> ListedPosting | None:
    if not external_id or not isinstance(title, str) or not title.strip() or not isinstance(url, str):
        return None
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    country_code = country_for(country)
    return ListedPosting(
        external_id=str(external_id),
        title=title.strip(),
        url=url,
        location_texts=locations,
        country_codes=[country_code] if country_code else [],
        employment_type=parse_employment_type(employment),
        posted_at=posted_at,
        posted_at_precision=DatePrecision.exact if posted_at else DatePrecision.unknown,
        has_full_detail=True,
        raw={"description": description},
    )


def _collect(postings: dict[str, ListedPosting], posting: ListedPosting | None) -> bool:
    if posting is None or posting.external_id in postings:
        return False
    postings[posting.external_id] = posting
    return True


class _PublicFeed:
    version: ClassVar[str] = "1"
    Config: ClassVar[type[CustomConfig]] = CustomConfig
    host: ClassVar[str]

    @classmethod
    def matches_url(cls, url: str) -> bool:
        return urlparse(url).hostname == cls.host

    async def detail(self, config: CustomConfig, posting: ListedPosting, http: Fetcher) -> DetailRecord:
        return DetailRecord(description_html=posting.raw.get("description"))


class AtlassianAdapter(_PublicFeed):
    platform: ClassVar[str] = "custom:atlassian"
    host: ClassVar[str] = "www.atlassian.com"

    async def list(self, config: CustomConfig, india: IndiaQuery, http: Fetcher) -> ListingResult:
        rows = await http.get_json(f"https://{self.host}/endpoint/careers/listings")
        if not isinstance(rows, list):
            raise SchemaChanged("Atlassian did not return a list of jobs")
        postings: dict[str, ListedPosting] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            portal = row.get("portalJobPost")
            locations = row.get("locations")
            description = "\n".join(
                value
                for key in ("overview", "responsibilities", "qualifications")
                if isinstance(value := row.get(key), str)
            )
            _collect(
                postings,
                _posting(
                    row.get("id"),
                    row.get("title"),
                    portal.get("portalUrl") if isinstance(portal, dict) else None,
                    [value for value in locations if isinstance(value, str)] if isinstance(locations, list) else [],
                    description,
                    employment=row.get("type"),
                ),
            )
        return ListingResult(list(postings.values()), len(postings) == len(rows), 1, len(rows))


class GrabAdapter(_PublicFeed):
    platform: ClassVar[str] = "custom:grab"
    host: ClassVar[str] = "www.grab.careers"

    async def list(self, config: CustomConfig, india: IndiaQuery, http: Fetcher) -> ListingResult:
        xml = await http.get_text(f"https://{self.host}/en/jobs/xml/", params={"rss": "true"})
        if "<!DOCTYPE" in xml:
            raise SchemaChanged("Unexpected XML document type in Grab's feed")
        try:
            root = ElementTree.fromstring(xml)
        except ElementTree.ParseError as exc:
            raise SchemaChanged("Invalid Grab XML feed") from exc
        if root.tag != "source" or root.findtext("publisher") != "Grab":
            raise SchemaChanged("Unexpected Grab feed format")
        rows = root.findall("job")
        postings: dict[str, ListedPosting] = {}
        for row in rows:
            posted_at = None
            date = row.findtext("date")
            if date:
                try:
                    posted_at = parsedate_to_datetime(date)
                except (ValueError, TypeError):
                    posted_at = None
                if posted_at and posted_at.tzinfo is None:
                    posted_at = None
            city, region, country = (row.findtext(key, "").strip() for key in ("city", "state", "country"))
            _collect(
                postings,
                _posting(
                    row.findtext("apijobid") or row.findtext("requisitionid"),
                    row.findtext("title"),
                    row.findtext("url"),
                    [", ".join(filter(None, (city, region, country)))],
                    row.findtext("description", ""),
                    country=country,
                    posted_at=posted_at,
                ),
            )
        return ListingResult(list(postings.values()), len(postings) == len(rows), 1, len(rows))


class IcimsJibeAdapter(_PublicFeed):
    platform: ClassVar[str] = "custom:amd"
    host: ClassVar[str] = "careers.amd.com"
    page_size: ClassVar[int] = 50

    async def list(self, config: CustomConfig, india: IndiaQuery, http: Fetcher) -> ListingResult:
        postings: dict[str, ListedPosting] = {}
        india_only = india.country_codes == ("IN",)
        total = None
        all_valid = True
        count = 0
        for page in range(1, config.max_pages + 1):
            params: dict[str, Any] = {"limit": self.page_size, "page": page}
            if india_only:
                params["country"] = "India"
            payload = await http.get_json(f"https://{self.host}/api/jobs", params=params)
            if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
                raise SchemaChanged("iCIMS did not return a list of jobs")
            rows = payload["jobs"]
            total = payload.get("totalCount")
            if not isinstance(total, int) or total < 0:
                raise SchemaChanged("iCIMS did not return a valid totalCount")
            count += len(rows)
            previous_count = len(postings)
            for row in rows:
                posting = self._map_job(row.get("data") if isinstance(row, dict) else None)
                if not _collect(postings, posting):
                    all_valid = False
            if count >= total or len(rows) < self.page_size:
                return ListingResult(list(postings.values()), count >= total and all_valid, page, total, india_only)
            if len(postings) == previous_count:
                return ListingResult(list(postings.values()), False, page, total, india_only)
        return ListingResult(list(postings.values()), False, config.max_pages, total, india_only)

    def _map_job(self, row: Any) -> ListedPosting | None:
        if not isinstance(row, dict):
            return None
        slug = str(row.get("slug") or "").strip()
        if not slug.isdecimal():
            return None
        try:
            date = datetime.fromisoformat(row.get("posted_date") or "")
        except (ValueError, TypeError):
            date = None
        return _posting(
            row.get("req_id") or slug,
            row.get("title"),
            f"https://{self.host}/careers-home/jobs/{slug}",
            [row["full_location"]] if isinstance(row.get("full_location"), str) else [],
            row.get("description") or "",
            country=row.get("country") or "",
            posted_at=date if date and date.tzinfo else None,
        )
