# JobWatch — Phase 3 Scraping Architecture (proposal, awaiting approval)

Status: **proposal**. Nothing in this document is implemented yet. Schema/API changes are listed in §6 for approval.

## 1. Principles

1. **One adapter per platform, config per portal.** 45 portals run on 6 shared adapters, and adding a Workday or
   Greenhouse company is a CSV row or UI form. Bespoke adapters exist only where a company runs its own
   system (13 portals), and each is a small module implementing the same interface.
2. **No generic ATS abstraction.** Adapters share a small *protocol* and HTTP utilities, not a configurable
   "universal scraper". Each adapter is explicit, typed and tested against recorded real responses.
3. **India gate at three layers:** source-side filter → structured location parsing → the single scope policy.
   A job is in scope only on positive evidence.
4. **Policy-compliant by construction:**
   - The honest User-Agent is hard-coded in the HTTP client, and adapters cannot override it.
   - robots.txt is checked before every fetch.
   - A block or challenge stops the portal. Nothing retries around it.

## 2. Components

```
backend/app/discovery/
  http.py            PoliteClient: fixed UA, per-host rate limiter (default 1 req/s, robots crawl-delay wins),
                     RobotsPolicy (cached 24 h, RFC 9309 semantics), retries for 5xx/timeouts/429 Retry-After,
                     BlockDetector (403/406/challenge pages -> PortalBlocked, never retried), request counter
  adapters/
    base.py          Adapter protocol + ListedPosting / DetailRecord / ListingResult(complete: bool)
    registry.py      platform key -> adapter class (explicit dict, no plugin magic)
    workday.py       country-facet resolution, location-text fallback, Indiana guard, >2000 facet splitting
    greenhouse.py    boards-api ?content=true (single request), offices + location parsing
    lever.py         postings API, country + workplaceType
    ashby.py         posting-api job-board, address country, isRemote
    oracle_hcm.py    recruitingCEJobRequisitions finder (siteNumber, locationId), detail endpoint
    eightfold.py     two variants: pcsx | apply_v2
    successfactors_csb.py   sitemap + job-page parsing (Wipro, HCLTech)
    custom/          amazon.py, ibm.py, capgemini.py, cisco_phenom.py, amd_jibe.py, rippling.py,
                     atlassian.py, intuit.py, grab.py, zoho.py, jane_street.py
  india.py           eligibility evaluation on ListedPosting (structured country -> city dictionary -> remote rule)
  pipeline.py        run_portal(): list -> India gate -> diff fingerprints -> fetch details (new/changed/stale)
                     -> PostingInput -> ingest (Phase 2) -> apply_misses (only if complete & trusted)
  anomaly.py         zero-result / >50% drop vs baseline / missing-field ratio -> quarantine
  registry_import.py data/companies.csv -> companies + career_sites (idempotent, disabled by default)
backend/app/worker/  Procrastinate app, scheduler tick, tasks: scrape_portal, fetch_details, notify (Phase 5)
```

### Adapter protocol

```python
class Adapter(Protocol):
    platform: ClassVar[str]           # 'workday', 'greenhouse', ..., 'custom:amazon'
    version: ClassVar[str]            # stored on scrape_runs; bump on parsing changes
    Config: ClassVar[type[BaseModel]] # validates career_sites.config

    async def list(self, cfg, scope: IndiaScope, http: PoliteClient) -> ListingResult
        # yields ListedPosting(external_id, requisition_id, title, location_text, locations,
        #                      country_codes, remote_flag, posted_raw, url, fingerprint, raw)
        # ListingResult.complete is False if ANY page failed or a cap prevented full enumeration
    async def detail(self, cfg, posting: ListedPosting, http: PoliteClient) -> DetailRecord
    async def probe(self, url: str, http: PoliteClient) -> ProbeResult | None  # "Detect" in Add-company UI
```

## 3. India-only enforcement (all sources)

