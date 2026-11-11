# JobWatch — Phase 1 Research Findings

Research date: **2026-11-11**. Labels: **VERIFIED** = confirmed by a live request or the official page;
**REPORTED** = from docs/blogs/vendor material, not independently confirmed; **UNVERIFIED** = could
not confirm, must be checked before building on it.

---

## 1. Workday career sites (primary discovery mechanism)

The public career site front-end is backed by an unauthenticated JSON API ("cxs"). Tested live against
NVIDIA (`wd5`) and Salesforce (`wd12`).

| Aspect | Finding |
|---|---|
| URL anatomy | `https://{tenant}.wd{N}.myworkdayjobs.com/[{locale}/]{site}` — tenant = first host label, `wdN` must be exact (wrong `wdN` → 422), site = first non-locale path segment. **VERIFIED** |
| One tenant, many sites | Salesforce exposes 9 sites (External_Career_Site, Tableau, Slack…). Config is **per site**, not per tenant. **VERIFIED** |
| List endpoint | `POST /wday/cxs/{tenant}/{site}/jobs`, body `{"appliedFacets":{},"limit":20,"offset":0,"searchText":""}`. No auth/CSRF. **VERIFIED** |
| Page size | **Max 20**; `limit: 21` → `HTTP_400`. **VERIFIED** |
| `total` | Only populated on the first page (later pages return `0`). For large tenants it appears capped at 2000 while deeper offsets still return data. → paginate until an empty page. **VERIFIED** |
| List item | `title, externalPath, locationsText ("5 Locations" for multi-location), postedOn ("Posted 3 Days Ago"), bulletFields ["JR2029481"]`. **VERIFIED** |
| Detail endpoint | `GET /wday/cxs/{tenant}/{site}{externalPath}` → `jobPostingInfo` {title, jobDescription (HTML), jobReqId, location, country, timeType, startDate (absolute), externalUrl, canApply…}. `remoteType`/`additionalLocations` present when set (**REPORTED**). **VERIFIED** |
| Dates | `postedOn` is relative ("Posted 30+ Days Ago" cap). `startDate` and the page's JSON-LD `datePosted` are absolute. We store `first_seen_at` ourselves and treat `posted_at` as approximate. |
| Facets | `appliedFacets` with opaque IDs, e.g. country facet `locationHierarchy1` → India returned 242 of NVIDIA's ~2,700 jobs. Facet names and IDs **differ per tenant** → resolve "India" → ID at setup time and cache it. **VERIFIED** |
| JSON-LD | Public job HTML page embeds schema.org `JobPosting`. Useful fallback. **VERIFIED** |
| Sitemap | `/{site}/siteMap.xml` lists all job URLs; explicitly allowed by robots.txt. Cheap fallback for "new ID" detection. **VERIFIED** |
| Anti-bot | Behind Cloudflare; plain HTTP with a normal UA worked, no 429s across ~20 requests incl. a burst of 8. **VERIFIED** |
| robots.txt | Site paths explicitly `Allow`ed; `/wday/cxs/` neither allowed nor disallowed (grey area). We stay polite: descriptive UA, 0.5–1 s between requests, jittered schedule, `Retry-After` honoured. |
| Errors | 400 (bad body), 404 `S21` (bad site), 422 (wrong wdN) → **config errors, don't retry**. 429/5xx → retry with backoff. |

**Implication for 30-minute polling.** A large tenant unfiltered is ~135 list requests per run. With a
country facet (e.g. India) it is ~12. Detail is fetched only for **new** postings or when the
list-level fingerprint changes, plus a weekly refresh. Estimated steady-state load for 100 companies:
~1,000–2,000 lightweight requests per 30 min, spread evenly — trivial for one small VPS.

**Other ATSes (cheap, official public JSON APIs — REPORTED, not verified):** Greenhouse
(`boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true`), Lever (`api.lever.co/v0/postings/{co}`),
Ashby (`api.ashbyhq.com/posting-api/job-board/{org}`), SmartRecruiters. Per your instruction we will
**not** build a generic ATS framework. Because the extractor registry is config-driven, adding one of these
later is a single ~100-line module, if companies on your list use them.

**Custom career pages — preferred strategies, in order:** (1) schema.org JobPosting JSON-LD,
(2) sitemap/RSS, (3) embedded JSON (`__NEXT_DATA__`), (4) declarative CSS selectors,
(5) headless Chromium (only when the page cannot render without JS).

---

## 2. External job platforms

| Platform | Job-seeker search API? | ToS on automation | Verdict for v1 |
|---|---|---|---|
| LinkedIn | No (partner-only, employer/ATS APIs) | Prohibits bots/automated methods | **URL capture / manual** (+ optional email-alert ingestion) |
| Naukri | No public API found | Prohibits crawling/scraping (REPORTED; terms page 403s) | **URL capture / manual** (+ optional email alerts) |
| Indeed | Publisher API retired; `/rss` and `/viewjob` disallowed in robots.txt | Restrictive | **URL capture / manual** (+ optional email alerts) |
| Internshala | No | Explicitly bans bots/scrapers | **URL capture / manual** |
| Wellfound | No | Bans automated harvesting | **URL capture / manual** |
| Foundit | None found (pages 403) | UNVERIFIED | **URL capture / manual** |
| Instahyre / Cutshort / Hirist | None found | UNVERIFIED | **URL capture / manual** |
| Google Cloud Talent Solution | Employer-side only | — | Not feasible |
| **Adzuna** | **Yes**, official REST (`app_id`/`app_key`, free tier) | Permitted by API terms | **Integrate** — India support (`/in/`) to be confirmed with a key |
| **Himalayas** | **Yes**, free JSON API (60 rpm, 10k/day w/ key) | Attribution link; backend use; remove expired jobs | **Integrate** (remote roles) |
| **Remotive** | **Yes**, public API | ≤ 4 fetches/day, 24 h delay, must attribute & link | **Integrate** (remote roles, 4×/day schedule) |
| **RemoteOK** | **Yes**, `remoteok.com/api` | Must attribute with followed link | **Integrate** (remote roles) |
| **HN "Who is hiring"** | **Yes**, Algolia HN API (public) | — | **Integrate** (monthly thread) |
| Jooble | Yes, key by application | UNVERIFIED | Defer until key approved |
| Arbeitnow | Yes, free | Europe-focused | Skip (low value for India) |

