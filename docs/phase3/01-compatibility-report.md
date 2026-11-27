# JobWatch — Career-Site Compatibility Report

Verified **2026-11-11** · 91 companies · 97 career portals · Registry: [`data/companies.csv`](../../data/companies.csv)
Raw evidence (per-portal probe results, robots excerpts, India counts): [`data/verification/`](../../data/verification/)

## How this was verified

Four research agents checked every company using one [protocol](../../data/verification/PROTOCOL.md):

- Each found the official portal, followed redirects to the company- or ATS-owned host, and identified the platform.
- Each read `robots.txt` and made **at most ~6 requests per company**, using **an honest `JobWatch/0.1` User-Agent only**, to confirm that India jobs can be isolated.
- Blocks, challenges, logins and CAPTCHAs were recorded and **never circumvented**.

India counts are point-in-time observations from one probe. Some are capped. For example, Workday reports at most 2,000.

Notes on inputs:

- The additional "URL candidates" document you mentioned did not reach me, so every URL was researched independently. Send it and I'll diff it against the registry.
- The two e-mail addresses at the end of your second list (HCL, Infosys) are not career portals and were excluded. The companies themselves are covered.
- **Process slip, disclosed:** for Morgan Stanley and Nike, the agent sent one API probe before reading `robots.txt`, which turned out to disallow the path. Both are classified as *not reliably scrapable*, and JobWatch will not fetch them.

## Summary (reconciled: every company counted exactly once)

Companies are classified by their **best** portal (A > B > C > D), so a company with several portals is counted
once. The per-company roll-up is [`data/company_coverage.csv`](../../data/company_coverage.csv), produced by the
same script, which asserts that the categories sum to the company count.

| Classification | Companies | Portals | What happens |
|---|---|---|---|
| **A. Automatic: shared platform adapter** | **42** | 45 | 6 platform adapters, configuration only |
| **B. Automatic: company-specific adapter** | **11** | 11 | small bespoke adapter each (Wipro + HCLTech share one) |
| **C. Cannot currently be scraped reliably** | **27** | 27 | bookmarklet / manual entry |
| **D. Manual entry / bookmarklet only** | **11** | 14* | bookmarklet / manual entry |
| **Total** | **91** | 97 | |

\* D has 14 portals but 11 companies. LTIMindtree and D. E. Shaw have two manual portals each, and Blinkit's group page
is a manual portal while Blinkit itself is counted in C, through its blocked main portal.
*The earlier draft counted Blinkit in both C and D, which is why it summed to 92 and 37.*

**Bookmarklet / manual entry needed: 38 companies (C 27 + D 11).**