| Layer | Rule |
|---|---|
| **Source filter** | Use the portal's own India filter when it exists (Workday country facet, Oracle `locationId`, Eightfold `location=India`, Amazon `normalized_country_code[]=IND`). This cuts load by about 90%. |
| **Structured evidence** | `india.evaluate(posting)` returns `in_scope`, `out_of_scope` or `unknown`, together with a **reason**. Possible reasons: country code IN; an Indian city or state (dictionary, with the Indiana guard); a remote role with explicit India eligibility ("Remote - India", Lever `country=IN`, Ashby address country IN). |
| **Remote rule** | A remote job is in scope **only** if India is explicitly listed. Bare "Remote", "Remote - Remote" or "Anywhere" is `unknown` and never `in_scope`. |
| **Multi-location** | A posting is in scope if **any** of its locations is in India. |
| **What gets stored** | `out_of_scope` postings are counted (`postings_seen`) but **not ingested**. `unknown` postings are ingested but **hidden** from the inbox, default lists, dashboard and notifications. They appear only in a **"Needs location review"** queue, where you can force them in or out (`scope_overridden`). |
| **Manual / bookmarklet** | The same evaluation applies. The capture response flags `location` in `needs_review` when eligibility is not established. |

This changes one Phase 2 default: `LocationScopeSettings.include_unknown` becomes **false** (see §6).

## 4. Run lifecycle (per portal)

1. The scheduler tick (every minute) picks due portals (`next_check_at <= now`, 30 min ± jitter) and enqueues
   `scrape_portal(site_id)` with a per-portal queueing lock and run lock, so two runs never overlap.
2. `robots` pre-check for the data host. If disallowed, the portal is paused with health `blocked` and the reason recorded.
3. `list` follows pagination until an empty page, with dedupe by external_id and a hard page cap per adapter.
4. The India gate runs, then a diff against stored `listing_fingerprint`.
5. `detail` runs on the separate queue for new, changed and stale postings (stale means more than 7 days old).
6. `ingest` (Phase 2) runs in batches of 20, one transaction each.
7. Anomaly checks run: zero results, a drop of more than 50% vs baseline, or a high missing-field ratio. Any of these sets the run to `quarantined`.
8. `apply_misses` runs only when `complete and status == succeeded`. Closure needs 2 misses at least 6 h apart (Phase 2 rule).
9. Portal health is updated: `healthy`, `degraded` after 1 failure, `failing` after 3, `blocked` on block/robots. The run is recorded in `scrape_runs`.

Blocked portals are **not** retried automatically. The Health page shows them with the reason, and you can
re-enable them manually after the site's policy changes.

## 5. Testing

- **Recorded fixtures:** one real list response plus one detail response per platform (and per bespoke adapter),
  captured once with the honest UA, about 2–3 requests each, and stored under `tests/fixtures/<platform>/`.
  CI replays them with `respx` and never touches the network.
- **Adapter contract tests:** pagination, completeness flag, India gate (incl. Indiana, bare Remote, "IN-Pune"),
  >2000 splitting (Workday), block detection (403/challenge HTML), robots disallow, crawl-delay.
- **Pipeline integration tests** (real Postgres): a full run → ingest; a failed or partial run never closes jobs;
  quarantine on a drop; out-of-scope postings not stored; unknown postings hidden but reviewable.
- **Dry run:** `POST /api/v1/sites/{id}/test` runs `list` (first page only) and returns India-gated samples,
  without writing anything.

## 6. Proposed schema / API changes (need your approval)

These are additive where possible. Phase 2 endpoints keep working.

