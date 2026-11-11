"""CSV company import: preview (parse + classify, no writes, no network) and commit (one transaction).

Previews live in a module-level dict keyed by a random token with a 30-minute TTL. JobWatch is a
single-process personal app, so process-local storage is acceptable; a restart or a second worker
simply loses pending previews (the user re-uploads). A token can be committed only once.
"""

import csv
import io
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from pydantic import HttpUrl, TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.companies.schemas import CsvImportPreview, CsvImportResult, CsvImportRow
from app.companies.service import add_aliases, clean_aliases, config_url, insert_company, slugify
from app.core.errors import Conflict, NotFound, ValidationFailed
from app.discovery.configs import WorkdayConfig, parse_workday_url
from app.models import Company, CompanyAlias
from app.models.enums import ExtractorKind

MAX_ROWS = 2000
TTL = timedelta(minutes=30)
_TRUE = {"true", "yes", "1", "y"}
_FALSE = {"false", "no", "0", "n"}
_URL = TypeAdapter(HttpUrl)


@dataclass
class _PlannedRow:
    row_number: int
    action: str
    name: str | None = None
    careers_url: str | None = None
    website_url: str | None = None
    monitoring_enabled: bool | None = None
    aliases: list[str] = field(default_factory=list)
    workday: bool = False
    existing_company_id: int | None = None
    message: str | None = None


@dataclass
class _Preview:
    created_at: datetime
    rows: list[_PlannedRow]
    committed: bool = False


_PREVIEWS: dict[str, _Preview] = {}


def _purge(now: datetime) -> None:
    for token in [t for t, p in _PREVIEWS.items() if now - p.created_at > TTL]:
        del _PREVIEWS[token]


def _url(value: str | None, label: str) -> str | None:
    if not value:
        return None
    try:
        return str(_URL.validate_python(value))
    except ValidationError as e:
        raise ValueError(f"{label} is not a valid http(s) URL") from e


def _bool(value: str | None) -> bool | None:
    if value is None or not value.strip():
        return None
    v = value.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise ValueError(f"monitoring_enabled '{value}' must be true/false/yes/no/1/0")


def _parse_csv(data: bytes) -> list[tuple[int, dict[str, str]]]:
    try:
        content = data.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise ValidationFailed("CSV must be UTF-8 encoded") from e
    reader = csv.reader(io.StringIO(content))
    header = next(reader, None)
    if not header or not any(h.strip() for h in header):
        raise ValidationFailed("CSV is empty: a header row is required")
    cols = [h.strip().lower() for h in header]
    if "name" not in cols:
        raise ValidationFailed("CSV header must include a 'name' column")
    rows: list[tuple[int, dict[str, str]]] = []
    for line_no, raw in enumerate(reader, start=2):
        if not any(cell.strip() for cell in raw):
            continue  # blank line
        if len(rows) >= MAX_ROWS:
            raise ValidationFailed(f"CSV has more than {MAX_ROWS} rows")
        rows.append((line_no, {c: (raw[i].strip() if i < len(raw) else "") for i, c in enumerate(cols)}))
    return rows


