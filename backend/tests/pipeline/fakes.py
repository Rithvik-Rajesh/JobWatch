"""Fake adapter + helpers for pipeline tests (no network)."""

from datetime import UTC, datetime
from typing import Any, ClassVar

from pydantic import BaseModel

from app.discovery.adapters.base import DetailRecord, IndiaQuery, ListedPosting, ListingResult
from app.discovery.configs import CustomConfig
from app.models import CareerSite
from app.models.enums import ExtractorKind

T0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


class FakeAdapter:
    """Scripted adapter. State is class-level (the registry instantiates a fresh object per run); the `fake_adapter`
    fixture resets it for every test."""

    platform: ClassVar[str] = "custom:fake"
    version: ClassVar[str] = "fake-1"
    Config: ClassVar[type[BaseModel]] = CustomConfig

    result: ClassVar[ListingResult | Exception] = ListingResult(postings=[], complete=True)
    detail_calls: ClassVar[list[str]] = []
    detail_error: ClassVar[Exception | None] = None
    list_calls: ClassVar[list[Any]] = []
    cache_value: ClassVar[Any] = None  # written onto cfg.options during list() like adapters cache facet ids

    @classmethod
    def reset(cls) -> None:
        cls.result = ListingResult(postings=[], complete=True)
        cls.detail_calls = []
        cls.detail_error = None
        cls.list_calls = []
        cls.cache_value = None

    async def list(self, config: CustomConfig, india: IndiaQuery, http: Any) -> ListingResult:
        FakeAdapter.list_calls.append((config.model_copy(deep=True), india))
        if self.cache_value is not None:
            config.options["cached"] = self.cache_value
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def detail(self, config: CustomConfig, posting: ListedPosting, http: Any) -> DetailRecord:
        FakeAdapter.detail_calls.append(posting.external_id)
        if self.detail_error is not None:
            raise self.detail_error
        return DetailRecord(
            description_text=f"Description of {posting.title}. 3+ years of experience required.",
            apply_url=f"{posting.url}/apply",
        )

    @classmethod
    def matches_url(cls, url: str) -> bool:
        return False


def lp(ext: str, title: str = "Software Engineer", location: str | None = "Pune, India", **kw: Any) -> ListedPosting:
    kw.setdefault("fingerprint", f"fp-{ext}-{title}")
    return ListedPosting(
        external_id=ext,
        title=title,
        url=f"https://careers.example.com/jobs/{ext}",
        location_texts=[location] if location else [],
        **kw,
    )


def listing(*postings: ListedPosting, complete: bool = True) -> ListingResult:
    return ListingResult(postings=list(postings), complete=complete, pages_fetched=1)


async def make_site(session, company, label: str = "main", *, monitoring: bool = False, **kw: Any) -> CareerSite:
    site = CareerSite(
        company_id=company.id,
        label=label,
        platform=ExtractorKind.custom,
        config={"kind": "custom", "module": "fake", "options": {}},
        careers_url="https://careers.example.com",
        monitoring_enabled=monitoring,
        **kw,
    )
    session.add(site)
    await session.flush()
    return site