*Update 2026-11-12: IBM and Rippling moved from B to C after the approved provenance check found their inferred endpoints are not referenced by their own careers pages (IBM's careers site also serves a bot challenge). Evidence: [`data/verification/provenance_ibm_rippling.md`](../../data/verification/provenance_ibm_rippling.md).*

### Endpoint access vs. India coverage

Being able to reach a listings endpoint is not the same as having India jobs. A company counts as having **India
coverage** only when its listings contain postings located in India, or remote postings that explicitly list
India. Bare "Remote" was never counted.

| Of the 53 automatable companies (A + B) | Companies |
|---|---|
| India coverage **confirmed** (≥ 1 India-eligible posting observed) | **51** |
| India coverage **unverified** (endpoint works; listings don't establish India eligibility yet) | **1**: HCLTech |
| **No India postings observed** (endpoint works; no India offices or roles) | **1**: Jane Street |

India-eligible openings observed across the 51: about **15,100**, a lower bound since Accenture's count is capped at 2,000.
Accenture, Wipro and Amazon account for about 9,800 of these, mostly IT-services and operations roles. The approved
global title filters will narrow them.

Per portal, `india_coverage` in `data/companies.csv` is one of confirmed, none_observed, unverified or not_applicable.
Only `confirmed` portals are **monitored by default**. Salesforce's subsidiary sites (0 India), Visa's campus site
(unverified), Jane Street and HCLTech start disabled.

## A. Automatic: shared platform adapters (45 portals)

| Platform | Portals | Companies (India jobs observed) | India isolation |
|---|---|---|---|
| **Workday** (21) | 21 | Accenture (2000+, capped), Airbus (213), Adobe (36), Barclays (~304), BlackRock (34), Boeing (4), BrowserStack (28), HPE (~400), Intel (94), Mastercard (~225), Nvidia (242), Red Hat (6), Salesforce (96; subsidiary sites 0 → disabled), Samsung ×2 (31 + 3), Target (71), Visa ×2 (92 + campus), Workday (20), Autodesk (1, **low confidence**) | Country facet resolved per tenant. Location-text fallback where facets are office-level (Barclays, Mastercard, HPE, Red Hat). **"India" text search wrongly matches Indiana**, so text matching uses an Indian-city dictionary with an Indiana/Indianapolis exclusion. |
| **Greenhouse** (12) | 12 | Stripe (45), Databricks (89), Zscaler (78), Rubrik (30), Razorpay (23), Harness (16), Twilio (14), Tower Research (12), Coinbase (10), Airbnb (10), Groww (9), Datadog (8) | Location text (+ offices) |
| **Oracle HCM** (4) | 4 | Oracle (345), JPMorgan Chase (303), Texas Instruments (126), Dell (88) | `locationId` facet (keyword search undercounts: Dell 11 vs 88) |
| **Eightfold** (4) | 4 | Qualcomm (497), Microsoft (234), NetApp (75), Eightfold AI (21) | `location=India`. Two endpoint variants: PCSX (Microsoft, Qualcomm, Eightfold AI) vs `apply/v2` (NetApp) |
| **Lever** (3) | 3 | Meesho (49), CRED (14), Spotify (1) | `country` field + location text |
| **Ashby** (1) | 1 | Snowflake (11) | Location / address country |

## B. Automatic: company-specific adapters (11 portals)

| Company | Source | India observed | Notes |
|---|---|---|---|
| Amazon | `amazon.jobs/en/search.json` | 2,101 | robots allows; must use `normalized_country_code[]=IND` (`country[]` does not filter) |
| Wipro | SuccessFactors Career Site Builder sitemap + job pages | ~5,720 | shared `successfactors_csb` adapter with HCLTech |
| HCLTech | SuccessFactors CSB | unknown | India isolation needs job-page parsing (no location in sitemap) |
| Capgemini | job-stream API used by its search page | 720 | |
| Cisco | Phenom search pages (embedded JSON) | 300 | robots allows search pages |
| AMD | iCIMS/Jibe `/api/jobs` | 207 | **robots `crawl-delay: 5` honoured** |
| Atlassian | public listings JSON used by the careers page | 44 | Many "Remote - Remote" postings without India eligibility are excluded |
| Intuit | `sitemap.xml` + job pages | 27 | robots disallows `/search-jobs/`, so sitemap only (JSON-LD to confirm) |
| Grab | XML job feed | 9 | country filter client-side |
| Zoho | Zoho Recruit embedded data | 2 | very low volume, lowest priority |
| Jane Street | `/jobs/main.json` | **0** | **No India offices.** Adapter is trivial but disabled by default. |

## C. Cannot currently be scraped reliably (27) → bookmarklet / manual

| Reason | Companies |
|---|---|
| **Honest User-Agent blocked** (Cloudflare/Akamai/WAF 403/406). Not circumvented. | SAP, ServiceNow, Cohesity, Wells Fargo, PayPal, Bloomberg, Uber, TCS, Infosys, Cognizant, Blinkit |
| **robots.txt disallows the data path** | Palo Alto Networks¹, Nike, Morgan Stanley, Walmart, Flipkart (TurboHire), Swiggy (SmartRecruiters API host), Slice (Kula) |
| **Token/session-gated or partial only** | Google² (only page 1 of ~272 reachable; robots disallows `?page=`), Apple (CSRF handshake), Meta (per-page GraphQL tokens; robots prohibits collection), American Express (Eightfold session), Goldman Sachs (robots disallows all but `/roles/`; undocumented GraphQL) |
| **No public listing source identified** | Deutsche Bank, Wayfair |
| **Endpoint provenance not confirmed** | IBM, Rippling |

¹ Palo Alto Networks' Workday API answers, but robots.txt disallows the career-site path itself. I treat that as "do not crawl" until you decide otherwise (Decision 3).
² Partial listings would make "closed" detection wrong, so Google stays bookmarklet-only. Revisit if they publish a feed.

## D. Manual entry / bookmarklet only (14 portals, 12 companies)

LinkedIn (listings only on linkedin.com; its ToS forbid automation) · Arista Networks (HTTP 406, ATS unknown) ·
Confluent (now redirects to IBM careers) · Postman (no live board found) · D. E. Shaw (global + India apply portals
are JS-only) · Graviton Research Capital (static page, no listings) · Dream11 (linked Lever board 404) · WinZO (no
working careers page) · Myntra (Flutter SPA) · Tech Mahindra (ASP.NET app) · LTIMindtree / LTM (2 portals,
negligible public data) · Blinkit (Eternal group page has no listings).

## India eligibility findings that shape the design

- **Remote jobs are inconsistent.** Coinbase, Twilio, Zscaler and Atlassian sometimes say "Remote - India" (eligible), while Stripe, Rubrik and Atlassian also use bare "Remote" with no country (**not** eligible under your rule).
- **Location strings vary a lot:**
  - "Bangalore, IND" (Zscaler)
  - "IN-Pune" and "IN-Bangalore-MSO" (Snowflake)
  - "Bangalore " with no country (Stripe)
  - office names only (Barclays)
  - "India" text search matches Indiana (Mastercard, Salesforce, HPE)

  Country isolation must use structured fields first (facets, country codes) and an Indian city/state dictionary second. It must never fall back to a substring match on "India".
- **Volume caps:** Workday reports `total` ≤ 2,000 (Accenture, Target, Airbus). Paginate until an empty page, and split by sub-facet when the India facet itself exceeds 2,000.
