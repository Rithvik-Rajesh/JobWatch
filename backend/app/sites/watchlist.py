"""Company scraper inventory and bounded, read-only scrape probes."""

from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter
from sqlalchemy import select

from app.core.auth import CurrentUser
from app.core.db import SessionDep
from app.core.errors import Conflict, NotFound
from app.discovery.registry_import import import_registry, parse_registry
from app.models import CareerSite, Company, CompanyAlias
from app.sites import service

router = APIRouter(prefix="/settings/scrapers", tags=["scraper settings"])
DATA = Path(__file__).resolve().parents[3] / "data"


def watchlist() -> list[str]:
    return (DATA / "watchlist.txt").read_text(encoding="utf-8").splitlines()


@router.get("")
async def scraper_inventory(session: SessionDep, user: CurrentUser):
    names = watchlist()
    companies = {company.name.casefold(): company for company in await session.scalars(select(Company))}
    by_id = {company.id: company for company in companies.values()}
    for alias in await session.scalars(select(CompanyAlias)):
        companies.setdefault(alias.alias.casefold(), by_id[alias.company_id])
    sites_by_company: dict[int, list[CareerSite]] = {}
    for site in await session.scalars(select(CareerSite).order_by(CareerSite.label)):
        sites_by_company.setdefault(site.company_id, []).append(site)
    registry = {}
    for row in parse_registry(DATA / "companies.csv"):
        registry.setdefault(row.company, []).append(row)

    result = []
    for name in names:
        company = companies.get(name.casefold())
        sites = sites_by_company.get(company.id, []) if company else []
        portals = []
        for site in sites:
            supported = site.support_class in service.AUTOMATABLE and service.adapter_available(
                site.platform, site.config
            )
            checkable = supported and site.archived_at is None and company.archived_at is None
            evidence = site.evidence or {}
            portals.append(
                {
                    "id": site.id,
                    "label": site.label,
                    "platform": site.platform.value,
                    "checkable": checkable,
                    "status": "unchecked" if checkable else "unavailable",
                    "reason": "Archived"
                    if supported and not checkable
                    else (evidence.get("csv", {}).get("support_reason") or "No supported adapter for this portal."),
                    "check": evidence.get("scraper_check") if checkable else None,
                }
            )
        registered = {site.label.casefold() for site in sites}
        for row in registry.get(name, []):
            if row.label.casefold() in registered:
                continue
            portals.append(
                {
                    "id": None,
                    "label": row.label,
                    "platform": row.platform.value,
                    "checkable": False,
                    "status": "not_added",
                    "reason": row.error or row.evidence.get("csv", {}).get("support_reason"),
                    "check": None,
                }
            )
        result.append({"company": name, "sites": portals})
    return result


@router.post("/install")
async def install_watchlist(session: SessionDep, user: CurrentUser):
    result = await import_registry(
        session, DATA / "companies.csv", apply=True, companies=set(watchlist()), preserve_existing=True
    )
    summary = asdict(result)
    summary["platforms"] = dict(result.platforms)
    return summary


@router.post("/{site_id}/check")
async def check_scraper(site_id: int, session: SessionDep, user: CurrentUser):
    site = await session.get(CareerSite, site_id)
    if site is None:
        raise NotFound("Career site not found")
    company = await session.get(Company, site.company_id)
    if site.archived_at is not None or (company and company.archived_at is not None):
        raise Conflict("Career site is archived")
    if site.support_class not in service.AUTOMATABLE or not service.adapter_available(site.platform, site.config):
        raise Conflict("No supported scraper for this career site")

    result = await service.test_site(session, site_id)
    probe = result.diagnostics.get("detail_probe", {})
    checked = {
        "ok": result.ok and (not probe.get("attempted") or probe.get("ok") is True),
        "at": datetime.now(UTC).isoformat(),
        "sampled": result.postings_seen,
        "detail_checked": bool(probe.get("attempted")),
        "reason": result.error_message or probe.get("error"),
    }
    site.evidence = {**(site.evidence or {}), "scraper_check": checked}
    return checked
