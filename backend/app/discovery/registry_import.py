"""Import the verified company registry (data/companies.csv + data/verification/group_*.json) into
companies + career_sites.

    uv run python -m app.discovery.registry_import ../data/companies.csv [--apply]

Dry-run by default (everything is computed through the real code path, then rolled back). Rules:
  * One company per distinct name (matched by name/alias like the company service) and one career site per CSV row
    (label = site_label).
  * EVERY site is imported with monitoring_enabled=false regardless of `monitor_by_default`; that flag is kept
    in `evidence` for the later, approved rollout. support_class / india_coverage / verification fields and the
    full evidence (CSV row + matching verification record) are stored on the site.
  * Idempotent: re-running changes nothing. Existing sites are only refreshed from the registry (platform, config,
    URLs, verification fields, evidence); health, monitoring and scheduling state are never touched, and config
    values cached by adapters (facet ids etc.) are preserved while the portal identity is unchanged.
  * Rows whose platform config fails validation are reported, not imported.
"""

import argparse
import asyncio
import csv
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.companies import service as company_service
from app.core.errors import ValidationFailed
from app.models import CareerSite, Company, CompanyAlias
from app.models.enums import ExtractorKind, IndiaCoverage, SupportClass
from app.sites import service as sites_service

# config keys written by adapters at runtime; they never count as a registry change
CACHE_KEYS = {"facet_cache", "india_location_id"}
SHARED_PLATFORMS = {"workday", "greenhouse", "lever", "ashby", "oracle_hcm", "eightfold"}


@dataclass
class RegistryRow:
    row_number: int
    company: str
    label: str
    platform: ExtractorKind = ExtractorKind.none
    config: dict[str, Any] = field(default_factory=dict)
    careers_url: str | None = None
    support_class: SupportClass = SupportClass.manual
    india_coverage: IndiaCoverage = IndiaCoverage.unverified
    verification_status: str | None = None
    verified_at: datetime | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def valid(self) -> bool:
        return self.error is None


@dataclass
class ImportSummary:
    rows: int = 0
    valid_rows: int = 0
    errors: list[str] = field(default_factory=list)
    companies_created: int = 0
    companies_matched: int = 0
    sites_created: int = 0
    sites_updated: int = 0
    sites_unchanged: int = 0
    platforms: Counter = field(default_factory=Counter)
    applied: bool = False

    def render(self) -> str:
        lines = [
            f"Registry import ({'APPLIED' if self.applied else 'dry run - nothing written'})",
            f"  rows: {self.rows}  valid: {self.valid_rows}  rejected: {len(self.errors)}",
            f"  companies: {self.companies_created} created, {self.companies_matched} matched",
            f"  sites: {self.sites_created} created, {self.sites_updated} updated, {self.sites_unchanged} unchanged",
            "  platforms: " + ", ".join(f"{k}={v}" for k, v in sorted(self.platforms.items())),
            "  all sites imported with monitoring disabled",
        ]
        lines += [f"  REJECTED {e}" for e in self.errors]
        return "\n".join(lines)


