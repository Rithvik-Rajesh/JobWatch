# JobWatch — Phase 1 Architecture Proposal

Status: **decisions recorded, awaiting Phase 2 go-ahead** · Date: 2026-11-11

Companion documents:
- [01-research-findings.md](01-research-findings.md): Workday, job portals, AmbitionBox, hosting
- [02-database-schema.md](02-database-schema.md): full relational schema and ingest invariants

---

## 1. Summary of decisions

| Area | Decision | Why |
|---|---|---|
| Shape | **Modular monolith**: one Python codebase, two processes (`api`, `worker`), one Postgres | Personal app. Microservices would add ops cost with no benefit. Module boundaries are enforced in code. |
| Backend | **Python 3.12, FastAPI, SQLAlchemy 2 (async), Alembic, Pydantic v2** | Python has the best scraping/parsing ecosystem (httpx, selectolax, extruct, Playwright). FastAPI generates an OpenAPI contract that the frontend consumes as typed code. |
| Database | **PostgreSQL 17** with full-text search (`tsvector`) + `pg_trgm` | Relational integrity, transactional outbox, FTS good enough for ~100k jobs, and it doubles as the queue. No Elasticsearch, no Redis. |
| Background jobs | **Procrastinate** (Postgres-backed task queue) | Tasks survive restarts (they're rows), retries with backoff, periodic tasks, and **per-company locks** (`lock="company:42"`) plus **queueing locks** (no duplicate enqueue). No extra infrastructure. |
| HTTP / parsing | `httpx` (HTTP/2, timeouts, retries via `tenacity`), `selectolax` (HTML), `extruct` (JSON-LD), `nh3` (HTML sanitising) | Fast and dependency-light. |
| Browser automation | **Playwright (Chromium)**, opt-in per company, run in a separate low-concurrency queue | Used only when a site can't be read without JS. Keeps memory predictable. |
| Frontend | **React 18 + TypeScript + Vite** SPA; **TanStack Query/Table/Virtual**, **shadcn/ui (Radix) + Tailwind**, **dnd-kit** (Kanban), **Recharts**, **cmdk** (command palette), React Router | Productivity-app feel, dense virtualized tables, accessible primitives. No SSR needed for a private app. |
| API contract | OpenAPI from FastAPI → `openapi-typescript` → typed client in `frontend/src/api/` | One source of truth. Drift is caught at build time. |
| Notifications | Telegram Bot API through a **transactional outbox** table + channel interface | Exactly-once-ish delivery, no duplicates, easy to add email later. |
| Auth | Single user: username + argon2 password hash in env, HttpOnly session cookie, CSRF token, login rate-limit | The app is internet-facing, so it must be authenticated. Simple and adequate. |
| Deploy | **Docker Compose on one Hetzner CPX22 VPS** (Caddy for TLS), nightly `pg_dump` → Cloudflare R2 | About €20/month all-in, straightforward to maintain. |
| Observability | `structlog` JSON logs, `/healthz` + `/readyz`, Healthchecks.io dead-man's switch, in-app Health page, Telegram alerts | Enough visibility without running a monitoring stack. |

---

## 2. System architecture

```
                     ┌────────────────────── Hetzner VPS (Docker Compose) ───────────────────────┐
  Browser ──HTTPS──▶ │ Caddy ──▶ api (FastAPI + static SPA)                                       │
  Bookmarklet ─────▶ │              │  REST /api/v1                                               │
                     │              ▼                                                             │
                     │        ┌─────────────┐   procrastinate_jobs (queue tables)                 │
                     │        │ PostgreSQL  │◀──────────────────────────┐                         │
                     │        └─────────────┘                           │                         │
                     │              ▲                                   │                         │
                     │              │                                   │                         │
                     │ worker (Procrastinate) ── queues: scheduler · scrape · detail · browser(1)  │
                     │       │                          · enrich · notify · maintenance          │
                     │       ├──▶ Workday / career sites / Adzuna / Himalayas / Remotive / HN     │
                     │       └──▶ Telegram Bot API                                                │
                     │ backup (cron: pg_dump → R2)                                                │
                     └────────────────────────────────────────────────────────────────────────────┘
                       Healthchecks.io ◀─ heartbeat (scheduler tick + backup)   UptimeRobot ─▶ /healthz
```

### 2.1 Backend module layout (each module = `models.py`, `schemas.py`, `service.py`, `router.py`)

```
backend/app/
  core/            config (pydantic-settings), db session, logging, auth, errors, pagination
  companies/       CRUD, CSV import, career-URL detection, aliases
  jobs/            query/search/facets, manual add, merge, versions & diffs
  applications/    status transitions, timeline events, notes, contacts, interviews, bulk ops, board
  reminders/       CRUD, due/notify computation
  statuses/        configurable status lookup
  salary/          SalaryService + providers/ (employer_posting, user_entered, ambitionbox_api[disabled])
  notifications/   outbox writer, dispatcher, channels/ (telegram), message templates
  health/          run history, anomaly evaluation, health overview
  dashboard/       aggregate queries
  settings/        typed settings, saved views, resume versions, tags
  discovery/
    extractors/    base.py (Protocol) · workday.py · jsonld.py · html.py · custom/<slug>.py
    sources/       adzuna.py · himalayas.py · remotive.py · remoteok.py · hn_hiring.py · capture.py
    normalize/     location, employment type, work arrangement, experience, skills, salary text, title
    validation/    per-record validation + run-level anomaly checks
    ingest.py      THE single write path for postings/jobs/versions (used by every source)
    detect.py      careers URL → suggested extractor config
  worker/          procrastinate app, task definitions, scheduler tick
backend/migrations/  Alembic
backend/tests/       unit · integration (real Postgres) · fixtures/http (recorded responses)
```

Dependency rule: `discovery` → `ingest` → domain services. Routers never call extractors directly; they
enqueue tasks. Domain modules never import `discovery`.

### 2.2 Extractor contract

```python
class Extractor(Protocol):
    kind: ClassVar[str]                 # 'workday' | 'jsonld' | 'html' | 'custom'
    version: ClassVar[str]              # bump when parsing logic changes (stored on scrape_runs)
    Config: ClassVar[type[BaseModel]]   # validates companies.extractor_config

    async def list_postings(self, cfg, ctx: FetchContext) -> AsyncIterator[ListedPosting]: ...
        # yields external_id, source_url, title, location_text, posted_raw, fingerprint
    async def fetch_detail(self, cfg, posting: ListedPosting, ctx) -> RawJobDetail: ...
    async def probe(self, url: str, ctx) -> ProbeResult | None: ...   # used by "detect" when adding a company

@dataclass
class ExtractionOutcome:
    complete: bool          # True only if every page was fetched and validated
    postings: list[ListedPosting]
    warnings: list[Warning]
```

`FetchContext` provides: a shared `httpx.AsyncClient`, a per-host rate limiter (default 1 req/s,
configurable), retry policy, request counter, and structured logger bound to `run_id`/`company`.

### 2.3 Adding a company (no code changes)

Typical case, about 10 seconds: **Companies → Add → paste careers URL → Detect**.
`detect.py` recognises `*.myworkdayjobs.com` / `myworkdaysite.com`, derives tenant/site, makes one list
call, lists the available country facets, and shows "**242 jobs found**, 5 samples". Pick "India" (or all)
→ Save. Non-Workday URLs are probed for JSON-LD/sitemap. If found, they're configured as `jsonld`; if not,
you get an `html` selector form with a live preview.

Stored config examples (`companies.extractor_config`):

```jsonc
// workday
{ "url": "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite",
  "country": "India",                         // resolved to facet ID at runtime, cached below
  "facet_cache": { "locationHierarchy1": ["2fcb99c4..."], "resolved_at": "2026-11-11" },
  "search_text": null,
  "title_include": ["engineer", "developer", "scientist"], "title_exclude": ["intern"] }

// jsonld (listing page links + JSON-LD on each job page)
{ "listing_url": "https://careers.example.com/jobs?location=india",
  "job_link_selector": "a.job-card", "pagination": { "param": "page", "max_pages": 20 },
  "render": false }                           // true → Playwright queue

// html (declarative selectors when no structured data exists)
{ "listing_url": "...", "item_selector": ".opening", "fields": { "title": "h3", "location": ".loc",
  "url": "a@href" }, "detail": { "description": ".job-body" }, "render": false }

// custom → backend/app/discovery/extractors/custom/<slug>.py (escape hatch, isolated per company)
```

CSV bulk import (`name,careers_url,website_url,country,monitoring_enabled`) runs every row through the
same `detect` step and shows a preview table with a per-row result (✓ detected Workday / ⚠ needs manual
config / ✗ invalid) before committing.

### 2.4 Discovery pipeline (one company run)

```
scheduler tick (every 1 min) ─▶ select companies where next_check_at <= now()  (FOR UPDATE SKIP LOCKED)
   ─▶ set next_check_at = now() + interval + jitter(±3 min) ─▶ defer scrape_company(company_id)
        [queueing_lock=company_id → never two queued; lock=company_id → never two running]

scrape_company:
  1. insert scrape_runs(status=running)
  2. list_postings()  → paginate until empty page; dedupe by external_id within run
  3. validate each record (required: external_id, title, url; warn: location, …)
  4. run-level checks vs baseline → status = succeeded | partial | quarantined | failed
  5. ingest.upsert_listing(...) in batches of 20 (one txn each)
  6. enqueue fetch_detail for new / fingerprint-changed / stale(>7 d) postings  (detail queue)
  7. if complete & not quarantined: apply misses → close postings missing on 2 runs spanning ≥ 6 h
  8. update company health, counts, baseline; write outbox rows (digest of new jobs, health alerts)
  9. finish scrape_runs row

fetch_detail(posting_id): fetch → normalize → content_hash → new job_versions row if changed
   → refresh jobs' denormalized view → enqueue salary enrichment if no employer salary
```

Retries: network/5xx/429 are retried in-task (3 attempts, exponential backoff, `Retry-After`). If the
task still fails, Procrastinate retries it (2×, 2 min / 10 min). 4xx config errors are not retried.
They mark the company `failing` with a clear error ("Workday site not found (S21) — check URL").
Health escalation: `degraded` after 1 failure, `failing` after 3 consecutive. A Telegram alert is sent
once per company per day while failing, deduplicated through the outbox `dedupe_key`.

External sources (Adzuna, Himalayas, Remotive, RemoteOK, HN) run as their own tasks with
per-source schedules (e.g. Remotive 4×/day per its terms). They resolve companies through name/alias
matching (unknown → new unmonitored company) and go through the **same `ingest`** path.

### 2.5 Notifications

- Domain code writes `notification_outbox` rows **in the same transaction** as the change, with a
  `dedupe_key`. `UNIQUE(channel, dedupe_key)` makes duplicates impossible.
- The `notify` task drains the outbox (SKIP LOCKED), sends, and marks rows sent/failed with backoff.
  Delivery failures are shown on the Health page.
- Events: new jobs (one **digest per run** after your filters: title keywords, locations, companies);
  important updates to jobs you're tracking (closed, reopened, deadline/salary/title changed); scraper
  failing / recovered; reminder due (deadline, follow-up, interview, interview prep); daily summary
  (optional).
- Quiet hours and per-event toggles live in `settings`.

Example Telegram message:
```
🆕 3 new jobs · NVIDIA
• Senior Software Engineer, CUDA — Bengaluru · Hybrid
• ASIC Design Engineer — Bengaluru
• Data Scientist, Applied — Hyderabad
Open in JobWatch ↗   ·   View all NVIDIA ↗
```
Each line deep-links to `/jobs/:id`, with inline buttons for "Interested" / "Not interested" (Telegram
callback → API, authenticated by bot secret + chat-id allowlist).

### 2.6 Salary enrichment

```python
class SalaryProvider(Protocol):
    key: str; provenance: SalaryProvenance; enabled: bool
    async def lookup(self, q: SalaryQuery) -> list[SalaryEstimate]   # q: company, role, exp band, location
```

Runs on the `enrich` queue after detail ingest. It never blocks discovery, and failures are logged only.
Results are cached per `(provider, company, normalized_role, exp_band)` for 30 days. The UI renders
two visually distinct blocks:
- **Employer-provided compensation:** only from `employer_posting`, with the verbatim text.
- **Estimated company salary based on external salary data:** provider, source link, retrieved date,
  role matched vs. queried, experience match, sample size, confidence badge. Shows **"Not available"**
  when empty.

---

## 3. Application lifecycle rules

- Default statuses: New · Interested · Shortlisted · Applied · Online Assessment · Interviewing ·
  Offer Received · Rejected · Withdrawn · Not Interested. They can be renamed, recoloured, reordered, and
  extended. Behaviour keys off the **category**, never the label.
- **Expired/Closed is not an application status.** It is `jobs.availability`, shown as a separate badge
  ("Closed by employer"). A closed job keeps your "Interviewing" status untouched.
- Any status → any status is allowed (it's your tracker), but every change is a timeline event.
  `occurred_at` can be back-dated ("applied on Nov 14").
- Moving to *Applied* sets `applied_at` (if unset) and auto-creates a "Follow up" reminder after N days
  (setting, default 7). Moving to *Rejected/Withdrawn* prompts for an optional reason.
- Optimistic concurrency (`row_version`, `If-Match`) prevents a stale Kanban drag from overwriting a
  newer change. The UI updates optimistically and rolls back on 409.

---

## 4. API contract (REST, `/api/v1`, JSON, cookie session)

Conventions: list endpoints return `{items, page, page_size, total}`; errors return
`{error: {code, message, details}}`; mutating endpoints are idempotent where it matters (status set
is idempotent; bulk ops take `Idempotency-Key`).

| Area | Endpoints |
|---|---|
| Auth | `POST /auth/login` · `POST /auth/logout` · `GET /auth/me` |
| Dashboard | `GET /dashboard/summary` · `GET /dashboard/timeseries?metric=applied\|discovered&bucket=day\|week` · `GET /dashboard/funnel` · `GET /dashboard/breakdown?by=status\|company\|source` · `GET /dashboard/upcoming?days=14` |
| Jobs | `GET /jobs` (q, company_id[], status[], source[], country[], location, employment_type[], work_arrangement[], salary_min, has_salary, posted_after, discovered_after, is_new, availability[], tag[], sort, page) · `GET /jobs/facets` (same filters → counts) · `GET /jobs/{id}` · `POST /jobs` (manual) · `POST /jobs/capture` (bookmarklet payload) · `PATCH /jobs/{id}` (manual-source fields only) · `GET /jobs/{id}/versions` · `GET /jobs/{id}/versions/{vid}/diff` · `POST /jobs/{id}/merge` · `POST /jobs/{id}/unmerge` |
| Applications | `PUT /jobs/{id}/status` `{status, occurred_at?, reason?, note?}` · `PATCH /jobs/{id}/application` (If-Match) · `POST /applications/bulk-status` · `GET /applications` (tracker table) · `GET /applications/board` · `PATCH /applications/{id}/position` · `GET /jobs/{id}/timeline` · `POST /jobs/{id}/timeline` (manual entry) |
| Notes / contacts / interviews | `GET\|POST /jobs/{id}/notes` · `PATCH\|DELETE /notes/{id}` · same shape for `contacts`, `interviews` |
| Reminders | `GET /reminders?from&to&state` · `POST /reminders` · `PATCH /reminders/{id}` · `POST /reminders/{id}/complete` · `POST /reminders/{id}/snooze` |
| Companies | `GET /companies` · `POST /companies` · `GET\|PATCH /companies/{id}` · `POST /companies/{id}/archive` · `POST /companies/detect` `{careers_url}` → config + preview · `POST /companies/{id}/test` (dry run, no writes) · `POST /companies/{id}/scrape` · `POST /companies/import` (CSV → preview) · `POST /companies/import/{token}/commit` · `GET /companies/{id}/runs` · `GET /companies/{id}/history` |
| Sources | `GET /sources` · `PATCH /sources/{id}` · `POST /sources/{id}/run` |
| Health | `GET /health/overview` · `GET /health/runs?status&company_id` · `GET /health/notifications` · `GET /healthz` · `GET /readyz` |
| Salary | `GET /jobs/{id}/salary` · `POST /jobs/{id}/salary` (user-entered) · `POST /jobs/{id}/salary/refresh` · `GET /salary/providers` |
| Settings | `GET\|PUT /settings/{key}` · `/statuses` CRUD + `POST /statuses/reorder` · `/saved-views` CRUD · `/resume-versions` CRUD · `/tags` CRUD · `POST /notifications/test` |

---

## 5. Frontend information architecture

```
┌──────────────┬──────────────────────────────────────────────────────────────────────────┐
│ ◉ JobWatch   │  ⌘K Search jobs, companies, actions…                    + Add  ⏰ 3  ☾  │
│              ├──────────────────────────────────────────────────────────────────────────┤
│ ▣ Dashboard  │                                                                          │
│ ⌕ Jobs    128│                         page content                                     │
│ ▤ Pipeline   │                                                                          │
│ ⏰ Agenda   3│                                                ┌─────────────────────────┤
│ 🏢 Companies │                                                │ Job detail drawer       │
│ ♥ Health  ⚠1 │                                                │ (deep-linkable /jobs/:id)│
│ ⚙ Settings  │                                                └─────────────────────────┤
└──────────────┴──────────────────────────────────────────────────────────────────────────┘
```

| Route | Purpose | Key UX |
|---|---|---|
| `/` Dashboard | Command centre | KPI tiles (discovered, new today, interested, shortlisted, applied, interviewing, offers, rejected) · "Today & upcoming" agenda (interviews, follow-ups, deadlines) · charts: applications over time, status distribution, conversion funnel, jobs by company, jobs by source · freshness strip ("last scan 4 min ago · 2 companies failing") |
| `/jobs` Discover | Triage inbox | Dense virtualized table (card view toggle) · filter bar with chips + facet counts · saved views as tabs ("New today", "India SWE", "Remote") · full-text search · multi-select bulk status · **keyboard triage**: `j/k` move, `i` Interested, `s` Shortlist, `a` Applied, `x` Not interested, `o` open apply page, `Enter` open drawer · "NEW" / "UPDATED" / "CLOSED" badges · undo toast on every status change |
| `/jobs/:id` | Job detail (drawer or full page) | Header with status pill + quick-action bar · tabs: Description (sanitized original, link to source) · Application (fields, contacts, interviews) · Timeline · Notes · History (versions with diffs, discovery sources) · salary card with provenance labelling |
| `/pipeline` | Application tracker | **Board** (dnd-kit Kanban, columns from statuses with `show_on_board`, closed/rejected collapsible) · **Table** (company, role, applied date, status, next action, next follow-up, latest update), inline editing · shared filters |
| `/agenda` | Reminders & interviews | Overdue / Today / This week / Later lists + month calendar · quick add (`r`) · complete/snooze inline |
| `/companies` | Monitoring config | Table with health dot, extractor, open jobs, last success, last error · Add (detect wizard) · CSV import wizard · enable/disable toggle · "Scrape now" |
| `/companies/:id` | Company detail | Config editor with **Test** (dry run preview) · jobs list · run history · discovery history chart · errors |
| `/health` | Scraper reliability | Totals (companies/active/healthy/failing), success rate 24 h/7 d, avg duration, queue depth, last scheduler heartbeat, recent failures & quarantined runs with reason, notification delivery failures, per-source status |
| `/settings` | Configuration | Statuses (drag reorder, colour) · Notifications (Telegram link + test, event toggles, new-job filters, quiet hours) · Sources (enable/config) · Salary providers · Resume versions · Tags · Bookmarklet install · Account |

Frontend structure:
```
frontend/src/
  api/          generated types + typed fetch client + TanStack Query hooks per resource
  components/ui/      shadcn primitives (owned copies)
  components/         StatusPill, StatusMenu, SalaryCard, Timeline, FilterBar, DataTable, KpiTile, Chart*
  features/<area>/    dashboard · jobs · job-detail · pipeline · agenda · companies · health · settings
  lib/          keyboard shortcuts, formatting (dates in your TZ, INR lakh/crore formatting), query-string ↔ filters
  routes.tsx
```
Filter state lives in the URL query string, so every view is shareable and bookmarkable and saved views
are just stored query strings. Light and dark themes. Responsive down to phone width, so the
agenda and quick status changes work on mobile.

---

## 6. Reliability guarantees (mapping to your §6 / §8)

| Requirement | Mechanism |
|---|---|
| Every 30 min, concurrently, no overlap per company | Scheduler tick + `next_check_at` with jitter · Procrastinate `queueing_lock` + `lock` per company · worker concurrency 8 (scrape) / 16 (detail) / 1 (browser) · per-host rate limiter |
| Survive restarts | Queue is Postgres rows; stale `running` jobs are retried on boot · graceful SIGTERM: stop fetching new tasks, finish current batch (30 s) |
| No duplicate jobs | `UNIQUE(source_id, company_id, external_id)` + upsert · cross-source dedupe heuristic + manual merge |
| No duplicate notifications | Outbox `UNIQUE(channel, dedupe_key)` |
| Failure never closes jobs | Misses are applied only from `complete && succeeded && !quarantined` runs, needing 2 misses ≥ 6 h apart |
| Detect structure changes | Per-record validation (missing fields ratio), zero-result check, > 50 % drop vs rolling baseline, schema validation of JSON responses (Pydantic models with `extra="allow"` but required keys) |
| Visibility | `scrape_runs` table, Health page, Telegram alerts, Healthchecks.io heartbeat |

---

## 7. Testing strategy

- **Unit:** normalizers (location/experience/salary parsing tables), Workday URL parser, content hashing,
  status transition rules, anomaly detection.
- **Contract fixtures:** recorded Workday list/detail/facet responses (NVIDIA, Salesforce + error cases
  400/404/422/429) replayed with `respx`. No live calls in CI. An opt-in `pytest -m live` smoke test.
- **Integration (real Postgres via docker):** ingest idempotency (run the same scrape twice → identical DB),
  closure rules (failed/partial/quarantined never close), dedupe across sources, concurrent status updates
  (409 path), outbox dedupe, reminder notification once.
- **Frontend:** Vitest + Testing Library for components/hooks. **Playwright e2e** smoke: login → add
  company (mocked backend) → triage job with keyboard → drag on Kanban → verify timeline.
- **CI:** GitHub Actions: ruff + mypy + pytest; eslint + tsc + vitest; build Docker images.

---

## 8. Deployment & cost

**Recommended:** Hetzner Cloud **CPX22** (2 vCPU, 4 GB RAM, 80 GB SSD), EU region (Helsinki/Nuremberg).
Docker Compose services: `caddy`, `api`, `worker`, `worker-browser` (Playwright, can be disabled),
`postgres`, `backup`.

| Item | Monthly |
|---|---|
| Hetzner CPX22 (EU) | ~€17–20 |
| Hetzner server snapshots (+20 %, optional) | ~€3.5–4 |
| Cloudflare R2 backups (nightly `pg_dump -Fc`, 30-day retention) | €0–1 |
| Domain (e.g. `.dev`/`.in`, amortised) | ~€1 |
| Telegram, Healthchecks.io, UptimeRobot, Adzuna/Himalayas/Remotive/RemoteOK/HN APIs | €0 |
| Salary provider | €0 (no authorized provider; API.SH would be ~$0–2 if ever enabled) |
| **Total** | **≈ €19–26 / month (≈ $21–29)** |

Budget alternatives: CPX12 (2 GB, ~€10) if browser automation is not needed. Singapore region
(+€10) if UI latency matters to you. Zero-ops alternative: Railway (~$40–45).

Ops: `unattended-upgrades`, ufw (22/80/443 only), SSH keys only, Postgres not exposed, `.env` (chmod 600,
never committed) with an `.env.example`. Deploy = `git pull && docker compose up -d --build` via a
`make deploy` script (GitHub Actions deploy optional later). Restore runbook plus a quarterly restore
drill, scripted as `make restore-test`.

---

## 9. Implementation plan & agent orchestration

I stay lead architect and own `backend/app/core/`, the **schema/migrations**, `discovery/ingest.py`
interfaces, and the OpenAPI contract. Every agent's output is reviewed and integrated by me before
the next phase. Sonnet subagents, with strict file ownership:

| Phase | Work | Agents (parallel) | Ownership |
|---|---|---|---|
| **2 Core** | Repo scaffold, compose dev env, schema + migrations + seeds (me first, ~½ day) → domain modules & APIs | Backend/DB agent | `backend/app/{companies,jobs,applications,reminders,statuses,settings,dashboard}` |
| | Normalizers + ingest + dedupe (against my interface) | Scraping agent | `backend/app/discovery/{normalize,ingest_impl}` |
| | Test harness, factories, integration tests for ingest & transitions | QA agent | `backend/tests/` |
| **3 Discovery** | Workday extractor, jsonld/html extractors, detect, validation | Scraping agent | `discovery/extractors,validation,detect.py` |
| | Procrastinate worker, scheduler, retries, health evaluation | Infra agent | `backend/app/worker/`, `health/` |
| **4 Frontend** | App shell, design system, all pages (can start once the OpenAPI contract is frozen at the end of Phase 2, in parallel with Phase 3) | Frontend agent (+ second frontend agent for dashboard/health) | `frontend/` split by `features/*` |
| **5 Integrations** | Telegram outbox + bot, Adzuna/Himalayas/Remotive/RemoteOK/HN sources, bookmarklet, salary providers | Backend agent + Scraping agent | `notifications/`, `discovery/sources/`, `salary/` |
| **6 Production** | Dockerfiles, Caddy, backups, monitoring, security review, e2e tests, docs | Infra agent, QA agent, Docs agent | `deploy/`, `docs/`, `e2e/` |

Phases 3 and 4 overlap, which is the main parallelism win. Rough sequencing: Phase 2 → (3 ∥ 4) → 5 → 6.

---

## 10. Explicitly out of scope (v1)

LLM scoring, resume AI, scraper-repair agents, generic ATS framework, auto-apply, any LinkedIn account
automation, server-side scraping of LinkedIn/Naukri/Indeed/Internshala/Wellfound/AmbitionBox.

---

## 11. Decisions (answered 2026-11-11)

| # | Question | Decision | Effect on the plan |
|---|---|---|---|
| 1 | Hosting region | **Decide later** | Build and run locally with Docker Compose. The deploy target is chosen in Phase 6, and the compose setup stays provider-neutral. |
| 2 | Workday location scope | **India only** by default | `detect` pre-selects the India country facet, with a per-company override (including "all locations"). |
| 3 | External sources | **Adzuna** in v1; **email-alert ingestion** (LinkedIn/Naukri/Indeed) as v1.1 | Himalayas, Remotive, RemoteOK and HN are dropped from v1. They are still easy to add later through the source interface. Adzuna's India coverage must be confirmed once you have a key. |
| 4 | Salary | **API.SH / AmbitionBox provider ships disabled** | v1 salary sources are the job posting itself and figures you enter. |
| 5 | Company list | **You have a CSV** | It is imported through the CSV wizard. Its mix of Workday and non-Workday sites decides which custom extractors come first. |

Status: **awaiting explicit go-ahead for Phase 2.**
