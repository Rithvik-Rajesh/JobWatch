# Phase 2 — Decisions, Contracts and Ownership

Date: 2026-11-11. This document amends [Phase 1 schema §3](../phase1/02-database-schema.md) where noted.
**Source of truth for the schema is `backend/app/models/` + `backend/migrations/`; for the API it is
the FastAPI app (`docs/api/openapi.json` snapshot).**

## 1. Clarifications from review → design decisions

| Your clarification | Decision / where it lives |
|---|---|
| Conservative cross-source dedupe | **Auto-attach only on strong identity:** (a) same `(source, company, external_id)` (normal upsert), (b) same company + same **requisition ID** (`postings.requisition_id`, e.g. Workday `JR2029481`), or (c) identical **canonical apply URL**. Everything else creates a separate job; if it is *similar* (same company, title trigram similarity ≥ 0.6, overlapping country or both unknown, both seen within 60 days) a row in **`duplicate_suggestions`** is created. The user confirms (merge) or dismisses (never suggested again). Merge is reversible in data terms: tombstone via `jobs.merged_into_job_id`, postings re-parented, history kept. *Supersedes Phase 1 §3.6 (auto-merge at ≥ 0.9).* |
| Preserve absolute posting dates; `first_seen_at` separate | `jobs.posted_at` + `posted_at_precision` (`exact` / `day` / `approximate` / `unknown`) + `posted_at_raw`. **Rule:** an absolute date (`exact`/`day`) is never overwritten by an `approximate` one, and an approximate one is replaced as soon as an absolute date arrives (e.g. Workday detail `startDate` / JSON-LD `datePosted`). `first_seen_at` is written once by JobWatch and never changes. The UI labels approximate dates ("~3 days ago, estimated"). |
| Dedicated New/Unreviewed inbox | `applications.reviewed_at` (NULL = unreviewed). **Inbox** = unreviewed ∧ not merged ∧ `location_scope` in visible scopes. Any status change sets `reviewed_at`; "Mark reviewed" leaves the inbox without changing status (`POST /jobs/review`, undo via `/jobs/unreview`). Endpoints: `GET /inbox`, `GET /inbox/summary`. Saved views support `page='inbox'`. |
| Bookmarklet + manual entry in v1 | `POST /jobs/capture` (Bearer capture token, parses the JSON-LD the bookmarklet sends, **never fetches the URL**) and `POST /jobs`. Both go through the single ingest path with sources `capture` / `manual`. |
| Availability ⟂ application status | `jobs.availability` / `postings.availability` are system-owned; `applications.status_id` is user-owned. Ingest has no code path that writes `status_id` (enforced by tests). "Expired/Closed" is a badge, not a status. |
| Timeline preserves history | `application_events` is append-only, **enforced by a DB trigger** (UPDATE raises). Status changes write `from_status_id`/`to_status_id` in the same transaction. Merges re-parent events; nothing is deleted. |
| India filter enforced everywhere | One policy: `app/jobs/scope.py` + setting `location_scope` (default `countries=["IN"]`, `include_unknown=true`, `include_remote_anywhere=false`). Ingest stores `jobs.location_scope` (`in_scope`/`out_of_scope`/`unknown`). Job list, inbox, facets and dashboard default to the visible scopes; notifications use **only `in_scope`**; Phase 3 Workday discovery derives the country facet from the same setting. Changing the setting re-evaluates every job in one transaction. Per-job user override: `scope_overridden`. |

## 2. Other architectural decisions taken in Phase 2

1. **Transaction per request.** `SessionDep` commits on success / rolls back on error. Services
   never call `commit()`. FastAPI 0.162 runs `yield`-dependency teardown *after* the response by
   default, which would hide commit failures. We use `Depends(get_session, scope="function")`
   (verified) so a failing commit returns an error.
2. **Optimistic concurrency** on `applications.row_version` (status changes, edits, Kanban moves)
   → `409 conflict` when stale.
3. **Auth:** single admin user (argon2 hash in env) + signed HttpOnly cookie. CSRF uses the
   required custom header `X-JobWatch-Client: web` on mutating requests. The bookmarklet uses a
   separate bearer token.
4. **Search:** Postgres FTS (`jobs.search_vector`) maintained by a trigger (title/company A,
   skills B, department C, description D) plus `pg_trgm` for fuzzy title similarity. No external
   search engine.
5. **IDs:** `bigint identity`. **Enums:** native Postgres enums for code-driving values; statuses are a
   lookup table (configurable).
6. **Integration tests use real Postgres** (`jobwatch_test`, migrated via Alembic). Each test runs in a
   rolled-back transaction, and the app's sessions join it through SAVEPOINTs. No mocked repositories.
   HTTP tests go through the full ASGI stack with real auth.

