# JobWatch — Database Schema (Phase 1 proposal)

> **Phase 2 note:** the implemented schema lives in `backend/app/models/` and `backend/migrations/`.
> Additions since this proposal: `duplicate_suggestions`, `postings.requisition_id` /
> `canonical_apply_url`, `jobs.location_scope` / `scope_overridden` / `description_text` /
> `posted_at_raw`, `applications.reviewed_at` (inbox), `scrape_runs.source_id` /
> `postings_in_scope`, and the `capture` source kind. See [Phase 2 decisions](../phase2/decisions.md).

PostgreSQL 16. All timestamps are `timestamptz` (stored UTC, rendered in the user's
timezone from `settings`). Primary keys are `bigint generated always as identity`
(compact, index-friendly; this is a single-tenant app, so UUIDs buy nothing).
Enumerations that the user may want to extend are **lookup tables**; enumerations
that drive code paths are **Postgres enums / check constraints**.

## 1. Entity overview

```
                ┌──────────────┐        ┌───────────────┐
                │  companies   │1──────*│  scrape_runs  │
                └──────┬───────┘        └───────────────┘
                       │1
                       │*
┌──────────┐ 1   * ┌───┴────┐ 1     * ┌───────────────┐ 1   * ┌──────────────┐
│ sources  │───────│postings│─────────│ (jobs)        │       │ job_versions │
└──────────┘       └───┬────┘  * ── 1 └──────┬────────┘       └──────▲───────┘
                       │1                    │1                     │*
                       └─────────────────────┼──────────────────────┘
                                             │1                (versions belong to a posting)
                         ┌───────────────────┼─────────────────────┬──────────────┐
                         │1                  │*                    │*             │*
                  ┌──────┴───────┐    ┌──────┴────────┐   ┌────────┴──────┐ ┌─────┴────────┐
                  │ applications │    │salary_records │   │   reminders   │ │ job_tags     │
                  └──────┬───────┘    └───────────────┘   └───────────────┘ └──────────────┘
          ┌──────────┬───┴──────┬────────────┬───────────┐
          │*         │*         │*           │*          │
   ┌──────┴─────┐┌───┴────┐┌────┴─────┐┌─────┴─────┐┌────┴──────────┐
   │ app_events ││ notes  ││interviews││ contacts  ││resume_versions│(FK from applications)
   └────────────┘└────────┘└──────────┘└───────────┘└───────────────┘

  statuses (lookup) · settings (kv) · saved_views · notification_outbox · tags
```

Key modelling decisions:

| Concern | Decision |
|---|---|
| A job seen on several sources | `jobs` is the canonical opportunity; `postings` is one listing on one source. Many postings → one job. |
| Repeated scraping must not duplicate | `postings` has `UNIQUE (source_id, company_id, external_id)`; ingest is an upsert keyed on it. |
| Job versions | `job_versions` are per **posting** (each source has its own text). Version is only written when the normalized content hash changes. `jobs` holds a denormalized "current view" for fast filtering/search. |
| Availability vs. my status | `jobs.availability` (open / closed / unknown) is system-owned. `applications.status_id` is user-owned. Nothing in the scraper ever writes to `applications.status_id`. |
| Application history | `application_events` is append-only; status changes are written in the same transaction as the status update. |
| Salary provenance | Never stored on `jobs`. Every figure is a `salary_records` row with `provenance`, `provider`, `source_url`, `retrieved_at`, match quality and confidence. |
| Every job has a company | Aggregator / manual jobs resolve to a `companies` row; unknown companies are created with `monitoring_enabled = false`. |
| One pipeline record per job | `applications.job_id` is `UNIQUE`; the row is created in the same transaction as the job (status `new`), so filters never need outer joins and bulk updates are trivial. |

## 2. DDL

