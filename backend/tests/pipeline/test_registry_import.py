"""Registry import: mapping, idempotency, evidence, safety (small CSV written here + the real registry)."""

import csv
import json
from collections import Counter
from pathlib import Path

from sqlalchemy import func, select

from app.discovery.registry_import import import_registry, parse_registry
from app.models import CareerSite, Company
from app.models.enums import ExtractorKind, IndiaCoverage, SupportClass

REAL_CSV = Path(__file__).resolve().parents[3] / "data" / "companies.csv"
COLUMNS = [
    "company", "site_label", "careers_url", "landing_url", "platform", "tenant", "site_id", "api_endpoint",
    "support", "verification_status", "india_coverage", "monitor_by_default", "evidence_file", "notes", "verified_at",
]  # fmt: skip


def row(company, platform, **kw):
    base = dict.fromkeys(COLUMNS, "")
    base.update(company=company, site_label="main", platform=platform, support="auto", india_coverage="confirmed")
    base.update(verification_status="verified_live", monitor_by_default="true", verified_at="2026-11-11")
    base.update(kw)
    return base


SMALL = [
    row("Acme", "workday", careers_url="https://acme.wd5.myworkdayjobs.com/External"),
    row("Acme", "greenhouse", site_label="boards", tenant="acme", careers_url="https://acme.com/jobs"),
    row("Beta", "lever", tenant="beta", careers_url="https://jobs.lever.co/beta"),
    row("Gamma", "ashby", tenant="gamma", careers_url="https://jobs.ashbyhq.com/gamma"),
    row(
        "Delta", "oracle_hcm", tenant="delta", site_id="CX_1001",
        careers_url="https://delta.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001",
        api_endpoint="https://delta.fa.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions",
    ),
    row(
        "Eps", "eightfold", tenant="eps.com", careers_url="https://eps.eightfold.ai/careers",
        api_endpoint="https://eps.eightfold.ai/api/apply/v2/jobs?domain=eps.com&location=India",
    ),
    row(
        "Zeta", "eightfold", tenant="zeta.com", careers_url="https://zeta.eightfold.ai/careers",
        api_endpoint="https://zeta.eightfold.ai/api/pcsx/search?domain=zeta.com",
    ),
    row("Eta Systems", "custom_api", support="adapter", careers_url="https://eta.example/careers"),
    row("Theta", "custom_html", support="manual", india_coverage="unverified", careers_url="https://theta.example"),
    row("Iota", "greenhouse", tenant="", careers_url="https://iota.example/jobs"),  # invalid: no tenant
    row("Kappa", "workday", careers_url="https://kappa.example.com/jobs"),  # invalid: not a workday url
    row("Lambda", "lever", tenant="lambda", support="not_reliable", monitor_by_default="false"),
]  # fmt: skip


def write_csv(tmp_path, rows) -> Path:
    vdir = tmp_path / "verification"
    vdir.mkdir(exist_ok=True)
    (vdir / "group_x.json").write_text(
        json.dumps([{"company": "Acme", "site_label": "main", "verification": "verified_live", "notes": "evidence!"}])
    )
    path = tmp_path / "companies.csv"
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    return path


async def site_for(session, company, label="main") -> CareerSite:
    return await session.scalar(
        select(CareerSite).join(Company).where(Company.name == company, CareerSite.label == label)
    )


def test_parse_small_registry_mapping(tmp_path):
    rows = {(r.company, r.label): r for r in parse_registry(write_csv(tmp_path, SMALL))}
    assert rows[("Acme", "main")].config == {"kind": "workday", "url": "https://acme.wd5.myworkdayjobs.com/External",
                                              "india_strategy": "auto", "facet_param": None, "facet_cache": {},
                                              "split_facet_param": None, "max_pages": 150}  # fmt: skip
    assert rows[("Acme", "boards")].config["board_token"] == "acme"
    assert rows[("Beta", "main")].config["company"] == "beta"
    assert rows[("Gamma", "main")].config["org"] == "gamma"
    delta = rows[("Delta", "main")].config
    assert (delta["host"], delta["site_number"]) == ("delta.fa.oraclecloud.com", "CX_1001")
    eps, zeta = rows[("Eps", "main")].config, rows[("Zeta", "main")].config
    assert (eps["host"], eps["domain"], eps["variant"]) == ("eps.eightfold.ai", "eps.com", "apply_v2")
    assert (zeta["variant"], zeta["domain"]) == ("pcsx", "zeta.com")
    eta, theta = rows[("Eta Systems", "main")], rows[("Theta", "main")]
    assert (eta.platform, eta.config["module"], eta.support_class) == (
        ExtractorKind.custom,
        "eta_systems",
        SupportClass.adapter,
    )
    assert (theta.platform, theta.config, theta.support_class) == (ExtractorKind.none, {}, SupportClass.manual)
    assert not rows[("Iota", "main")].valid and "tenant" in rows[("Iota", "main")].error
    assert not rows[("Kappa", "main")].valid
    assert rows[("Lambda", "main")].support_class == SupportClass.not_reliable  # honoured, never upgraded
    assert rows[("Acme", "main")].evidence["verification"]["notes"] == "evidence!"
    assert rows[("Acme", "boards")].evidence["verification"] is None
    assert rows[("Acme", "main")].evidence["monitor_by_default"] is True
    assert rows[("Lambda", "main")].evidence["monitor_by_default"] is False


