# JobWatch career-site verification protocol

Goal: for each assigned company, find and VERIFY the official careers site(s), identify the career-site
platform, and determine whether India-only job listings can be retrieved automatically and politely
from a public, unauthenticated data source that the site itself uses. This is verification, NOT
scraping: **max ~6 HTTP requests per company**, ≥1 s between requests to the same host, never more
than one page of results (use limit=1..20).

## Rules (hard)
- Use `curl -sS -m 20 -L -A "Mozilla/5.0 (compatible; JobWatch/0.1; personal job tracker)"` — ALWAYS this honest
  User-Agent. Never impersonate a browser or change the UA to get past a block. If the site blocks or
  challenges this UA (403/429/challenge page), STOP probing that host and record `access: "bot_protected"`
  (or `blocked`) — that itself is the finding.
- Never bypass login, CAPTCHA, Cloudflare/Akamai challenges, or access controls. If blocked → record it.
- Fetch and read `robots.txt` of the host that serves job data. Record whether the job listing / API path
  is allowed, disallowed, or not mentioned.
- Treat any URL found in search results or third-party lists as a CANDIDATE. Verify it resolves (follow
  redirects) to a page/host owned by the company or its ATS vendor. Do not use LinkedIn/aggregator URLs.
- Do not write files anywhere except your assigned output JSON file.
- Do not modify the git repo.

## Platform detection hints
Look at the final URL, HTML (script src, iframes, `window.__*` globals, meta generator), and network-visible
API paths referenced in page source. Known platforms and how to probe them:

| platform | fingerprint | probe (1 request, India filter if possible) |
|---|---|---|
| workday | `*.wdN.myworkdayjobs.com/<site>` or `myworkdaysite.com` | `POST https://{host}/wday/cxs/{tenant}/{site}/jobs` body `{"appliedFacets":{},"limit":1,"offset":0,"searchText":"India"}` → `total`; also inspect `facets` for a country facet (e.g. `locationHierarchy1`, `Location_Country`) and its India value id + count |
| greenhouse | `boards.greenhouse.io/<token>`, `job-boards.greenhouse.io`, `grnh.se`, `gh_jid=` | `GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs` → count jobs whose `location.name` mentions India/Indian city |
| lever | `jobs.lever.co/<co>` | `GET https://api.lever.co/v0/postings/{co}?mode=json` → count India locations (also try `api.eu.lever.co`) |
| ashby | `jobs.ashbyhq.com/<org>` | `GET https://api.ashbyhq.com/posting-api/job-board/{org}` → count India locations |
| smartrecruiters | `jobs.smartrecruiters.com/<Co>`, `careers.smartrecruiters.com` | `GET https://api.smartrecruiters.com/v1/companies/{id}/postings?country=in&limit=1` → `totalFound` |
| eightfold | `*.eightfold.ai`, `/careers?` with `pcsx`/`apply/v2` | `GET https://{host}/api/apply/v2/jobs?domain={domain}&location=India&num=1` (or `/api/pcsx/search?...`) — note if public |
| oracle_hcm | `*.oraclecloud.com/hcmUI/CandidateExperience` | `GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList&finder=findReqs;siteNumber={CX_xxxx},limit=1,locationId=...` or keyword India → TotalJobsCount |
| successfactors | `career*.successfactors.*`, `jobs.sap.com`-style Career Site Builder (`/search/?q=&locationsearch=`) | check `/sitemap.xml`, RSS (`/services/rss/job/?locale=en_US&keywords=`), or search HTML |
| phenom | `cdn.phenompeople.com`, `phApp`, `/widgets` | page embeds `phApp.ddo` JSON with jobs; note `refNum`; India filter via `/search-results?location=India` |
| avature | `*.avature.net` | listing HTML / RSS `SearchJobs/feed` |
| icims / taleo / jobvite / keka / darwinbox / zoho_recruit / freshteam / others | as named | record and probe if a public JSON/RSS exists |
| custom_api | company's own site with a JSON API the page calls (e.g. amazon.jobs `/en/search.json?country[]=IND`, Google `careers.google.com` → `www.google.com/about/careers/applications/jobs/results?location=India`, Apple `jobs.apple.com/api/...`, Microsoft `jobs.careers.microsoft.com`/`apply.careers.microsoft.com`, Uber `/api/loadSearchJobsResults`, Jane Street `/jobs/main.json`) | 1 request with India filter; note headers needed (e.g. CSRF header) — if it needs a session/cookie handshake or anti-bot token, mark `fragile` |
| custom_html | server-rendered HTML listings, maybe JSON-LD JobPosting | 1 request; note JSON-LD presence |
| linkedin_only / none | no own listings (redirects to LinkedIn etc.) | — |

Companies can have **multiple portals** (e.g. separate Workday sites for subsidiaries, campus/university
portals, or India-specific portals). Record each relevant portal as its own entry (same company name,
different `site_label`). Include India-specific or campus portals if they exist.

## India eligibility
Determine HOW India jobs can be isolated: `source_filter` (API facet/param), `location_text` (filter on
returned location strings), or `none`. Note how remote jobs express eligibility (e.g. "Remote - India").

## Output: a JSON array written to your assigned file. One object per portal:
```json
{
  "company": "Nvidia",
  "site_label": "main",                       // "main", "campus", "india", subsidiary name...
  "careers_url": "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite",   // verified public listings URL
  "landing_url": "https://www.nvidia.com/en-us/about-nvidia/careers/",              // marketing page if different
  "platform": "workday",                       // from the table, or "custom_api"/"custom_html"/"linkedin_only"/"unknown"
  "tenant": "nvidia",                          // workday tenant / greenhouse token / lever co / ashby org / SR company id / oracle siteNumber / eightfold domain
  "site_id": "NVIDIAExternalCareerSite",       // workday site, oracle site number, etc. (null if n/a)
  "api_endpoint": "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs",
  "india_filter": "source_filter",             // source_filter | location_text | none
  "india_filter_detail": "facet locationHierarchy1=2fcb99c455831013ea52b82135ba3266 (India)",
  "india_jobs_observed": 242,                  // number from the probe, null if unknown
  "total_jobs_observed": 2000,
  "robots": "not_mentioned",                   // allowed | disallowed | not_mentioned | unavailable
  "robots_detail": "Allow: /NVIDIAExternalCareerSite/; /wday/cxs not mentioned",
  "access": "open",                            // open | needs_headers | needs_session | bot_protected | login_required | blocked
  "verification": "verified_live",             // verified_live (probe returned data) | verified_page (page loads, no data probe) | unverified | failed
  "support": "supported",                      // supported (existing/simple platform adapter) | adapter_needed (feasible, platform/custom adapter required) | fragile (works but anti-bot/session dependent) | not_feasible | manual_only
  "notes": "short, factual"
}
```
Be accurate over fast. If unsure, say so in notes and lower `verification`. Do not invent tenant IDs — only record what you observed.
