"""Build data/companies.csv from the career-site verification results.

Input : JSON arrays produced by the verification agents (one object per portal, see PROTOCOL fields).
Output: data/companies.csv (one row per career portal; a company may have several rows).

The `support` column is the LEAD's final classification, applied uniformly from the evidence:

  auto          - shared platform adapter (workday, greenhouse, lever, ashby, oracle_hcm, eightfold);
                  public data, robots not disallowing the data path, India isolable.
  adapter       - feasible, but needs a company-specific adapter (bespoke JSON API / feed / sitemap).
  not_reliable  - blocked by bot protection, robots disallows the data path, needs session/CSRF tokens,
                  or only partial results are politely reachable. -> bookmarklet / manual entry.
  manual        - no machine-readable listings found (LinkedIn-only, dead/unknown portal, JS-only app).
                  -> bookmarklet / manual entry.

Usage: python3 -I data/tools/build_registry.py <results_dir> data/companies.csv
"""

import csv
import glob
import json
import os
import sys

VERIFIED_AT = "2026-11-11"
SHARED_PLATFORMS = {"workday", "greenhouse", "lever", "ashby", "oracle_hcm", "eightfold"}

# Lead overrides: (company prefix, site_label or None) -> dict of field overrides.
# Every override carries a reason that ends up in `support_reason`.
OVERRIDES: list[tuple[str, str | None, dict]] = [
    # --- robots.txt disallows the career-site / data path ---------------------------------------
    ("Palo Alto Networks", None, {"support": "not_reliable", "support_reason":
        "robots.txt disallows /panwexternalcareers/ (the Workday site path); cxs API path not mentioned. "
        "Treated as disallowed until you decide otherwise."}),
    ("Nike", None, {"support": "not_reliable", "support_reason": "robots.txt disallows the Workday site paths."}),
    ("Morgan Stanley", None, {"support": "not_reliable", "support_reason":
        "robots.txt Disallow: / except /careers and /api/apply; the allowed endpoint returns 'Not authorized'."}),
    ("Walmart", None, {"support": "not_reliable", "support_reason": "Data API under robots-disallowed /api."}),
    ("Flipkart", None, {"support": "not_reliable", "support_reason": "TurboHire host robots.txt: Disallow: / for all bots."}),
    ("Swiggy", None, {"support": "not_reliable", "support_reason":
        "SmartRecruiters public API host robots.txt disallows generic bots."}),
    ("Slice", None, {"support": "not_reliable", "support_reason": "Kula ATS robots.txt disallows /api/."}),
    ("WinZO", None, {"support": "manual", "support_reason": "No working careers page (404) and robots disallows most paths."}),
    ("Goldman Sachs", None, {"support": "not_reliable", "support_reason":
        "higher.gs.com robots disallows all but /roles/; GraphQL API undocumented. Fragile."}),
    # --- partial / token-gated -------------------------------------------------------------------
    ("Google", None, {"support": "not_reliable", "support_reason":
        "Only page 1 (20 of ~272 India jobs) is reachable: robots.txt disallows ?page= pagination. "
        "Incomplete listings cannot drive new/closed detection."}),
    ("Apple", None, {"support": "not_reliable", "support_reason":
        "JSON search API requires a CSRF token handshake; HTML gives only page 1."}),
    ("Meta", None, {"support": "not_reliable", "support_reason":
        "GraphQL requires per-page tokens (lsd/fb_dtsg); robots comment prohibits automated collection."}),
    ("American Express", None, {"support": "not_reliable", "support_reason": "Eightfold tenant requires a browser session/CSRF."}),
    # --- shared-platform adapters (agent marked adapter_needed only because the adapter isn't built) -
    ("Microsoft", None, {"support": "auto", "support_reason": "Eightfold PCSX; robots.txt explicitly allows /api/pcsx."}),
    ("Qualcomm", None, {"support": "auto", "support_reason": "Eightfold PCSX (apply/v2 returns 403 for this tenant)."}),
    ("NetApp", None, {"support": "auto", "support_reason": "Eightfold apply/v2 (PCSX disabled for this tenant)."}),
    ("Oracle", None, {"support": "auto", "support_reason": "Oracle HCM Candidate Experience REST, India via locationId."}),
    ("Dell", None, {"support": "auto", "support_reason": "Oracle HCM; use locationId (keyword=India undercounts 11 vs 88)."}),
    ("Texas Instruments", None, {"support": "auto", "support_reason": "Oracle HCM (site CX)."}),
    ("JP Morgan", None, {"support": "auto", "support_reason":
        "Oracle HCM (CX_1001), India via locationId. robots.txt request blocked by WAF (4xx -> treated as "
        "unavailable/allowed per RFC 9309); re-checked at runtime."}),
    # --- bespoke adapters ------------------------------------------------------------------------
    ("Amazon", None, {"support": "adapter", "support_reason":
        "amazon.jobs search.json (normalized_country_code[]=IND); robots allows. ~2100 India jobs."}),
    ("IBM", None, {"support": "adapter", "support_reason":
        "IBM search API (POST); endpoint not visible in page source -> verify provenance before enabling."}),
    ("AMD", None, {"support": "adapter", "support_reason": "iCIMS/Jibe /api/jobs; robots crawl-delay 5 s must be honoured."}),
    ("Cisco", None, {"support": "adapter", "support_reason": "Phenom: job data embedded in search pages; robots allows search."}),
    ("Atlassian", None, {"support": "adapter", "support_reason": "Public listings JSON used by the careers page."}),
    ("Rippling", None, {"support": "adapter", "support_reason":
        "Rippling ATS board JSON; endpoint inferred from platform pattern, robots unavailable -> verify first."}),
    ("Intuit", None, {"support": "adapter", "support_reason":
        "robots disallows /search-jobs/; only sitemap.xml + individual job pages (JSON-LD unverified)."}),
    ("Capgemini", None, {"support": "adapter", "support_reason": "Public job-stream API used by the search page (India filter)."}),
    ("Grab", None, {"support": "adapter", "support_reason": "XML job feed; country filter applied client-side."}),
    ("Wipro", None, {"support": "adapter", "support_reason": "SuccessFactors Career Site Builder: sitemap + job pages; India via '-IND-' slug."}),
    ("HCLTech", None, {"support": "adapter", "support_reason":
        "SuccessFactors; India isolation unresolved (no location in sitemap) -> needs detail-page parsing."}),
    ("Zoho", None, {"support": "adapter", "support_reason": "Zoho Recruit embedded data; very low volume (2 jobs). Low priority."}),
    ("Jane Street", None, {"support": "adapter", "support_reason":
        "Public main.json, but NO India offices (ATX/CHI/HKG/LDN/NYC/SGP). Monitoring yields nothing today."}),
    ("LinkedIn", None, {"support": "manual", "support_reason":
        "Listings live on linkedin.com; LinkedIn's terms forbid automated access (also Cloudflare-challenged)."}),
    ("Dream11", None, {"support": "manual", "support_reason":
        "Careers page links a Lever board that returns 404; no live listings host found."}),
    # --- unidentified sources ------------------------------------------------------------------
    ("Deutsche Bank", None, {"support": "not_reliable", "support_reason": "No public listing API identified yet."}),
    ("Wayfair", None, {"support": "not_reliable", "support_reason": "No public listing API identified yet."}),
    ("Arista", None, {"support": "manual", "support_reason": "Site returns HTTP 406; ATS unknown."}),
    ("Autodesk", None, {"support": "auto", "support_reason":
        "Workday 'Ext' site lists only 4 jobs and autodesk.com blocks the honest UA -> LOW CONFIDENCE, re-verify."}),
    ("Accenture", None, {"support": "auto", "support_reason":
        "Workday; India facet >2000 results -> adapter must split by sub-facet (city/timeType) to page fully."}),
]

