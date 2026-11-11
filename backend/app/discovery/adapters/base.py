"""Adapter contract — CENTRAL (lead-owned). Every career-site adapter implements `Adapter`.

Design rules
  * One adapter per PLATFORM (workday, greenhouse, ...). Company differences are CONFIG, validated by the
    adapter's `Config` model and stored in career_sites.config. Bespoke company systems get one module in
    adapters/custom/ implementing the same protocol.
  * Adapters never perform raw HTTP. They call the injected `Fetcher`, whose production implementation
    (PoliteClient) enforces the honest User-Agent, robots.txt, rate limits/crawl-delay, retries and block
    detection. Tests inject `FixtureFetcher` (recorded real responses).
  * Adapters do NOT decide India eligibility; they report the location evidence they have (structured
    country codes, location strings, remote flags). The pipeline's India gate decides. Adapters MAY use the
    source's own India filter (`IndiaQuery`) to reduce volume.
  * `list()` must set `complete=False` whenever enumeration was not exhaustive (a page failed, a cap was hit,
    a safety page limit was reached). Incomplete runs never close jobs.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel

from app.models.enums import DatePrecision, EmploymentType, WorkArrangement

USER_AGENT = "Mozilla/5.0 (compatible; JobWatch/0.1; personal job tracker)"


# ───────────────────────────── errors ─────────────────────────────


class AdapterError(Exception):
    """Base class. `error_class` is stored on scrape_runs."""

    error_class = "internal"


class ConfigError(AdapterError):
    """Site config is wrong (404 site, 422 wrong wdN, unknown board). Not retried; site -> failing."""

    error_class = "config"


class TransientError(AdapterError):
    """Network/5xx/timeout after retries. Retried by the worker; never closes jobs."""

    error_class = "network"


class SchemaChanged(AdapterError):
    """Response no longer matches the expected shape. Site -> failing, alert."""

    error_class = "schema_changed"


class PortalBlocked(AdapterError):
    """403/406/429-without-Retry-After/challenge page for the honest UA. Site is paused (health=blocked).
    NEVER retried or worked around."""

    error_class = "blocked"


class RobotsDisallowed(AdapterError):
    """robots.txt disallows the URL for our User-Agent. Site is paused (health=blocked)."""

    error_class = "robots_disallowed"


# ───────────────────────────── HTTP abstraction ─────────────────────────────


class Fetcher(Protocol):
    """Polite HTTP. Implementations raise PortalBlocked / RobotsDisallowed / TransientError / ConfigError."""

    async def get_json(
        self, url: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None
    ) -> Any: ...

    async def post_json(
        self,
        url: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any: ...

    async def get_text(
        self, url: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None
    ) -> str: ...


# ───────────────────────────── data passed between adapter and pipeline ─────────────────────────────


@dataclass(frozen=True)
class IndiaQuery:
    """What the adapter may use to ask the SOURCE for India jobs only."""

    country_codes: tuple[str, ...] = ("IN",)  # from LocationScopeSettings
    country_names: tuple[str, ...] = ("India",)


@dataclass
class ListedPosting:
    """One posting as seen on a listing page / feed. `external_id` must be stable and unique per site."""

    external_id: str
    title: str
    url: str  # public posting URL (preserved as source_url)
    requisition_id: str | None = None
    location_texts: list[str] = field(default_factory=list)  # every location string the source gives
    country_codes: list[str] = field(default_factory=list)  # ONLY from structured source fields (ISO alpha-2)
    remote: bool | None = None  # source explicitly marks remote
    remote_eligibility: list[str] = field(default_factory=list)  # e.g. ["India"] from "Remote - India"
    department: str | None = None
    employment_type: EmploymentType | None = None
    posted_at: datetime | None = None
    posted_at_precision: DatePrecision = DatePrecision.unknown
    posted_at_raw: str | None = None
    fingerprint: str | None = None  # hash of listing-level fields; change => refetch detail
    has_full_detail: bool = False  # listing already contains the full description (e.g. Greenhouse content=true)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class DetailRecord:
    """Full posting content. Fields left None are filled from the ListedPosting / normalizers."""

    description_html: str | None = None
    description_text: str | None = None
    apply_url: str | None = None
    location_texts: list[str] | None = None
    country_codes: list[str] | None = None
    remote: bool | None = None
    remote_eligibility: list[str] | None = None
    employment_type: EmploymentType | None = None
    work_arrangement: WorkArrangement | None = None
    department: str | None = None
    posted_at: datetime | None = None
    posted_at_precision: DatePrecision | None = None
    deadline_at: datetime | None = None
    salary_text: str | None = None
    requisition_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ListingResult:
    postings: list[ListedPosting]
    complete: bool  # True only if enumeration was exhaustive
    pages_fetched: int = 0
    source_total: int | None = None  # what the source claims, if anything
    used_source_india_filter: bool = False
    warnings: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ProbeResult:
    """Returned by Adapter.probe() for the "Detect" flow when adding a site."""

    platform: str
    config: dict[str, Any]
    sample_titles: list[str]
    india_jobs_estimate: int | None
    notes: str = ""


# ───────────────────────────── adapter protocol ─────────────────────────────


class Adapter(Protocol):
    platform: ClassVar[str]  # 'workday', 'greenhouse', ... or 'custom:<module>'
    version: ClassVar[str]  # bump when parsing changes (stored on scrape_runs.extractor_version)
    Config: ClassVar[type[BaseModel]]  # validates career_sites.config

    async def list(self, config: BaseModel, india: IndiaQuery, http: Fetcher) -> ListingResult: ...

    async def detail(self, config: BaseModel, posting: ListedPosting, http: Fetcher) -> DetailRecord: ...

    @classmethod
    def matches_url(cls, url: str) -> bool:
        """True if `url` looks like this platform (used by the Detect flow; no network)."""
        ...