## 2a. Decisions made during integration review

1. **Duplicate suggestions are cross-source only.** Two different external ids on the *same* source
   are distinct requisitions by definition, so similar titles on one career site ("Software Engineer"
   vs "Software Engineer II") are never suggested. Without this rule, 100+ monitored companies would
   flood the queue.
2. **Merging copies timeline events instead of moving them.** The timeline is append-only at the DB
   level, so the absorbed job's events are copied onto the survivor with their original
   `occurred_at` and `payload.merged_from_job_id`. The originals remain on the tombstone. Notes,
   contacts, interviews, reminders, salary records and tags are moved.
3. **Primary posting** (drives the job's displayed fields): the first open posting, ranked by source:
   career site, then job-board APIs, then capture, then manual.
4. **Same-status change** is a no-op (no event, no version bump) but marks the job reviewed.
   A status change resets the card's Kanban position (the old position belonged to the old column).
5. **Auto follow-up** fires once, the first time a job enters the Applied category.
6. **Company names and aliases share one case-insensitive namespace** (409 on collision).
7. **Dashboard counts** apply the India scope and exclude merged jobs. "Upcoming" ignores the scope,
   so a tracked out-of-scope job's interview still shows. Funnel stages count jobs that actually
   reached each stage (not nested).

## 3. Requirements that cannot be fulfilled as originally intended

| Requirement | Status |
|---|---|
| AmbitionBox salary enrichment | No authorized access exists (Phase 1 research). The provider interface and provenance model are built; the AmbitionBox/API.SH provider stays **disabled**. v1 salary sources: employer-provided (from posting text) and user-entered. |
| LinkedIn / Naukri / Indeed / Internshala / Wellfound / Foundit integration | No permitted programmatic access. v1 offers bookmarklet capture and manual entry; email-alert ingestion is v1.1. |
| Login rate limiting | Deferred to Phase 6 (security hardening, with the reverse proxy). |

## 4. Ownership (Phase 2)

**Lead (central, do not edit without the lead):** `app/models/**`, `migrations/**`, `app/core/**`,
`app/main.py`, all `schemas.py`, router **signatures** (paths, params, response models),
`app/discovery/contracts.py`, `app/discovery/configs.py`, `app/applications/lifecycle.py`,
`app/jobs/scope.py`, `app/settings/registry.py`, `tests/conftest.py`.

| Agent | Implements (service + router bodies + tests) | Owns files |
|---|---|---|
| **Catalog** | companies (CRUD, aliases, slugging, CSV import preview/commit, archive, manual scrape → queued run, runs/history), statuses, settings (incl. scope re-evaluation), saved views, resume versions, tags, sources | `app/companies/service.py`, `app/companies/csv_import.py`, `app/statuses/service.py`, `app/settings/service.py`, `app/sources/service.py`, router bodies of those modules, `tests/catalog/**` |
| **Jobs & ingest** | ingest service (upsert, versions, normalizers, dedupe suggestions, closure rules, scope), job queries/search/facets, inbox, manual entry, capture, versions & diffs, duplicates & merge, salary view + user-entered salary + employer salary parsing | `app/discovery/ingest.py`, `app/discovery/normalize/**`, `app/jobs/service.py`, `app/jobs/queries.py`, `app/jobs/merge.py`, `app/jobs/capture.py`, `app/salary/service.py`, `app/salary/providers/**`, `app/jobs/router.py` bodies, `tests/jobs/**` |
| **Applications** | status transitions, inbox review side-effects, milestones, follow-up auto-reminder, bulk status, tracker, board & moves, timeline, notes, contacts, interviews, reminders | `app/applications/service.py`, `app/applications/board.py`, `app/reminders/service.py`, router bodies of `applications` & `reminders`, `tests/applications/**` |
| **Insights** | dashboard aggregates, health overview/runs/notifications, salary provider listing | `app/dashboard/service.py`, `app/health/service.py`, router bodies of `dashboard` & `health`, `tests/insights/**` |

Cross-module calls go through the other module's **service functions**. Where a needed function
does not exist yet, call the documented contract (lifecycle/scope/registry) or report the gap to
the lead. Never edit another agent's files.

## 5. Coding conventions

- Services are plain async functions `async def verb_noun(session: AsyncSession, ..., *, now=None)`.
  Inject `now` for testability, use `flush()` and never `commit()`, and raise `app.core.errors.*`.
- Routers stay thin: parse → service → schema. No SQL in routers.
- All timestamps are timezone-aware UTC; day-bucketing uses the `timezone` setting.
- Every endpoint gets at least one HTTP integration test (happy path + main failure mode). Every
  invariant listed above gets a dedicated test.
- `uv run ruff check app tests` and `uv run pytest -q` must pass before handing back.