AGENT_TO_FINAL = {
    "supported": "auto",
    "adapter_needed": "adapter",
    "fragile": "not_reliable",
    "not_feasible": "not_reliable",
    "manual_only": "manual",
}

COLUMNS = [
    "company", "site_label", "careers_url", "landing_url", "platform", "tenant", "site_id", "api_endpoint",
    "india_filter", "india_filter_detail", "india_jobs_observed", "total_jobs_observed", "robots", "robots_detail",
    "access", "verification_status", "support", "support_reason", "monitor_by_default", "notes", "verified_at",
]

CANONICAL_NAMES = {
    "JP Morgan Chase": "JPMorgan Chase", "D. E. Shaw": "D. E. Shaw", "D. E. Shaw (DE Shaw India)": "D. E. Shaw",
    "Walmart (incl. Walmart Global Tech India)": "Walmart", "Target (incl. Target in India)": "Target",
    "Dream11 (Dream Sports)": "Dream11", "Tata Consultancy Services (TCS)": "Tata Consultancy Services",
    "Dell Technologies": "Dell Technologies", "Arista Networks": "Arista Networks",
}


def classify(row: dict) -> dict:
    out = dict(row)
    out["company"] = CANONICAL_NAMES.get(row["company"], row["company"]).strip()
    final = AGENT_TO_FINAL.get(row.get("support") or "", "manual")
    reason = ""
    if row.get("access") in {"bot_protected", "blocked", "login_required"}:
        final, reason = "not_reliable", f"Honest User-Agent blocked ({row.get('access')}); not circumvented."
    elif row.get("access") == "needs_session":
        final, reason = "not_reliable", "Requires a browser session / token handshake."
    for prefix, label, ov in OVERRIDES:
        if out["company"].startswith(prefix) and (label is None or label == row.get("site_label")):
            final, reason = ov["support"], ov["support_reason"]
            break
    if not reason:
        reason = {
            "auto": f"Shared {row.get('platform')} adapter; public data; India via {row.get('india_filter')}.",
            "adapter": "Feasible with a company-specific adapter.",
            "not_reliable": "Not reliably/politely automatable.",
            "manual": "No machine-readable listings found.",
        }[final]
    if final == "auto" and row.get("platform") not in SHARED_PLATFORMS:
        final = "adapter"
    observed = row.get("india_jobs_observed")
    out["support"] = final
    out["support_reason"] = reason
    # Monitor by default only when automatable AND there is (or may be) India inventory.
    out["monitor_by_default"] = str(final in {"auto", "adapter"} and observed not in (0,)).lower()
    out["verification_status"] = row.get("verification")
    out["verified_at"] = VERIFIED_AT
    return out


def main(results_dir: str, out_path: str) -> None:
    rows = []
    for path in sorted(glob.glob(os.path.join(results_dir, "group_*.json"))):
        with open(path, encoding="utf-8") as fh:
            rows.extend(json.load(fh))
    final = sorted((classify(r) for r in rows), key=lambda r: (r["company"].lower(), r.get("site_label") or ""))
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for r in final:
            writer.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in COLUMNS})
    counts: dict[str, int] = {}
    for r in final:
        counts[r["support"]] = counts.get(r["support"], 0) + 1
    print(f"{len(final)} portals, {len({r['company'] for r in final})} companies -> {out_path}")
    print(counts)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