# ------------------------------------------------------------------------------------------- parsing
def _clean(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "site"


def _host(url: str | None) -> str | None:
    return urlparse(url).hostname if url else None


def map_config(row: dict[str, str]) -> tuple[ExtractorKind, dict[str, Any]]:
    """CSV row -> (platform, raw config). Raises ValueError when a required column is missing."""
    platform = (row.get("platform") or "").strip()
    tenant, site_id = _clean(row.get("tenant")), _clean(row.get("site_id"))
    api, careers = _clean(row.get("api_endpoint")), _clean(row.get("careers_url"))

    def need(value: str | None, what: str) -> str:
        if not value:
            raise ValueError(f"{platform}: missing {what}")
        return value

    match platform:
        case "workday":
            return ExtractorKind.workday, {"url": need(careers, "careers_url")}
        case "greenhouse":
            return ExtractorKind.greenhouse, {"board_token": need(tenant, "tenant")}
        case "lever":
            return ExtractorKind.lever, {"company": need(tenant, "tenant")}
        case "ashby":
            return ExtractorKind.ashby, {"org": need(tenant, "tenant")}
        case "oracle_hcm":
            host = _host(api) or _host(careers) or tenant
            return ExtractorKind.oracle_hcm, {"host": need(host, "host"), "site_number": need(site_id, "site_id")}
        case "eightfold":
            host = _host(api) or _host(careers)
            query = parse_qs(urlparse(api).query) if api else {}
            domain = (query.get("domain") or [None])[0] or tenant
            path = urlparse(api).path if api else ""
            variant = "apply_v2" if ("/apply/" in path or "apply_v2" in path) and "/pcsx/" not in path else "pcsx"
            return ExtractorKind.eightfold, {
                "host": need(host, "host"),
                "domain": need(domain, "domain"),
                "variant": variant,
            }
    return ExtractorKind.none, {}


def load_verification(verification_dir: Path) -> dict[tuple[str, str], dict[str, Any]]:
    records: dict[tuple[str, str], dict[str, Any]] = {}
    for path in sorted(verification_dir.glob("group_*.json")):
        for rec in json.loads(path.read_text()):
            records[(rec["company"], rec.get("site_label") or "main")] = rec
    return records


def parse_registry(csv_path: Path, verification_dir: Path | None = None) -> list[RegistryRow]:
    """Pure (no database): CSV rows -> validated RegistryRow objects (invalid ones carry `error`)."""
    verification = load_verification(verification_dir or csv_path.parent / "verification")
    out: list[RegistryRow] = []
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        for i, row in enumerate(csv.DictReader(f), start=2):
            company, label = (row.get("company") or "").strip(), (row.get("site_label") or "main").strip() or "main"
            item = RegistryRow(row_number=i, company=company, label=label)
            out.append(item)
            if not company:
                item.error = "missing company name"
                continue
            try:
                item.platform, raw = map_config(row)
                item.support_class = SupportClass((row.get("support") or "").strip())
                item.india_coverage = IndiaCoverage((row.get("india_coverage") or "").strip())
                if item.platform == ExtractorKind.none and item.support_class == SupportClass.adapter:
                    item.platform = ExtractorKind.custom
                    raw = {"module": _slug(company if label == "main" else f"{company}_{label}")}
                item.config = sites_service.validated_config(item.platform, raw)
            except (ValueError, ValidationFailed, ValidationError) as e:
                item.error = str(e).splitlines()[0] if isinstance(e, ValueError) else f"invalid config: {e}"
                continue
            item.careers_url = _clean(row.get("careers_url"))
            item.verification_status = _clean(row.get("verification_status"))
            verified = _clean(row.get("verified_at"))
            item.verified_at = datetime.fromisoformat(verified).replace(tzinfo=UTC) if verified else None
            item.evidence = {
                "csv": dict(row),
                "verification": verification.get((company, label)),
                "monitor_by_default": (row.get("monitor_by_default") or "").strip().lower() == "true",
            }
    return out


# --------------------------------------------------------------------------------------------- import
async def _find_company(session: AsyncSession, name: str) -> Company | None:
    low = name.lower()
    company = await session.scalar(select(Company).where(func.lower(Company.name) == low))
    if company is None:
        company = await session.scalar(
            select(Company)
            .join(CompanyAlias, CompanyAlias.company_id == Company.id)
            .where(func.lower(CompanyAlias.alias) == low)
        )
    return company


def _core(config: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in config.items() if k not in CACHE_KEYS}


async def _apply_rows(session: AsyncSession, rows: list[RegistryRow], summary: ImportSummary) -> None:
    seen_companies: set[int] = set()
    for item in rows:
        if not item.valid:
            summary.errors.append(f"row {item.row_number} ({item.company} / {item.label}): {item.error}")
            continue
        summary.valid_rows += 1
        summary.platforms[item.platform.value] += 1
        company = await _find_company(session, item.company)
        if company is None:
            company = await company_service.insert_company(session, name=item.company, careers_url=item.careers_url)
            summary.companies_created += 1
        elif company.id not in seen_companies:
            summary.companies_matched += 1
        seen_companies.add(company.id)

        site = await session.scalar(
            select(CareerSite).where(
                CareerSite.company_id == company.id, func.lower(CareerSite.label) == item.label.lower()
            )
        )
        if site is None:
            await sites_service.create_site_row(
                session,
                company,
                label=item.label,
                platform=item.platform,
                config=item.config,
                careers_url=item.careers_url,
                monitoring_enabled=False,
                support_class=item.support_class,
                india_coverage=item.india_coverage,
                verification_status=item.verification_status,
                verified_at=item.verified_at,
                evidence=item.evidence,
            )
            summary.sites_created += 1
        else:
            desired: dict[str, Any] = {
                "platform": item.platform,
                "careers_url": item.careers_url or sites_service.derive_careers_url(item.platform, item.config),
                "support_class": item.support_class,
                "india_coverage": item.india_coverage,
                "verification_status": item.verification_status,
                "verified_at": item.verified_at,
                "evidence": item.evidence,
            }
            if _core(site.config) != _core(item.config):
                desired["config"] = item.config
            changed = False
            for attr, value in desired.items():
                if getattr(site, attr) != value:
                    setattr(site, attr, value)
                    changed = True
            if changed:
                summary.sites_updated += 1
            else:
                summary.sites_unchanged += 1
        await session.flush()
        await sites_service.sync_company_summary(session, company.id)


async def import_registry(
    session: AsyncSession, csv_path: Path, *, apply: bool = False, verification_dir: Path | None = None
) -> ImportSummary:
    """Run the import inside a savepoint; it is rolled back unless `apply` (the caller commits the outer
    transaction)."""
    rows = parse_registry(csv_path, verification_dir)
    summary = ImportSummary(rows=len(rows), applied=apply)
    nested = await session.begin_nested()
    try:
        await _apply_rows(session, rows, summary)
    except BaseException:
        await nested.rollback()
        raise
    if apply:
        await nested.commit()
    else:
        await nested.rollback()
    return summary


async def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Import data/companies.csv into companies + career_sites")
    parser.add_argument("csv", type=Path)
    parser.add_argument("--apply", action="store_true", help="write to the database (default: dry run)")
    args = parser.parse_args(argv)

    from app.core.db import dispose_engine, get_sessionmaker

    try:
        async with get_sessionmaker()() as session:
            summary = await import_registry(session, args.csv, apply=args.apply)
            if args.apply:
                await session.commit()
        print(summary.render())
    finally:
        await dispose_engine()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
