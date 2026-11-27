"""Per-platform career-site configuration models (validate career_sites.config). CENTRAL CONTRACT.

Shared by: the registry import (data/companies.csv), the sites API, and the platform adapters.
Rules
  * Config holds only what identifies the portal and how to ask it for India jobs. No title filters here:
    those are global (TitleFilterSettings) and applied by the pipeline, never inside adapters.
  * India targeting comes from the global LocationScopeSettings; `india_strategy` only says HOW this portal
    can express it. Facet ids discovered at runtime are cached in `*_cache` fields by the adapter.
"""

import re
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, HttpUrl, field_validator

WORKDAY_HOST_RE = re.compile(r"^(?P<tenant>[a-z0-9-]+)\.(?P<wd>wd\d+)\.myworkdayjobs\.com$", re.I)
LOCALE_RE = re.compile(r"^[a-z]{2}(-[A-Z]{2})?$")


class _Base(BaseModel):
    model_config = {"extra": "forbid"}


# ───────────────────────────── Workday ─────────────────────────────


class WorkdaySite(BaseModel):
    host: str
    tenant: str
    site: str

    @property
    def api_base(self) -> str:
        return f"https://{self.host}/wday/cxs/{self.tenant}/{self.site}"


def parse_workday_url(url: str) -> WorkdaySite:
    parsed = urlparse(url)
    m = WORKDAY_HOST_RE.match(parsed.hostname or "")
    if not m:
        raise ValueError("Not a myworkdayjobs.com career site URL")
    segments = [s for s in parsed.path.split("/") if s]
    if segments and LOCALE_RE.match(segments[0]):
        segments = segments[1:]
    if not segments:
        raise ValueError("Workday URL must include the site name, e.g. /External_Career_Site")
    return WorkdaySite(host=parsed.hostname.lower(), tenant=m["tenant"].lower(), site=segments[0])


class WorkdayConfig(_Base):
    kind: Literal["workday"] = "workday"
    url: HttpUrl  # https://{tenant}.wd{N}.myworkdayjobs.com/[locale/]{site}
    # facet: use the tenant's country facet (resolved by descriptor, e.g. "India");
    # location_text: facets are office-level -> fetch all, gate on location strings (Indiana-safe);
    # auto: try facet, fall back to location_text.
    india_strategy: Literal["auto", "facet", "location_text"] = "auto"
    facet_param: str | None = None  # override facet key, e.g. "locationCountry"
    facet_cache: dict[str, list[str]] = Field(default_factory=dict)  # written by the adapter
    split_facet_param: str | None = None  # sub-facet used to split result sets > 2000 (e.g. "locations")
    max_pages: int = Field(default=150, ge=1, le=1000)  # safety cap (20 per page); hitting it => incomplete

    @field_validator("url")
    @classmethod
    def _workday_url(cls, v: HttpUrl) -> HttpUrl:
        parse_workday_url(str(v))
        return v

    @property
    def site(self) -> WorkdaySite:
        return parse_workday_url(str(self.url))


# ───────────────────────────── Greenhouse / Lever / Ashby ─────────────────────────────


class GreenhouseConfig(_Base):
    kind: Literal["greenhouse"] = "greenhouse"
    board_token: str = Field(pattern=r"^[A-Za-z0-9_-]+$")  # boards-api.greenhouse.io/v1/boards/{token}/jobs
    api_host: str = "boards-api.greenhouse.io"


class LeverConfig(_Base):
    kind: Literal["lever"] = "lever"
    company: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")  # api.lever.co/v0/postings/{company}
    region: Literal["global", "eu"] = "global"  # eu -> api.eu.lever.co


class AshbyConfig(_Base):
    kind: Literal["ashby"] = "ashby"
    org: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")  # api.ashbyhq.com/posting-api/job-board/{org}


# ───────────────────────────── Oracle HCM / Eightfold ─────────────────────────────


class OracleHcmConfig(_Base):
    kind: Literal["oracle_hcm"] = "oracle_hcm"
    # Usually *.oraclecloud.com, but tenants may use their own domain (Dell: enterpriseplatform.dell.com).
    host: str = Field(pattern=r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")
    site_number: str = Field(pattern=r"^[A-Za-z0-9_]+$")  # e.g. CX_1001
    language: str = "en"
    india_location_id: str | None = None  # resolved by the adapter if absent (cached here)
    max_pages: int = Field(default=200, ge=1, le=1000)  # safety cap (25 per page); hitting it => incomplete


class EightfoldConfig(_Base):
    kind: Literal["eightfold"] = "eightfold"
    host: str  # e.g. apply.careers.microsoft.com, qualcomm.eightfold.ai
    domain: str  # e.g. microsoft.com
    variant: Literal["pcsx", "apply_v2"] = "pcsx"  # tenants enable different public endpoints
    location_query: str = "India"
    max_pages: int = Field(default=200, ge=1, le=1000)  # safety cap; hitting it => incomplete


# ───────────────────────────── generic / custom ─────────────────────────────


class JsonLdConfig(_Base):
    kind: Literal["jsonld"] = "jsonld"
    listing_url: HttpUrl
    job_link_selector: str | None = None
    sitemap_url: HttpUrl | None = None
    pagination_param: str | None = None
    max_pages: int = Field(default=20, ge=1, le=200)


class HtmlConfig(_Base):
    kind: Literal["html"] = "html"
    listing_url: HttpUrl
    item_selector: str
    fields: dict[str, str]  # {"title": "h3", "location": ".loc", "url": "a@href"}
    detail_fields: dict[str, str] = Field(default_factory=dict)
    pagination_param: str | None = None
    max_pages: int = Field(default=20, ge=1, le=200)

    @field_validator("fields")
    @classmethod
    def _required(cls, v: dict[str, str]) -> dict[str, str]:
        missing = {"title", "url"} - v.keys()
        if missing:
            raise ValueError(f"fields must include {sorted(missing)}")
        return v


class SuccessFactorsCsbConfig(_Base):
    kind: Literal["successfactors_csb"] = "successfactors_csb"
    base_url: HttpUrl  # e.g. https://careers.wipro.com
    sitemap_path: str = "/sitemap.xml"
    india_slug_pattern: str | None = None  # e.g. "-IND-" when the job URL encodes the country


class CustomConfig(_Base):
    max_pages: int = Field(default=100, ge=1, le=1000)
    kind: Literal["custom"] = "custom"
    module: str = Field(pattern=r"^[a-z0-9_]+$")  # app/discovery/adapters/custom/<module>.py
    options: dict[str, Any] = Field(default_factory=dict)


ExtractorConfig = Annotated[
    WorkdayConfig
    | GreenhouseConfig
    | LeverConfig
    | AshbyConfig
    | OracleHcmConfig
    | EightfoldConfig
    | JsonLdConfig
    | HtmlConfig
    | SuccessFactorsCsbConfig
    | CustomConfig,
    Field(discriminator="kind"),
]

CONFIG_MODELS: dict[str, type[BaseModel]] = {
    "workday": WorkdayConfig,
    "greenhouse": GreenhouseConfig,
    "lever": LeverConfig,
    "ashby": AshbyConfig,
    "oracle_hcm": OracleHcmConfig,
    "eightfold": EightfoldConfig,
    "jsonld": JsonLdConfig,
    "html": HtmlConfig,
    "successfactors_csb": SuccessFactorsCsbConfig,
    "custom": CustomConfig,
}


def validate_config(platform: str, config: dict[str, Any]) -> BaseModel:
    """Validate a career_sites.config document for `platform` (fills `kind`)."""
    model = CONFIG_MODELS[platform]
    return model.model_validate({**config, "kind": platform})
