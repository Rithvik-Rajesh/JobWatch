"""Per-extractor configuration models (validate companies.extractor_config).

CENTRAL CONTRACT. Phase 2 validates and stores these; Phase 3 extractors consume them.
Location scope is NOT configured per company by default: the Workday country facet is derived from
the global LocationScopeSettings. `scope_override` exists for rare exceptions.
"""

import re
from typing import Annotated, Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator

WORKDAY_HOST_RE = re.compile(r"^(?P<tenant>[a-z0-9-]+)\.(?P<wd>wd\d+)\.myworkdayjobs\.com$", re.I)
LOCALE_RE = re.compile(r"^[a-z]{2}(-[A-Z]{2})?$")


class WorkdayConfig(BaseModel):
    kind: Literal["workday"] = "workday"
    url: HttpUrl  # https://{tenant}.wd{N}.myworkdayjobs.com/[locale/]{site}
    scope_override: Literal["global", "all_locations"] = "global"
    search_text: str | None = None
    title_include: list[str] = Field(default_factory=list)
    title_exclude: list[str] = Field(default_factory=list)
    facet_cache: dict[str, list[str]] = Field(default_factory=dict)  # written by the extractor

    @field_validator("url")
    @classmethod
    def _workday_url(cls, v: HttpUrl) -> HttpUrl:
        parse_workday_url(str(v))
        return v


class WorkdaySite(BaseModel):
    host: str
    tenant: str
    site: str

    @property
    def api_base(self) -> str:
        return f"https://{self.host}/wday/cxs/{self.tenant}/{self.site}"


def parse_workday_url(url: str) -> WorkdaySite:
    from urllib.parse import urlparse

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


class JsonLdConfig(BaseModel):
    kind: Literal["jsonld"] = "jsonld"
    listing_url: HttpUrl
    job_link_selector: str | None = None
    sitemap_url: HttpUrl | None = None
    pagination_param: str | None = None
    max_pages: int = Field(default=20, ge=1, le=200)
    render: bool = False


class HtmlConfig(BaseModel):
    kind: Literal["html"] = "html"
    listing_url: HttpUrl
    item_selector: str
    fields: dict[str, str]  # {"title": "h3", "location": ".loc", "url": "a@href"}
    detail_fields: dict[str, str] = Field(default_factory=dict)
    pagination_param: str | None = None
    max_pages: int = Field(default=20, ge=1, le=200)
    render: bool = False

    @field_validator("fields")
    @classmethod
    def _required(cls, v: dict[str, str]) -> dict[str, str]:
        missing = {"title", "url"} - v.keys()
        if missing:
            raise ValueError(f"fields must include {sorted(missing)}")
        return v


class CustomConfig(BaseModel):
    kind: Literal["custom"] = "custom"
    module: str = Field(pattern=r"^[a-z0-9_]+$")  # app/discovery/extractors/custom/<module>.py
    options: dict[str, str] = Field(default_factory=dict)


ExtractorConfig = Annotated[WorkdayConfig | JsonLdConfig | HtmlConfig | CustomConfig, Field(discriminator="kind")]