| # | Change | Why it's necessary | Compatibility |
|---|---|---|---|
| 1 | **New table `career_sites`** (company_id, label, platform, config jsonb, monitoring_enabled, check_interval_min, next_check_at, health, consecutive_failures, last_checked_at, last_success_at, last_error, open_jobs_count, baseline_jobs_count, blocked_reason). `scrape_runs.career_site_id` FK. | Companies with several portals: Salesforce, Samsung, Visa, D. E. Shaw, LTIMindtree, and Workday's own extra sites. Health and scheduling are per portal, not per company. | Migration moves each company's existing extractor fields into one `main` site. `CompanyOut` keeps `extractor` / `health` / `next_check_at` as **read-only summaries** (primary site / worst health). `CompanyCreate.extractor_config` still works and creates the first site. New endpoints: `GET/POST /companies/{id}/sites`, `PATCH/DELETE /sites/{id}`, `POST /sites/{id}/test`, `POST /sites/{id}/scrape`. |
| 2 | **Extend platform enum**: add `greenhouse`, `lever`, `ashby`, `oracle_hcm`, `eightfold`, `successfactors_csb`. Bespoke sites use `custom` + `module`. | The classification above. | `ALTER TYPE ... ADD VALUE`. Existing values are unchanged. |
| 3 | **`health_status` gains `blocked`**. | To distinguish "blocked by policy/bot protection" from transient failures, and to never auto-retry blocked portals. | Additive enum value. |
| 4 | **`jobs.scope_reason` text**, e.g. "country IN (facet)", "city: Pune", "remote: India listed", "no location". | Transparency for the India gate and the review queue. | Additive nullable column. |
| 5 | **Default `location_scope.include_unknown = false`**, plus `GET /jobs?scope=unknown`, which already works via the existing filter, as the "Needs location review" queue. | Your rule: don't silently include jobs whose India eligibility cannot be established. | Setting default change. Existing stored settings are migrated. |
| 6 | Company registry import: `POST /companies/import` (Phase 2 CSV import) learns the extra columns (platform, tenant, site_id, …), or a CLI `jobwatch registry import data/companies.csv`. | Seeding 97 portals from the verified CSV. | Additive. Imported portals are **disabled by default** until you approve. |

## 8. Decisions needed from you

1. **Schema/API changes in §6.** Approve all six, or tell me which to drop.
2. **Unknown-location jobs.** Should they be hidden by default and shown in a "Needs location review" queue (recommended),
   or not stored at all?
3. **Palo Alto Networks.** Its robots.txt disallows the Workday site path but not the data endpoint. I recommend treating that as
   disallowed (bookmarklet only).
4. **IBM and Rippling.** Their endpoints work but were inferred, not seen in page source. Should I confirm provenance from their own
   page scripts (about 2 requests each) before enabling them?
5. **Role filters for high-volume companies.** Accenture, Wipro, Amazon, IBM and Capgemini have thousands of India openings,
   mostly non-engineering. Should I add a global title include/exclude list (e.g. include "engineer|developer|SDE|data|ML|
   scientist|analyst", exclude "sales|delivery lead|…")? It would be applied before ingest and editable in Settings.
6. **Fixture capture.** Recording 2–3 real responses per platform or bespoke adapter for tests (about 60 requests in total,
   honest UA, robots-checked). Is that OK as part of step 3.3/3.4?

Unchanged: posting identity `(source, company, external_id)`. Adapters prefix `external_id` with the portal key
(e.g. `workday:salesforce/External_Career_Site:JR123`), which prevents collisions between portals of one company.
The Phase 2 ingest, dedupe, closure and timeline invariants are reused as-is.

## 7. Implementation order (proposed)

| Step | Scope | Output | Network use |
|---|---|---|---|
| **3.1 Foundation** | Migrations for §6, `PoliteClient` (UA, rate limit, robots, block detection), adapter protocol and registry, India evaluator, pipeline, anomaly checks, dry-run endpoint, registry import (all disabled) | Tests on fakes and fixtures | none |
| **3.2 Worker** | Procrastinate worker, scheduler tick, locks, retries, graceful shutdown, run and health recording | Tests with real Postgres | none |
| **3.3 Shared adapters** | Workday → Greenhouse → Oracle HCM → Eightfold → Lever → Ashby, each with recorded fixtures | **45 portals / 42 companies** automatable | fixture capture: about 3 requests per platform |
| **3.4 Bespoke adapters** (by India volume) | Amazon, Wipro + HCLTech (SuccessFactors CSB), IBM*, Capgemini, Cisco, AMD, Rippling*, Atlassian, Intuit, Grab, Zoho (*after provenance check) | **+13 portals** | fixture capture: about 3 requests each |
| **3.5 Controlled rollout** (**requires your approval**) | Dry-run every enabled portal (first page only) → coverage report (India counts, unknown counts, samples) → you approve → enable in batches of about 10 portals → 24 h observation → remaining batches | Live monitoring | first real scrapes |

Steps 3.1 and 3.2 can run in parallel, as can the six adapters in 3.3, each owned by one agent with its own files.
Adzuna stays independent: it slots in as a `job_board_api` source whenever you provide the key.