```sql
-- ─────────────────────────────── extensions ───────────────────────────────
create extension if not exists pg_trgm;      -- fuzzy title/company matching, ILIKE acceleration
create extension if not exists unaccent;     -- search normalisation
create extension if not exists citext;       -- case-insensitive unique names/slugs

-- ─────────────────────────────── enums (code-driving) ─────────────────────
create type extractor_kind      as enum ('workday','jsonld','html','custom','none');
create type health_status       as enum ('unknown','healthy','degraded','failing','paused');
create type source_kind         as enum ('career_site','job_board_api','feed','email_alert','url_import','manual');
create type availability        as enum ('open','closed','unknown');
create type employment_type     as enum ('full_time','part_time','contract','internship','temporary','apprenticeship','other','unknown');
create type work_arrangement    as enum ('onsite','hybrid','remote','unknown');
create type date_precision      as enum ('exact','day','approximate','unknown');  -- Workday gives "Posted 3 Days Ago"
create type status_category     as enum ('inbox','considering','applied','in_process','offer','closed_negative','closed_neutral','archived');
create type run_trigger         as enum ('schedule','manual','retry','test');
create type run_status          as enum ('queued','running','succeeded','partial','failed','skipped','quarantined');
create type salary_provenance   as enum ('employer_provided','external_estimate','user_entered');
create type salary_period       as enum ('annual','monthly','hourly','unknown');
create type match_quality       as enum ('exact','close','broad','company_wide','unknown');
create type reminder_kind       as enum ('deadline','follow_up','interview_prep','interview','recruiter','custom');
create type reminder_state      as enum ('pending','done','dismissed');
create type outbox_state        as enum ('pending','sending','sent','failed','suppressed');

-- ─────────────────────────────── companies ────────────────────────────────
create table companies (
  id                    bigint generated always as identity primary key,
  name                  citext      not null,
  slug                  citext      not null unique,          -- used in URLs, CSV import, custom extractor names
  website_url           text,
  careers_url           text,
  logo_url              text,
  description           text,
  monitoring_enabled    boolean     not null default false,
  extractor             extractor_kind not null default 'none',
  extractor_config      jsonb       not null default '{}'::jsonb,  -- validated by a per-extractor Pydantic model
  check_interval_min    integer     not null default 30 check (check_interval_min between 10 and 1440),
  next_check_at         timestamptz,                         -- scheduler picks rows where next_check_at <= now()
  health                health_status not null default 'unknown',
  consecutive_failures  integer     not null default 0,
  last_checked_at       timestamptz,
  last_success_at       timestamptz,
  last_error            text,
  open_jobs_count       integer     not null default 0,      -- maintained by ingest, same txn
  baseline_jobs_count   integer,                             -- rolling median of recent successful runs (anomaly detection)
  notes                 text,
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now(),
  archived_at           timestamptz,
  constraint extractor_requires_url check (extractor = 'none' or careers_url is not null)
);
create index companies_due_idx  on companies (next_check_at) where monitoring_enabled and archived_at is null;
create index companies_name_trgm on companies using gin (name gin_trgm_ops);

create table company_aliases (                  -- "Amazon Web Services" → amazon; used by aggregator/import matching
  company_id bigint not null references companies(id) on delete cascade,
  alias      citext not null unique,
  primary key (company_id, alias)
);

-- ─────────────────────────────── sources ──────────────────────────────────
-- One row per origin platform. Career sites share a single 'career_site' row;
-- the company dimension lives on postings.
create table sources (
  id          bigint generated always as identity primary key,
  key         citext      not null unique,     -- 'career_site','manual','url_import','remotive','email:naukri', ...
  name        text        not null,
  kind        source_kind not null,
  enabled     boolean     not null default true,
  config      jsonb       not null default '{}'::jsonb,   -- non-secret config; secrets come from env
  homepage    text,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

-- ─────────────────────────────── jobs (canonical) ─────────────────────────
create table jobs (
  id                   bigint generated always as identity primary key,
  company_id           bigint not null references companies(id),
  primary_posting_id   bigint,                         -- FK added after postings exists; drives the denormalized view
  -- denormalized current view (copied from primary posting's latest version by ingest)
  title                text not null,
  title_normalized     text not null,                  -- lowercased, seniority/punctuation-normalised; dedupe + grouping
  department           text,
  location_text        text,
  locations            jsonb not null default '[]',    -- [{city,region,country_code,raw}]
  country_codes        text[] not null default '{}',   -- for fast filtering (GIN)
  employment_type      employment_type  not null default 'unknown',
  work_arrangement     work_arrangement not null default 'unknown',
  experience_min_years numeric(4,1),
  experience_max_years numeric(4,1),
  skills               text[] not null default '{}',
  posted_at            timestamptz,
  posted_at_precision  date_precision not null default 'unknown',
  deadline_at          timestamptz,
  apply_url            text,
  has_employer_salary  boolean not null default false, -- convenience flag; figures live in salary_records
  -- lifecycle (system-owned)
  availability         availability not null default 'open',
  first_seen_at        timestamptz not null default now(),
  last_seen_at         timestamptz not null default now(),
  closed_at            timestamptz,
  last_changed_at      timestamptz not null default now(),   -- last meaningful content change (for "updated" badge)
  merged_into_job_id   bigint references jobs(id),           -- manual merge leaves a tombstone, never deletes history
  search_vector        tsvector,                             -- title(A) company(A) skills(B) department(C) description(D)
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now()
);
create index jobs_company_idx        on jobs (company_id) where merged_into_job_id is null;
create index jobs_first_seen_idx     on jobs (first_seen_at desc) where merged_into_job_id is null;
create index jobs_posted_idx         on jobs (posted_at desc nulls last);
create index jobs_availability_idx   on jobs (availability);
create index jobs_country_gin        on jobs using gin (country_codes);
create index jobs_skills_gin         on jobs using gin (skills);
create index jobs_search_gin         on jobs using gin (search_vector);
create index jobs_title_trgm         on jobs using gin (title_normalized gin_trgm_ops);  -- cross-source dedupe candidates

-- ─────────────────────────────── postings (job ↔ source) ──────────────────
create table postings (
  id                  bigint generated always as identity primary key,
  job_id              bigint not null references jobs(id),
  source_id           bigint not null references sources(id),
  company_id          bigint not null references companies(id),
  external_id         text   not null,          -- Workday jobReqId / externalPath; URL hash for url_import; uuid for manual
  source_url          text   not null,          -- original listing URL (preserved forever)
  apply_url           text,
  listing_fingerprint text,                     -- hash of list-page fields; change ⇒ refetch detail
  content_hash        text,                     -- hash of normalized detail content; change ⇒ new version
  current_version_id  bigint,
  availability        availability not null default 'open',
  first_seen_at       timestamptz not null default now(),
  last_seen_at        timestamptz not null default now(),
  detail_fetched_at   timestamptz,
  missing_since       timestamptz,              -- first complete run where it was absent
  consecutive_misses  integer not null default 0,
  closed_at           timestamptz,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now(),
  constraint postings_identity unique (source_id, company_id, external_id)
);
create index postings_job_idx            on postings (job_id);
create index postings_company_open_idx   on postings (company_id) where availability = 'open';
create index postings_detail_due_idx     on postings (detail_fetched_at) where availability = 'open';
alter table jobs add constraint jobs_primary_posting_fk
  foreign key (primary_posting_id) references postings(id) deferrable initially deferred;

-- ─────────────────────────────── job_versions ─────────────────────────────
create table job_versions (
  id                   bigint generated always as identity primary key,
  posting_id           bigint not null references postings(id) on delete cascade,
  version_no           integer not null,
  content_hash         text not null,
  captured_at          timestamptz not null default now(),
  scrape_run_id        bigint,                          -- null for manual/url import
  title                text not null,
  department           text,
  location_text        text,
  locations            jsonb not null default '[]',
  employment_type      employment_type  not null default 'unknown',
  work_arrangement     work_arrangement not null default 'unknown',
  experience_text      text,                            -- verbatim requirement sentence(s)
  experience_min_years numeric(4,1),
  experience_max_years numeric(4,1),
  skills               text[] not null default '{}',
  qualifications_required  text,
  qualifications_preferred text,
  salary_text          text,                            -- verbatim employer-provided compensation text, if any
  posted_at            timestamptz,
  posted_at_raw        text,                            -- "Posted 3 Days Ago"
  deadline_at          timestamptz,
  description_html     text,                            -- sanitized original HTML (preserved)
  description_text     text,                            -- plain text for search/diff
  raw_payload          jsonb,                           -- source-native record, for re-normalisation & debugging
  changed_fields       text[] not null default '{}',    -- vs previous version: {'title','salary_text',...}
  constraint job_versions_no unique (posting_id, version_no)
);
alter table postings add constraint postings_current_version_fk
  foreign key (current_version_id) references job_versions(id) deferrable initially deferred;

-- ─────────────────────────────── statuses (configurable) ──────────────────
create table statuses (
  id              smallint generated always as identity primary key,
  key             citext not null unique,          -- 'new','interested','shortlisted','applied','assessment', ...
  label           text   not null,
  category        status_category not null,        -- code keys behaviour off category, never off label
  color           text   not null default 'gray',
  sort_order      smallint not null,
  show_on_board   boolean not null default true,
  is_system       boolean not null default false,  -- seeded statuses can be renamed/recoloured, not deleted
  archived_at     timestamptz
);
-- Seed: new(inbox) interested(considering) shortlisted(considering) applied(applied)
--       assessment(in_process) interviewing(in_process) offer(offer) rejected(closed_negative)
--       withdrawn(closed_neutral) not_interested(archived)
-- "Expired/Closed" is deliberately NOT a status: it is jobs.availability, shown as a separate badge.

-- ─────────────────────────────── resumes / tags ───────────────────────────
create table resume_versions (
  id          bigint generated always as identity primary key,
  label       text not null unique,          -- "SDE-2024-11 (backend focus)"
  file_url    text,                          -- optional link (Drive etc.); file storage out of scope for v1
  notes       text,
  created_at  timestamptz not null default now(),
  archived_at timestamptz
);
create table tags (
  id    bigint generated always as identity primary key,
  name  citext not null unique,
  color text not null default 'gray'
);
create table job_tags (
  job_id bigint not null references jobs(id) on delete cascade,
  tag_id bigint not null references tags(id) on delete cascade,
  primary key (job_id, tag_id)
);

-- ─────────────────────────────── applications (my pipeline record) ────────
create table applications (
  id                  bigint generated always as identity primary key,
  job_id              bigint not null unique references jobs(id),
  status_id           smallint not null references statuses(id),
  status_changed_at   timestamptz not null default now(),
  -- milestone dates: auto-set on first entry into a category, user-editable afterwards
  interested_at       timestamptz,
  shortlisted_at      timestamptz,
  applied_at          timestamptz,
  closed_at           timestamptz,              -- entered closed_* / archived category
  priority            smallint check (priority between 1 and 3),
  starred             boolean not null default false,
  application_url     text,                     -- may differ from job apply_url (e.g. referral portal link)
  resume_version_id   bigint references resume_versions(id),
  referral            text,                     -- free text; structured referrer goes in contacts
  next_action         text,                     -- "Send thank-you note", shown in tracker table
  next_follow_up_at   timestamptz,              -- denormalized from earliest pending follow_up reminder
  outcome_reason      text,                     -- rejection / withdrawal reason
  offer_details       jsonb,                    -- {base, bonus, equity, joining_bonus, currency, deadline, notes}
  board_position      double precision,         -- ordering within a kanban column (fractional indexing)
  row_version         integer not null default 1,   -- optimistic concurrency (If-Match)
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now()
);
create index applications_status_idx   on applications (status_id, board_position);
create index applications_followup_idx on applications (next_follow_up_at) where next_follow_up_at is not null;
create index applications_applied_idx  on applications (applied_at) where applied_at is not null;

-- Append-only activity timeline. occurred_at is user-editable ("I actually applied on Nov 14").
create table application_events (
  id              bigint generated always as identity primary key,
  application_id  bigint not null references applications(id) on delete cascade,
  event_type      text   not null check (event_type in (
                    'discovered','status_changed','field_changed','note_added','interview_scheduled',
                    'interview_updated','reminder_created','reminder_completed','job_updated',
                    'job_closed','job_reopened','salary_added','manual')),
  from_status_id  smallint references statuses(id),
  to_status_id    smallint references statuses(id),
  actor           text not null default 'user' check (actor in ('user','system')),
  summary         text not null,               -- human-readable line for the timeline
  payload         jsonb not null default '{}',
  occurred_at     timestamptz not null default now(),
  created_at      timestamptz not null default now()
);
create index application_events_timeline_idx on application_events (application_id, occurred_at desc);
create index application_events_status_idx   on application_events (to_status_id, occurred_at) where event_type = 'status_changed';
                                                                             -- ^ powers "applications over time" & funnel

create table notes (
  id              bigint generated always as identity primary key,
  application_id  bigint not null references applications(id) on delete cascade,
  body            text not null,               -- markdown
  pinned          boolean not null default false,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);
create index notes_app_idx on notes (application_id, created_at desc);

create table contacts (
  id              bigint generated always as identity primary key,
  application_id  bigint not null references applications(id) on delete cascade,
  role            text not null check (role in ('referrer','recruiter','hiring_manager','interviewer','other')),
  name            text not null,
  title           text,
  email           text,
  phone           text,
  profile_url     text,
  notes           text,
  created_at      timestamptz not null default now()
);

create table interviews (
  id              bigint generated always as identity primary key,
  application_id  bigint not null references applications(id) on delete cascade,
  round_name      text not null,               -- "Technical round 1"
  kind            text not null default 'technical'
                  check (kind in ('assessment','screen','technical','system_design','behavioral','hr','onsite','other')),
  scheduled_at    timestamptz,
  duration_min    integer,
  location        text,                        -- address or meeting link
  outcome         text check (outcome in ('pending','passed','failed','cancelled','no_show')) default 'pending',
  notes           text,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);
create index interviews_upcoming_idx on interviews (scheduled_at) where outcome = 'pending';

-- ─────────────────────────────── reminders ────────────────────────────────
create table reminders (
  id              bigint generated always as identity primary key,
  application_id  bigint references applications(id) on delete cascade,   -- null = standalone task
  interview_id    bigint references interviews(id)  on delete cascade,
  kind            reminder_kind  not null,
  title           text not null,
  notes           text,
  due_at          timestamptz not null,
  notify_at       timestamptz,                 -- default due_at - lead time from settings; null = no push
  state           reminder_state not null default 'pending',
  completed_at    timestamptz,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);
create index reminders_due_idx    on reminders (due_at)    where state = 'pending';
create index reminders_notify_idx on reminders (notify_at) where state = 'pending' and notify_at is not null;

-- ─────────────────────────────── salary ───────────────────────────────────
create table salary_records (
  id                 bigint generated always as identity primary key,
  job_id             bigint not null references jobs(id) on delete cascade,
  provenance         salary_provenance not null,
  provider           text not null,            -- 'job_posting','ambitionbox_manual','user', future provider keys
  is_current         boolean not null default true,
  currency           char(3),
  period             salary_period not null default 'unknown',
  amount_min         numeric(14,2),
  amount_max         numeric(14,2),
  amount_avg         numeric(14,2),
  amount_typical     numeric(14,2),            -- e.g. typical annual CTC
  raw_text           text,                     -- verbatim text as displayed by the source
  role_queried       text,                     -- what we asked the provider for
  role_matched       text,                     -- what the provider actually returned
  role_match         match_quality not null default 'unknown',
  experience_band    text,                     -- "2-4 yrs"
  experience_match   match_quality not null default 'unknown',
  location           text,
  sample_size        integer,                  -- number of salary reports
  confidence         text check (confidence in ('high','medium','low')),
  source_url         text,
  source_updated_at  date,                     -- provider's "last updated"
  retrieved_at       timestamptz not null default now(),
  raw                jsonb,
  constraint employer_salary_from_posting check (provenance <> 'employer_provided' or provider = 'job_posting'),
  constraint salary_amount_order check (amount_min is null or amount_max is null or amount_min <= amount_max)
);
create unique index salary_one_current_per_provider on salary_records (job_id, provider) where is_current;

-- ─────────────────────────────── scraping telemetry ───────────────────────
create table scrape_runs (
  id                 bigint generated always as identity primary key,
  company_id         bigint not null references companies(id) on delete cascade,
  trigger            run_trigger not null,
  status             run_status  not null default 'queued',
  queued_at          timestamptz not null default now(),
  started_at         timestamptz,
  finished_at        timestamptz,
  duration_ms        integer,
  extractor          extractor_kind not null,
  extractor_version  text not null,
  complete           boolean not null default false,  -- true only if every page was fetched & validated
  postings_seen      integer not null default 0,
  postings_new       integer not null default 0,
  postings_updated   integer not null default 0,
  postings_closed    integer not null default 0,
  postings_reopened  integer not null default 0,
  details_fetched    integer not null default 0,
  http_requests      integer not null default 0,
  error_class        text,                     -- 'network','http_4xx','http_5xx','schema_changed','zero_results','count_drop','validation','timeout','internal'
  error_message      text,
  warnings           jsonb not null default '[]',   -- [{code:'missing_field', field:'location', count:12}]
  created_at         timestamptz not null default now()
);
create index scrape_runs_company_idx on scrape_runs (company_id, queued_at desc);
create index scrape_runs_failed_idx  on scrape_runs (queued_at desc) where status in ('failed','quarantined','partial');

-- ─────────────────────────────── notifications ────────────────────────────
-- Transactional outbox: rows are inserted in the same txn as the domain change.
create table notification_outbox (
  id              bigint generated always as identity primary key,
  channel         text not null default 'telegram',
  dedupe_key      text not null,               -- 'jobs_new:run:123', 'reminder:45:2026-11-14T09:00Z', 'scraper_failing:7:2026-11-11'
  event_type      text not null,
  payload         jsonb not null,
  state           outbox_state not null default 'pending',
  attempts        integer not null default 0,
  next_attempt_at timestamptz not null default now(),
  sent_at         timestamptz,
  last_error      text,
  created_at      timestamptz not null default now(),
  constraint outbox_dedupe unique (channel, dedupe_key)
);
create index outbox_due_idx on notification_outbox (next_attempt_at) where state in ('pending','failed');

-- ─────────────────────────────── preferences / views ──────────────────────
create table settings (                        -- typed & validated by a Pydantic Settings model in the app
  key        text primary key,                 -- 'notifications','job_alert_filters','timezone','reminder_defaults', ...
  value      jsonb not null,
  updated_at timestamptz not null default now()
);

create table saved_views (
  id          bigint generated always as identity primary key,
  page        text not null check (page in ('jobs','applications','companies')),
  name        text not null,
  filters     jsonb not null default '{}',
  sort        jsonb not null default '[]',
  columns     jsonb,
  position    smallint not null default 0,
  is_default  boolean not null default false,
  created_at  timestamptz not null default now(),
  unique (page, name)
);
```

