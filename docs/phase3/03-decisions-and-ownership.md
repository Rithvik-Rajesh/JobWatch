# Phase 3 — Approved Decisions, Contracts and Ownership

Approved by the user on 2026-11-11. Source of truth for code: `backend/app/models`, migration `0003`,
`app/discovery/{contracts,configs}.py`, `app/discovery/adapters/{base,__init__}.py`, `app/sites/{schemas,router}.py`.

## 1. Approved decisions

| # | Decision | Implementation |
|---|---|---|
| 1 | `career_sites` table + schema changes; Phase 2 APIs stay backward-compatible | Migration 0003: `career_sites`, `postings.career_site_id`, `scrape_runs.career_site_id` + scope counters, `jobs.scope_reason`, enum additions (`greenhouse`, `lever`, `ashby`, `oracle_hcm`, `eightfold`, `successfactors_csb`, health `blocked`). Existing company extractor configs migrated to a `main` site. `companies.extractor*` / `health` / `next_check_at` remain as **read-only summaries** in `CompanyOut`; `CompanyCreate.extractor_config` still works by creating the first site. |
| 2 | Unclear-location jobs hidden from the inbox | `location_scope.include_unknown` now defaults to **false**. `GET /jobs?scope=unknown` is the **Needs Location Review** queue. Users can force a job in or out (`scope_overridden`). |
| 3 | Palo Alto Networks: bookmarklet/manual only | Registry `support=not_reliable`; the import never enables it. |
| 4 | IBM & Rippling provenance probes | Only via `scripts/record_fixture.py` (robots-checked, honest UA, rate-limited), tag `provenance`, budget 8. |
| 5 | Global title include/exclude filters, configurable | Setting `title_filters` (`include`, `exclude`; whole-word, case-insensitive; exclude wins). Applied by the **pipeline** to discovered postings only, never inside adapters, never to manual/bookmarklet entries. Filtered postings are counted (`postings_filtered`), not stored. |
| 6 | About 60 fixture requests | `scripts/record_fixture.py` enforces per-tag budgets and a hard global cap (66). Every request, including robots.txt fetches, goes to the committed ledger `data/verification/fixture_requests.jsonl`. |
| — | Evidence + verification status preserved per site | `career_sites.support_class`, `india_coverage`, `verification_status`, `verified_at`, `evidence` (the full verification record), imported from `data/companies.csv` and `data/verification/*.json`. |
| — | robots.txt changes → auto-pause | `PoliteClient` re-checks robots.txt (cache ≤ 6 h; also at the start of every run). If our path is disallowed, `RobotsDisallowed` is raised and the site gets `health=blocked`, `monitoring_enabled=false`, and `paused_reason`. It needs an explicit `POST /sites/{id}/resume`. |
| — | Failed scrapes never close jobs | `apply_misses` runs only for `complete and succeeded` runs, **per portal** (`career_site_id`). Blocked, failed, partial and quarantined runs never record misses. Covered by tests at the pipeline level. |
| — | 30-minute schedule configurable | Setting `scheduler.default_interval_min` (10–1440) with a per-site `check_interval_min` override. |
| — | Each shared adapter tested on multiple companies | Fixtures from **at least 2–3 different companies per platform**. The one exception is Ashby: Snowflake is the only Ashby company in the registry, so its second variant is a structurally different synthetic fixture, and this is called out in the report. |
| — | No full-scale scheduled scrape without approval | Setting `scheduler.enabled = false` (global switch). The registry import creates sites with `monitoring_enabled=false`. Only dry-runs (`POST /sites/{id}/test`) and single manual runs are possible until you approve. |

## 2. Fixture request budgets (enforced by the recorder)

| Tag | Agent | Budget | Intended use |
|---|---|---|---|
| `workday` | Workday adapter | 14 | 4 tenants × (robots + list page with facets + 1 detail); one with location-text strategy |
| `ghla` | Greenhouse/Lever/Ashby adapter | 12 | Greenhouse 3 boards, Lever 3, Ashby 1 (+ robots per host) |
| `ohcm_ef` | Oracle HCM + Eightfold adapter | 20 | Oracle 3 tenants, Eightfold 3 tenants (both variants), list + detail |
| `provenance` | IBM/Rippling probe | 8 | Confirm endpoints from the companies' own page scripts |
| **Total** | | **54** (cap 66) | |

## 3. Ownership (Phase 3.1–3.3)

**Lead (central, read-only for agents):** models, migrations, `core/`, `discovery/contracts.py`,
`discovery/configs.py`, `discovery/adapters/base.py`, `discovery/adapters/__init__.py`, `sites/schemas.py`,
router signatures, `settings/registry.py`, `scripts/record_fixture.py`, `tests/conftest.py`,
`tests/factories.py`, `tests/discovery_support.py`.

| Agent | Owns |
|---|---|
| **Runtime** | `app/discovery/http.py` (PoliteClient: UA, per-host rate limiter, robots policy with cache and crawl-delay, block detection, retries), `app/worker/**` (Procrastinate app, schema migration `0004`, scheduler tick, tasks, locks, graceful shutdown, heartbeat), `tests/runtime/**` |
| **Pipeline** | `app/discovery/india.py`, `app/discovery/title_filter.py`, `app/discovery/anomaly.py`, `app/discovery/pipeline.py`, `app/discovery/registry_import.py`, `app/sites/service.py` + `app/sites/router.py` bodies, Phase 3 changes in `app/discovery/ingest.py` (career_site_id, scope passthrough, per-portal misses), `app/discovery/normalize/location.py` (Indiana guard, "IN-Pune" style), company compatibility in `app/companies/service.py`, `tests/pipeline/**` |
| **Workday** | `app/discovery/adapters/workday.py`, `tests/adapters/test_workday.py`, `tests/fixtures/workday/**` |
| **GH/Lever/Ashby** | `app/discovery/adapters/{greenhouse,lever,ashby}.py`, their tests and fixtures |
| **Oracle/Eightfold** | `app/discovery/adapters/{oracle_hcm,eightfold}.py`, their tests and fixtures |
| **Provenance** | No code. It produces `data/verification/provenance_ibm_rippling.md` and fixtures under `tests/fixtures/provenance/` |

The pipeline calls `run_site(site_id, *, trigger, fetcher, now=None, dry_run=False) -> RunSummary`. The worker task
`scrape_site(site_id)` builds a `PoliteClient` and calls it. Adapters are looked up with `get_adapter(platform, module)`.
