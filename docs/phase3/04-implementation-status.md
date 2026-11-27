# Phase 3 — Implementation Status (3.1–3.3 complete, awaiting review)

Date: 2026-11-12 · Branch: `phase-3/discovery` · Tests: **583 passing** against real PostgreSQL · No deployment, no
scheduled scraping.

## What is implemented

| Area | Status | Where |
|---|---|---|
| Career sites (multi-portal), evidence and verification status per site | ✅ | migration 0003, `app/models/catalog.py::CareerSite` |
| Polite HTTP client: honest UA (not overridable), RFC 9309 robots.txt checked before every request and every redirect, crawl-delay, per-host rate limit, block/challenge detection with no retry, transient retries | ✅ | `app/discovery/http.py` |
| robots.txt change → automatic pause (`health=blocked`, monitoring off, `paused_reason`; resume is explicit) | ✅ | pipeline + `POST /sites/{id}/resume` |
| India gate (structured country first, Indian city/state dictionary, Indiana/Indianapolis guard, remote only if India explicitly listed, unknown → review queue) | ✅ | `app/discovery/india.py`, `normalize/location.py` |
| Global title include/exclude filters (setting, not in adapters; never applied to manual/bookmarklet) | ✅ | `app/discovery/title_filter.py`, setting `title_filters` |
| Pipeline: list → India gate → filters → anomaly check → detail (new/changed/stale, capped per run) → ingest → per-portal misses | ✅ | `app/discovery/pipeline.py` |
| Failed / blocked / partial / quarantined runs never close jobs; misses are counted per portal | ✅ (tested) | `tests/pipeline/test_run_site.py` |
| Anomaly quarantine (zero results, >50% drop vs baseline, missing-field ratio) | ✅ | `app/discovery/anomaly.py` |
| Worker (Procrastinate on Postgres), per-site locks, scheduler tick, configurable interval (30 min default) + jitter, heartbeat, graceful shutdown | ✅ (not started) | `app/worker/`, migration 0004 |
| Global scheduler switch **off**; all imported sites **monitoring off** | ✅ | setting `scheduler.enabled=false` |
| Sites API: CRUD, dry-run test, manual scrape, resume, runs, scheduler status | ✅ | `app/sites/` |
| Phase 2 API compatibility (company extractor fields are summaries of the `main` site) | ✅ | `app/companies/service.py` |
| Registry import from `data/companies.csv` (idempotent, dry-run by default) | ✅ (applied to the **local** dev DB only) | `app/discovery/registry_import.py` |
| Shared adapters | ✅ Workday, Greenhouse, Lever, Ashby, Oracle HCM, Eightfold | `app/discovery/adapters/` |

### Adapter test coverage (recorded real responses, multiple companies each)

| Adapter | Companies recorded | Tests |
|---|---|---|
| Workday | NVIDIA, Barclays (location-text strategy), Airbus, Accenture (>2000 cap) | 30 |
| Greenhouse | Zscaler, Stripe, Razorpay | |
| Lever | Meesho, CRED, Spotify | 70 (GH + Lever + Ashby) |
| Ashby | Snowflake (the only Ashby company) + 1 structurally different **synthetic** fixture | |
| Oracle HCM | JPMorgan, Oracle, Dell | 73 (Oracle + Eightfold) |
| Eightfold | Microsoft (PCSX), Qualcomm (PCSX), NetApp (apply_v2) | |

## Live request ledger (approved budget: about 60; hard cap 66)

All requests went through `scripts/record_fixture.py` and are listed in `data/verification/fixture_requests.jsonl`.

| Tag | Requests | Purpose |
|---|---|---|
| workday | 13 | 4 tenants |
| ghla | 10 | 7 boards |
| ohcm_ef | 15 | 3 Oracle + 3 Eightfold tenants |
| provenance | 8 | IBM / Rippling endpoint provenance |
| robots_recheck | 5 | Re-reading robots.txt after the recorder bug below |
| **Total** | **51** | Every fixture URL re-audited against the real robots.txt: **0 disallowed** |

## Issues found and fixed during integration (disclosed)

1. **Recorder robots.txt bugs (lead's code).**
   - It used Python's first-match parser instead of RFC 9309 longest-match. This wrongly refused Eightfold's
     explicitly allowed `/api/pcsx` and `/api/apply` paths, so its fixtures were synthetic until fixed.
   - It sent `Accept: application/json` for robots.txt, so Workday hosts answered 406. That counts as "no rules"
     under RFC 9309.
   - Both are fixed. robots.txt was re-read for the affected hosts (all allow the data paths), and every recorded URL
     was re-audited. The production `PoliteClient` was never affected.
2. **Manual-scrape race.** The worker could start before the API transaction committed and create a duplicate
   run, leaving the original "queued" forever and blocking further manual scrapes. Fixed: the worker waits for the
   queued row, and queued or running rows older than 15 minutes are expired.
3. **Policy guard.** Sites classified `not_reliable` or `manual` (Palo Alto Networks, Nike, Morgan Stanley, IBM, Rippling, …)
   kept their real platform and could have been enabled via the API. Monitoring, dry runs and manual scrapes are now
   refused for them (409 `not_automatable`), with the evidence reason shown.
4. **Company CSV import (Phase 2) bypassed career sites.** Fixed: it now routes through the company service.
5. **Detail requests per run** are now capped (`scheduler.max_details_per_run`, default 150). New postings get priority,
   and the rest are fetched on later runs.
6. **Workday listing fingerprint** no longer includes "Posted N days ago". It changes daily and would have
   forced daily detail requests for every job.
7. **IBM and Rippling provenance check: NOT FOUND.** Both moved to bookmarklet-only, leaving 11 bespoke adapters.
8. **Dell's Oracle tenant** is on `enterpriseplatform.dell.com`, so the config was loosened to accept any host.

## Known limitations

- **Accenture** India results exceed Workday's 2,000 cap even per city (Bengaluru alone has ~13.7k). Runs will report
  `complete=false`, so jobs are added and updated but never auto-closed. Title filters would make it manageable.
- **Eightfold** exposes no verified detail endpoint, so descriptions come only from the listing (NetApp's is empty).
- **Greenhouse** has no structured country, so the India gate relies on location text (tested on "Bangalore, IND",
  "Bangalore ", "India Locations", bare "Remote").
- The Salesforce subsidiary-sites placeholder row is rejected by the import. Salesforce's main site is imported.

## Not done yet (by design)

- **3.4 Bespoke adapters** (11: Amazon, Wipro + HCLTech, Capgemini, Cisco, AMD, Atlassian, Intuit, Grab, Zoho, Jane Street).
  They need about 30 more fixture requests (separate approval).
- **3.5 Controlled rollout**, which **requires your approval**: dry runs per site (one listing page each), then a
  coverage review, then enabling sites in batches with `scheduler.enabled=true`.