async def preview_csv(session: AsyncSession, data: bytes, *, now: datetime | None = None) -> CsvImportPreview:
    now = now or datetime.now(UTC)
    _purge(now)
    parsed = _parse_csv(data)

    companies = (await session.scalars(select(Company))).all()
    by_id = {c.id: c for c in companies}
    by_slug = {c.slug.lower(): c for c in companies}
    by_name = {c.name.lower(): c for c in companies}
    alias_rows = (await session.execute(select(CompanyAlias.company_id, CompanyAlias.alias))).all()
    by_alias = {a.lower(): cid for cid, a in alias_rows}
    aliases_of: dict[int, set[str]] = {}
    for cid, a in alias_rows:
        aliases_of.setdefault(cid, set()).add(a.lower())

    seen_names: dict[str, int] = {}  # names/aliases claimed earlier in this file -> row number
    planned: list[_PlannedRow] = []
    for line_no, rec in parsed:
        row = _PlannedRow(row_number=line_no, action="error")
        planned.append(row)
        row.name = rec.get("name") or None
        row.careers_url = rec.get("careers_url") or None
        row.website_url = rec.get("website_url") or None
        try:
            if not row.name:
                raise ValueError("name is required")
            row.careers_url = _url(row.careers_url, "careers_url")
            row.website_url = _url(row.website_url, "website_url")
            row.monitoring_enabled = _bool(rec.get("monitoring_enabled"))
            row.aliases = clean_aliases(row.name, [a for a in rec.get("aliases", "").split(";")])
            if row.careers_url:
                try:
                    parse_workday_url(row.careers_url)
                    row.workday = True
                except ValueError:
                    pass
        except ValueError as e:
            row.message = str(e)
            continue

        key = row.name.lower()
        existing = by_slug.get(slugify(row.name)) or by_name.get(key)
        if existing is None and key in by_alias:
            existing = by_id[by_alias[key]]
        row.existing_company_id = existing.id if existing else None

        claimed = [key, *[a.lower() for a in row.aliases]]
        clash = next((seen_names[c] for c in claimed if c in seen_names), None)
        if clash is not None:
            row.message = f"Duplicate of row {clash} in this file"
            continue
        # aliases must not belong to / equal the name of a different company
        bad = None
        for a in (x.lower() for x in row.aliases):
            owner = by_name.get(a)
            owner_id = owner.id if owner else by_alias.get(a)
            if owner_id is not None and owner_id != row.existing_company_id:
                bad = a
                break
        if bad:
            row.message = f"Alias '{bad}' already belongs to another company"
            continue
        for c in claimed:
            seen_names[c] = line_no

        if existing is None:
            row.action = "create"
            continue
        changed = False
        if row.careers_url and row.careers_url != existing.careers_url:
            changed = True
        if row.website_url and row.website_url != existing.website_url:
            changed = True
        if row.monitoring_enabled is not None and row.monitoring_enabled != existing.monitoring_enabled:
            changed = True
        if any(a.lower() not in aliases_of.get(existing.id, set()) for a in row.aliases):
            changed = True
        if row.workday and (
            existing.extractor != ExtractorKind.workday or existing.extractor_config.get("url") != row.careers_url
        ):
            changed = True
        row.action = "update" if changed else "skip"

    token = secrets.token_urlsafe(24)
    _PREVIEWS[token] = _Preview(created_at=now, rows=planned)
    counts = {"create": 0, "update": 0, "skip": 0, "error": 0}
    for r in planned:
        counts[r.action] += 1
    return CsvImportPreview(
        token=token,
        counts=counts,
        rows=[
            CsvImportRow(
                row_number=r.row_number,
                name=r.name,
                careers_url=r.careers_url,
                website_url=r.website_url,
                action=r.action,
                detected_extractor=ExtractorKind.workday if r.workday else ExtractorKind.none,
                message=r.message,
                existing_company_id=r.existing_company_id,
            )
            for r in planned
        ],
    )


async def commit_csv(session: AsyncSession, token: str, *, now: datetime | None = None) -> CsvImportResult:
    now = now or datetime.now(UTC)
    _purge(now)
    preview = _PREVIEWS.get(token)
    if preview is None:
        raise NotFound("Import preview not found or expired")
    if preview.committed:
        raise Conflict("This import has already been committed")

    created = updated = skipped = errors = 0
    ids: list[int] = []
    for row in preview.rows:
        if row.action == "error":
            errors += 1
        elif row.action == "skip":
            skipped += 1
            if row.existing_company_id:
                ids.append(row.existing_company_id)
        elif row.action == "create":
            config = WorkdayConfig(url=row.careers_url) if row.workday else None  # type: ignore[arg-type]
            company = await insert_company(
                session,
                name=row.name or "",
                website_url=row.website_url,
                careers_url=row.careers_url,
                monitoring_enabled=bool(row.monitoring_enabled),
                config=config,
                aliases=row.aliases,
                now=now,
            )
            created += 1
            ids.append(company.id)
        else:  # update
            company = await session.get(Company, row.existing_company_id)
            if company is None:
                raise Conflict(f"Company for row {row.row_number} no longer exists; re-upload the file")
            if row.careers_url:
                company.careers_url = row.careers_url
            if row.website_url:
                company.website_url = row.website_url
            if row.workday:
                cfg = WorkdayConfig(url=row.careers_url)  # type: ignore[arg-type]
                company.extractor = ExtractorKind.workday
                company.extractor_config = cfg.model_dump(mode="json")
                company.careers_url = company.careers_url or config_url(cfg)
            if row.monitoring_enabled is not None:
                company.monitoring_enabled = row.monitoring_enabled
            if (
                company.monitoring_enabled
                and company.extractor != ExtractorKind.none
                and company.archived_at is None
                and company.next_check_at is None
            ):
                company.next_check_at = now
            if not company.monitoring_enabled:
                company.next_check_at = None
            await session.flush()
            await add_aliases(session, company, row.aliases)
            updated += 1
            ids.append(company.id)

    preview.committed = True
    return CsvImportResult(created=created, updated=updated, skipped=skipped, errors=errors, company_ids=ids)
