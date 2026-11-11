"""Bookmarklet capture: build a PostingInput from the page data the browser sent (JSON-LD JobPosting,
meta tags, selected text). The URL is NEVER fetched."""

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.applications.lifecycle import status_by_key
from app.core.errors import ValidationFailed
from app.discovery.contracts import LocationInput, PostingInput
from app.discovery.ingest import canonical_apply_url, ingest_service
from app.discovery.normalize import country_for, html_to_text, parse_employment_type, sanitize_html
from app.jobs.schemas import CaptureRequest, CaptureResult
from app.jobs.service import change_initial_status, resolve_company
from app.models import Company, Job, Posting, Source
from app.models.enums import DatePrecision, WorkArrangement

UNKNOWN_COMPANY = "Unknown company"


def find_job_posting(json_ld: list[Any] | None) -> dict[str, Any] | None:
    """First schema.org JobPosting object in the blocks (handles @graph, nested lists, @type arrays)."""

    def is_job(obj: dict[str, Any]) -> bool:
        t = obj.get("@type")
        return t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t)

    def walk(node: Any) -> dict[str, Any] | None:
        if isinstance(node, list):
            for child in node:
                found = walk(child)
                if found:
                    return found
        elif isinstance(node, dict):
            if is_job(node):
                return node
            for key in ("@graph", "mainEntity", "itemListElement"):
                if key in node:
                    found = walk(node[key])
                    if found:
                        return found
        return None

    return walk(json_ld or [])


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        return _text(value.get("name") or value.get("value"))
    if isinstance(value, list):
        for v in value:
            t = _text(v)
            if t:
                return t
    if isinstance(value, int | float):
        return str(value)
    return None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _parse_dt(value: Any) -> datetime | None:
    s = _text(value)
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _locations(jp: dict[str, Any]) -> list[LocationInput]:
    out: list[LocationInput] = []
    for loc in _as_list(jp.get("jobLocation")):
        addr = loc.get("address") if isinstance(loc, dict) else None
        if isinstance(addr, str):
            raw = addr.strip()
            if raw:
                out.append(LocationInput(raw=raw))
            continue
        if not isinstance(addr, dict):
            continue
        city = _text(addr.get("addressLocality"))
        region = _text(addr.get("addressRegion"))
        country_raw = _text(addr.get("addressCountry"))
        country = country_for(country_raw) if country_raw else None
        raw = ", ".join(p for p in (city, region, country_raw) if p)
        if raw:
            out.append(LocationInput(raw=raw, city=city, region=region, country_code=country))
    for req in _as_list(jp.get("applicantLocationRequirements")):
        name = _text(req)
        if name:
            out.append(LocationInput(raw=name, country_code=country_for(name)))
    return out


def _salary_text(jp: dict[str, Any]) -> str | None:
    base = jp.get("baseSalary")
    if not isinstance(base, dict):
        return _text(base)
    currency = _text(base.get("currency")) or ""
    value = base.get("value")
    if isinstance(value, dict):
        lo, hi, single = value.get("minValue"), value.get("maxValue"), value.get("value")
        unit = (_text(value.get("unitText")) or "YEAR").lower()
    else:
        lo = hi = None
        single, unit = value, "year"

    def fmt(x: Any) -> str | None:
        try:
            return f"{Decimal(str(x)):,.0f}"
        except Exception:
            return None

    lo_s, hi_s, one = fmt(lo), fmt(hi), fmt(single)
    period = {"annual": "year", "yearly": "year"}.get(unit, unit)
    if lo_s and hi_s:
        return f"{currency} {lo_s} - {hi_s} per {period}".strip()
    if one or lo_s or hi_s:
        return f"{currency} {one or lo_s or hi_s} per {period}".strip()
    return None


