# Provenance check: IBM and Rippling (tag=provenance, 8/8 requests used)

All requests went through backend/scripts/record_fixture.py (honest UA, robots-checked, ledgered in data/verification/fixture_requests.jsonl). Fixtures: backend/tests/fixtures/provenance/. The job APIs were NOT called again. Budget is exhausted, so robots.txt for the two API hosts was not re-fetched; prior findings from companies.csv are cited and marked as such.

## IBM  - verdict: NOT FOUND (keep disabled; bookmarklet only)

Requests (ledger, tag=provenance):
1. GET https://careers.ibm.com/robots.txt -> 200
2. GET https://careers.ibm.com/careers/SearchJobs -> 202, AWS WAF bot challenge (JS token page, 1999 bytes). Not worked around; no listing HTML obtained. Fixture: ibm_careers.json
3. GET https://www.ibm.com/robots.txt -> 200
4. GET https://www.ibm.com/careers/search -> 200, 126082 bytes. Fixture: ibm_www_careers_search.json
5. GET https://www.ibm.com/etc.clientlibs/adobe-cms/clientlibs/clientlib-components-main...min.js -> 200, 21188 bytes. Fixture: ibm_clientlib_components_main.json

Evidence:
- Neither `www-api.ibm.com` nor `search/api/v2` appears in ibm_www_careers_search.json or ibm_clientlib_components_main.json.
- Partial, indirect signal only: the page defines the search parameters the API body uses. In ibm_www_careers_search.json: `var careerSearchScope ={scopeid: 'careers2'};` and `var careerSearchAppId ={appid: 'careers'};`. It calls an undefined-in-page `careerSearch(obj)`, whose implementation lives in a script not inspected (the one script fetched does not contain it). This suggests the page talks to a search backend with appId=careers / scope careers2, but the host/path is not shown.
- careers.ibm.com (Avature) is behind an AWS WAF challenge for non-browser clients, so its page could not be inspected.

robots.txt status:
- Page host www.ibm.com: robots.txt fetched (200); the recorder permitted GET /careers/search. 
- Page host careers.ibm.com: robots.txt fetched (200); recorder permitted /careers/SearchJobs. The WAF 202 challenge is a bot-detection response, so treat it as a block signal for automated access to that host.
- API host www-api.ibm.com: not re-fetched (budget). Prior note in companies.csv: robots redirects to an ibm.com page (no rules). Unverified this round.

Verdict: NOT FOUND. The endpoint is not referenced by anything inspected; the actual search script was not located within budget. Not safe to build the adapter. Possible follow-up with a new budget: find the script defining `careerSearch` (other clientlibs, e.g. clientlib-new-design / carbon-dynamic-loader) and fetch www-api.ibm.com/robots.txt.

## Rippling  - verdict: NOT FOUND (keep disabled; bookmarklet only)

Requests (ledger, tag=provenance):
6. GET https://www.rippling.com/robots.txt -> 200
7. GET https://www.rippling.com/careers/open-roles -> 200, 698801 bytes. Fixture: rippling_open_roles.json
8. GET https://www.rippling.com/_next/static/chunks/pages/careers/open-roles-e531054487c6a587.js -> 200, 40088 bytes. Fixture: rippling_open_roles_chunk.json

Evidence:
- `api.rippling.com`, `platform/api/ats`, `board/rippling`, `ats.rippling.com` are absent from both the page HTML and the page-specific chunk.
- The page is Next.js and drives its listing through Algolia, not the ATS board endpoint. In rippling_open_roles.json __NEXT_DATA__: `"algoliaIndexName":"careers_en-US_production"`; the chunk uses InstantSearch (`indexName:b,insights:!0`).
- Shared chunks (_app, framework, numbered chunks) were not inspected (budget), so a reference there cannot be fully excluded, but the page's own listing logic demonstrably uses Algolia.

robots.txt status:
- Page host www.rippling.com: robots.txt fetched (200); page and chunk fetches permitted.
- API host api.rippling.com: not re-fetched (budget). Prior note in companies.csv: robots returns a 404 body (unavailable). Unverified this round.

Verdict: NOT FOUND. Rippling's own page does not reference the ATS endpoint (it uses Algolia). Keep disabled; the Algolia-backed listing is not an approved source here either.