**Conclusion.** None of the India-centric portals (LinkedIn, Naukri, Indeed, Internshala, Foundit,
Wellfound) offers a permitted programmatic job-search integration. JobWatch therefore must not depend on
them. For these, the permitted workflows are:

1. **"Save to JobWatch" bookmarklet / browser extension (v1).** You open the job in your own browser.
   The bookmarklet reads the page's schema.org JobPosting JSON-LD if present, falling back to
   title/selection/URL, and POSTs it to your JobWatch API. Nothing is fetched server-side. This is the
   same as you copy-pasting, just faster.
2. **Manual add form (v1)** with paste-the-description and inline field editing.
3. **Email-alert ingestion (optional, v1.1).** Subscribe to LinkedIn/Naukri/Indeed alerts with a
   dedicated mailbox, and JobWatch parses the alert emails over IMAP. These are emails sent to you, so
   this is legitimate, but each sender's format needs its own parser and they change. Recommended as a
   fast follow, not a v1 blocker.
4. **Server-side URL fetch** only for domains on an allowlist whose terms/robots permit it (e.g. company
   career sites, Greenhouse/Lever boards). Never for LinkedIn/Naukri/Indeed/Internshala.

---

## 3. AmbitionBox salary data

| Question | Finding |
|---|---|
| Official public / partner API? | **None found.** No developer portal, no documented lookup, no public licensing programme. |
| Terms of Use | Explicitly prohibit automated crawling, scraping, extraction "not authorized in writing", and access other than through provided interfaces. Also prohibit deep links "without prior permission". **VERIFIED** |
| robots.txt | Generic UA allowed on most `/salaries` pages, but AI/search bots blocked and site is behind bot protection. Terms prevail. |
| **API.SH** | Real, working product: `GET /ambitionbox/v1/salary`, `$2 / 1,000 credits` (1,000 free/month), `x-api-key`, returns avg/min/max CTC, percentiles, experience bands, report counts, `fetched_at`. **But** its own terms say it is *not affiliated* with the sources and puts all legal responsibility and indemnity on the customer → it is a **scraper**, not an authorized integration. |
| Apify actors | Community scrapers ($1.45–$9 / 1k records), several need residential proxies to get past Akamai. Not authorized. |
| Glassdoor | Partner API closed to new applicants. Not available. |
| Levels.fyi | API on request / enterprise; terms require a written agreement for programmatic access. |
| Payscale, LinkedIn Salary, Indeed Salary, 6figr | No self-serve official API found. |

**Conclusion.** No authorized AmbitionBox integration exists today, and API.SH is not suitable under
your "authorized only" rule. Per your spec §5.4, we will:

- Build the `SalaryProvider` interface and the enrichment pipeline in full (provenance, match quality,
  confidence, caching, UI labelling).
- Ship **`employer_posting`** (parses compensation from the job text — the only "Employer-provided"
  source) and **`user_entered`** (you paste a figure + source URL after looking it up yourself; one click
  opens an AmbitionBox / Levels.fyi **search** for the company in your browser, without the app fetching
  anything).
- Ship a **disabled `ambitionbox_api` provider stub** (adapter-shaped for API.SH) that can be turned on
  only if you explicitly decide to accept that risk, or swapped for a licensed source if Info Edge ever
  grants one. Recommended next step for you: email AmbitionBox / Info Edge partnerships to ask about
  personal-use licensing.
- Show "Not available" whenever there is no reliable figure.

---

## 4. Hosting (see proposal §8 for the decision)

| Option | ~Monthly | Notes |
|---|---|---|
| **Hetzner CPX22** (2 vCPU / 4 GB / 80 GB), Helsinki/Germany | **€17–20** | IPv4 included. Lowest cost per GB of RAM. You patch the OS (unattended-upgrades). |
| Hetzner CPX22, Singapore | €27 | Lower latency from India; not needed for a poller. |
| Hetzner CPX12 (2 GB), EU | €10 | Workable if Chromium is rarely needed; tight. |
| Railway Hobby | ~$40–45 | Zero-ops, but 2–3× the cost for this RAM profile. |
| Render (3 services + Postgres) | ~$40+ (UNVERIFIED) | |
| Fly.io + self-run Postgres | ~$25–30 | Managed PG from $38. |
| Oracle Always Free | $0 | Reclaim/termination risk — not for primary. |
| Supabase/Neon free PG | $0 | Pausing/compute caps — not for an always-on worker. |

Backups: Cloudflare R2 (10 GB free, zero egress) ≈ $0–1/month. Monitoring: Healthchecks.io (dead-man's
switch) + UptimeRobot, both on free tiers.