async def capture_job(session: AsyncSession, body: CaptureRequest, *, now: datetime | None = None) -> CaptureResult:
    now = now or datetime.now(UTC)
    if body.initial_status and body.initial_status != "new":
        await status_by_key(session, body.initial_status)
    jp = find_job_posting(body.json_ld) or {}
    meta = {k.lower(): v for k, v in body.meta.items()}
    needs_review: list[str] = []

    title = _text(jp.get("title")) or _text(jp.get("name")) or meta.get("og:title") or (body.page_title or "").strip()
    if not title:
        raise ValidationFailed("Could not determine a job title from the page", code="capture_no_title")

    company_name = _text(jp.get("hiringOrganization"))
    if not company_name:
        needs_review.append("company")
        company_name = meta.get("og:site_name") or UNKNOWN_COMPANY

    locations = _locations(jp)
    location_text = "; ".join(loc.raw for loc in locations) or None
    if not locations:
        needs_review.append("location")

    work_arrangement = WorkArrangement.remote if _text(jp.get("jobLocationType")) == "TELECOMMUTE" else None

    desc_raw = _text(jp.get("description"))
    description_html = sanitize_html(desc_raw) if desc_raw and "<" in desc_raw else None
    description_text = html_to_text(desc_raw) if description_html else desc_raw
    if not description_text:
        description_text = (body.selected_text or "").strip() or meta.get("og:description") or meta.get("description")
    if not description_text:
        needs_review.append("description")

    date_posted = _parse_dt(jp.get("datePosted"))
    identifier = jp.get("identifier")
    requisition = _text(identifier.get("value")) if isinstance(identifier, dict) else _text(identifier)

    page_url = str(body.page_url)
    canonical = canonical_apply_url(page_url) or page_url
    external_id = hashlib.sha256(canonical.encode()).hexdigest()

    capture_source = await session.scalar(select(Source).where(Source.key == "capture"))
    if capture_source is None:
        raise ValidationFailed("Capture source is not configured", code="unknown_source")
    existing = await session.scalar(
        select(Posting).where(Posting.source_id == capture_source.id, Posting.external_id == external_id)
    )
    if existing is not None:
        company_id = existing.company_id
    else:
        company_id = (await resolve_company(session, company_name=company_name)).id

    data = PostingInput(
        source_key="capture",
        company_id=company_id,
        external_id=external_id,
        requisition_id=requisition,
        source_url=page_url,
        apply_url=page_url,
        title=title,
        department=_text(jp.get("occupationalCategory")),
        location_text=location_text,
        locations=locations or None,
        employment_type=parse_employment_type(
            [t for t in _as_list(jp.get("employmentType")) if isinstance(t, str)] or None
        ),
        work_arrangement=work_arrangement,
        salary_text=_salary_text(jp),
        posted_at=date_posted,
        posted_at_precision=DatePrecision.day if date_posted else DatePrecision.unknown,
        posted_at_raw=_text(jp.get("datePosted")),
        deadline_at=_parse_dt(jp.get("validThrough")),
        description_html=description_html,
        description_text=description_text,
        raw_payload={
            "origin": "bookmarklet",
            "page_url": page_url,
            "page_title": body.page_title,
            "meta": body.meta,
            "had_json_ld": bool(jp),
            "selected_text": (body.selected_text or "")[:2000] or None,
        },
    )
    outcome = await ingest_service.ingest(session, data, seen_at=now)
    if outcome.created_job:
        await change_initial_status(session, outcome.job_id, body.initial_status, now=now)

    job = await session.get(Job, outcome.job_id)
    assert job is not None
    company = await session.get(Company, job.company_id)
    assert company is not None
    if existing is not None or outcome.attached_to_existing_job:
        # company/location were not (re)derived for an already-known job: only flag what is still missing
        needs_review = [
            f
            for f in needs_review
            if (f == "company" and company.name == UNKNOWN_COMPANY)
            or (f == "location" and not job.country_codes and not job.location_text)
            or (f == "description" and not job.description_text)
        ]
    return CaptureResult(
        job_id=job.id,
        created=outcome.created_job,
        title=job.title,
        company_name=str(company.name),
        needs_review=needs_review,
        app_url=f"/jobs/{job.id}",
    )