async def test_dry_run_writes_nothing_apply_imports_and_rerun_changes_nothing(session, tmp_path):
    path = write_csv(tmp_path, SMALL)
    companies_before = await session.scalar(select(func.count()).select_from(Company))
    dry = await import_registry(session, path)
    assert not dry.applied and (dry.rows, dry.valid_rows, len(dry.errors)) == (12, 10, 2)
    assert dry.companies_created == 9 and dry.sites_created == 10
    assert await session.scalar(select(func.count()).select_from(Company)) == companies_before
    assert await session.scalar(select(func.count()).select_from(CareerSite)) == 0

    applied = await import_registry(session, path, apply=True)
    assert applied.applied and (applied.companies_created, applied.sites_created) == (9, 10)
    sites = (await session.scalars(select(CareerSite))).all()
    assert len(sites) == 10 and all(s.monitoring_enabled is False and s.next_check_at is None for s in sites)
    acme = await site_for(session, "Acme")
    assert acme.platform == ExtractorKind.workday and acme.support_class == SupportClass.auto
    assert acme.india_coverage == IndiaCoverage.confirmed and acme.verification_status == "verified_live"
    assert acme.verified_at.date().isoformat() == "2026-11-11"
    assert acme.evidence["csv"]["company"] == "Acme" and acme.evidence["verification"]["notes"] == "evidence!"
    assert acme.evidence["monitor_by_default"] is True  # recorded, NOT acted on
    assert (await site_for(session, "Acme", "boards")).platform == ExtractorKind.greenhouse
    theta = await site_for(session, "Theta")
    assert theta.platform == ExtractorKind.none and theta.support_class == SupportClass.manual
    assert (await site_for(session, "Kappa")) is None
    company = await session.scalar(select(Company).where(Company.name == "Acme"))
    assert company.extractor == ExtractorKind.workday and not company.monitoring_enabled

    snapshot = {s.id: (s.updated_at, s.config, s.evidence) for s in sites}
    again = await import_registry(session, path, apply=True)
    assert (again.companies_created, again.sites_created, again.sites_updated, again.sites_unchanged) == (0, 0, 0, 10)
    await session.refresh(acme)
    assert {
        s.id: (s.updated_at, s.config, s.evidence) for s in (await session.scalars(select(CareerSite))).all()
    } == snapshot


async def test_reimport_updates_registry_fields_but_keeps_runtime_state_and_adapter_cache(session, tmp_path):
    path = write_csv(tmp_path, SMALL[:1])
    await import_registry(session, path, apply=True)
    acme = await site_for(session, "Acme")
    acme.monitoring_enabled, acme.health = True, "healthy"  # state set later by the operator / worker
    acme.config = {**acme.config, "facet_cache": {"locationCountry": ["abc"]}}
    await session.flush()
    again = await import_registry(session, path, apply=True)
    assert again.sites_unchanged == 1 and acme.config["facet_cache"] == {"locationCountry": ["abc"]}

    changed = [{**SMALL[0], "support": "not_reliable", "careers_url": "https://acme.wd5.myworkdayjobs.com/Other"}]
    again = await import_registry(session, write_csv(tmp_path, changed), apply=True)
    assert again.sites_updated == 1
    await session.refresh(acme)
    assert acme.support_class == SupportClass.not_reliable and acme.config["url"].endswith("/Other")
    assert acme.config["facet_cache"] == {}  # identity changed -> adapter cache reset
    assert acme.monitoring_enabled is True  # registry import never touches monitoring state


async def test_matches_existing_company_by_name_or_alias(session, tmp_path):
    from app.companies.service import insert_company

    existing = await insert_company(session, name="Beta Corporation", aliases=["Beta"])
    summary = await import_registry(session, write_csv(tmp_path, [SMALL[2]]), apply=True)
    assert summary.companies_created == 0 and summary.companies_matched == 1
    site = await session.scalar(select(CareerSite).where(CareerSite.company_id == existing.id))
    assert site is not None and site.platform == ExtractorKind.lever


def test_real_registry_parses_97_rows():
    rows = parse_registry(REAL_CSV)
    assert len(rows) == 97 and len({(r.company, r.label) for r in rows}) == 97
    valid = [r for r in rows if r.valid]
    assert len(valid) == 96
    bad = [r for r in rows if not r.valid]
    assert [(r.company, r.label) for r in bad] == [("Salesforce", "subsidiary_sites_checked")]  # placeholder, no site
    assert Counter(r.platform.value for r in valid) == {
        "none": 36,
        "workday": 22,
        "greenhouse": 12,
        "custom": 11,
        "eightfold": 6,
        "lever": 4,
        "oracle_hcm": 4,
        "ashby": 1,
    }
    assert all(r.evidence["csv"]["company"] == r.company for r in valid)
    assert sum(1 for r in valid if r.evidence["verification"]) >= 85
    assert all(r.config.get("kind") == r.platform.value for r in valid if r.platform != ExtractorKind.none)
    # bookmarklet-only classes are carried through from the registry, never upgraded
    assert {r.company for r in valid if r.support_class == SupportClass.not_reliable} >= {"Palo Alto Networks"}


async def test_real_registry_dry_run_then_apply_is_idempotent(session):
    before = await session.scalar(select(func.count()).select_from(CareerSite))
    dry = await import_registry(session, REAL_CSV)
    assert dry.rows == 97 and dry.valid_rows == 96 and len(dry.errors) == 1 and not dry.applied
    assert await session.scalar(select(func.count()).select_from(CareerSite)) == before

    applied = await import_registry(session, REAL_CSV, apply=True)
    assert applied.sites_created == 96 and applied.companies_created > 80
    monitored = await session.scalar(select(func.count()).select_from(CareerSite).where(CareerSite.monitoring_enabled))
    assert monitored == 0
    again = await import_registry(session, REAL_CSV, apply=True)
    assert (again.companies_created, again.sites_created, again.sites_updated) == (0, 0, 0)
    assert again.sites_unchanged == 96