## 3. Invariants enforced by the ingest service (one code path for all sources)

1. **Idempotent upsert.** `INSERT … ON CONFLICT (source_id, company_id, external_id) DO UPDATE`.
   Re-running the same scrape produces zero new rows and zero new versions.
2. **Versioning.** A new `job_versions` row is written only when `content_hash` differs from the
   posting's current version. `changed_fields` drives "important update" notifications
   (title, salary_text, deadline_at, availability).
3. **Closure is conservative.** A posting's `consecutive_misses` is incremented only by a run with
   `status = 'succeeded' AND complete = true`. It is closed after **2** such misses spanning
   **≥ 6 h**. Failed, partial, or quarantined runs never touch availability. A closed posting that
   reappears is reopened (and logged as `job_reopened`).
4. **Job availability** = `open` if any non-merged posting is open, else `closed`. Changing
   availability writes a system `application_events` row but never changes `applications.status_id`.
5. **Anomaly quarantine.** If a run returns 0 results where the baseline is > 0, or fewer than
   50 % of `baseline_jobs_count` (and the drop is > 10 postings), the run is marked `quarantined`:
   new/updated postings are still ingested, but misses are not applied, and a health alert is raised.
6. **Cross-source dedupe.** *(Amended in Phase 2. See [docs/phase2/decisions.md](../phase2/decisions.md).)*
   Auto-attach only on identical requisition ID or canonical apply URL at the same company. Similar
   titles and locations produce a `duplicate_suggestions` row for the user to confirm or dismiss.
7. **Transactions.** One transaction per posting batch (≈ 20 postings) so a crash mid-run loses at
   most one batch, and the run is simply retried.
8. **Status change** = one transaction: update `applications` (with `row_version` check) + insert
   `application_events` + set milestone timestamp if first entry into its category + upsert
   default follow-up reminder (if enabled, e.g. "follow up 7 days after Applied").
