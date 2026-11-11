"""Record ONE real HTTP response as a test fixture — the only sanctioned way to make live requests while
building adapters (lead-owned).

Guarantees (enforced here, not by convention):
  * Honest User-Agent only (app.discovery.adapters.base.USER_AGENT); it cannot be overridden.
  * robots.txt of the host is fetched (once, cached) and obeyed; disallowed URLs are refused.
    robots crawl-delay is honoured (minimum 2 s between requests to the same host regardless).
  * Every request — including robots.txt fetches — is appended to the committed ledger
    data/verification/fixture_requests.jsonl, and budgets are enforced from that ledger:
      per-tag budget (--budget) and a hard global cap (GLOBAL_CAP) approved by the user.
  * 401/403/406/429 or challenge pages are recorded as BLOCKED and never retried.

Usage (from backend/):
  uv run python scripts/record_fixture.py --tag workday --budget 14 \
      --out tests/fixtures/workday/nvidia_list.json --method POST \
      --url https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs \
      --json '{"appliedFacets":{},"limit":20,"offset":0,"searchText":""}'
"""

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.discovery.adapters.base import USER_AGENT
from app.discovery.http import RobotsRules

REPO = Path(__file__).resolve().parents[2]
LEDGER = REPO / "data" / "verification" / "fixture_requests.jsonl"
ROBOTS_CACHE = REPO / "data" / "verification" / "robots_cache"
GLOBAL_CAP = 66  # user approved "approximately 60" fixture requests (+10% tolerance), plus the separately
# approved IBM/Rippling provenance probes are tracked under their own tag within this cap.
MIN_HOST_INTERVAL_S = 2.0
CHALLENGE_MARKERS = ("just a moment", "cf-chl", "access denied", "captcha", "attention required")


def _ledger() -> list[dict]:
    if not LEDGER.exists():
        return []
    return [json.loads(line) for line in LEDGER.read_text().splitlines() if line.strip()]


def _append(entry: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")


def _wait_for_host(host: str, delay: float) -> None:
    last = [e for e in _ledger() if e["host"] == host]
    if last:
        elapsed = time.time() - last[-1]["ts"]
        if elapsed < delay:
            time.sleep(delay - elapsed)


def _check_budget(tag: str, budget: int) -> None:
    entries = _ledger()
    used_tag = sum(1 for e in entries if e["tag"] == tag)
    if used_tag >= budget:
        sys.exit(f"REFUSED: budget for tag '{tag}' exhausted ({used_tag}/{budget})")
    if len(entries) >= GLOBAL_CAP:
        sys.exit(f"REFUSED: global fixture request cap reached ({len(entries)}/{GLOBAL_CAP})")


def _robots(client: httpx.Client, scheme: str, host: str, tag: str, budget: int) -> tuple[RobotsRules, float]:
    ROBOTS_CACHE.mkdir(parents=True, exist_ok=True)
    cache = ROBOTS_CACHE / f"{host}.txt"
    status_file = ROBOTS_CACHE / f"{host}.status"
    if not cache.exists():
        _check_budget(tag, budget)
        _wait_for_host(host, MIN_HOST_INTERVAL_S)
        url = f"{scheme}://{host}/robots.txt"
        try:
            r = client.get(url, headers={"Accept": "text/plain,*/*;q=0.8"})
            status, body = r.status_code, r.text if r.status_code == 200 else ""
        except httpx.HTTPError as exc:
            status, body = 0, ""
            print(f"robots fetch failed: {exc}", file=sys.stderr)
        _append(
            {
                "ts": time.time(),
                "at": datetime.now(UTC).isoformat(),
                "tag": tag,
                "host": host,
                "method": "GET",
                "url": url,
                "status": status,
                "kind": "robots",
            }
        )
        cache.write_text(body)
        status_file.write_text(str(status))
    status = int(status_file.read_text() or 0)
    if status >= 500 or status == 0:
        sys.exit(f"REFUSED: robots.txt for {host} unreachable (HTTP {status}); RFC 9309 -> assume disallowed")
    rules = RobotsRules.allow_all() if status >= 400 else RobotsRules.parse(cache.read_text())
    delay = max(MIN_HOST_INTERVAL_S, float(rules.crawl_delay or 0))
    return rules, delay


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--budget", type=int, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--method", default="GET", choices=["GET", "POST"])
    ap.add_argument("--url", required=True)
    ap.add_argument("--params", default=None, help="JSON object of query params")
    ap.add_argument("--json", default=None, help="JSON request body (POST)")
    ap.add_argument("--accept", default="application/json")
    ap.add_argument("--robots-only", action="store_true", help="(re)fetch and report robots.txt for --url's host")
    args = ap.parse_args()

    parsed = urlparse(args.url)
    host = parsed.hostname or ""
    params = json.loads(args.params) if args.params else None
    body = json.loads(args.json) if args.json else None
    headers = {"User-Agent": USER_AGENT, "Accept": args.accept}

    with httpx.Client(headers=headers, timeout=25, follow_redirects=True) as client:
        rp, delay = _robots(client, parsed.scheme, host, args.tag, args.budget)
        if args.robots_only:
            path = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
            status = (ROBOTS_CACHE / f"{host}.status").read_text()
            print(f"robots {host}: HTTP {status}; {path} allowed={rp.can_fetch(path)}; crawl-delay={rp.crawl_delay}")
            return
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        if not rp.can_fetch(path):
            sys.exit(f"REFUSED: robots.txt disallows {args.url} for our User-Agent")
        _check_budget(args.tag, args.budget)
        _wait_for_host(host, delay)
        try:
            r = client.request(args.method, args.url, params=params, json=body)
            status, text, ctype = r.status_code, r.text, r.headers.get("content-type", "")
        except httpx.HTTPError as exc:
            status, text, ctype = 0, f"{type(exc).__name__}: {exc}", ""
        blocked = status in (401, 403, 406, 429) or (
            "html" in ctype and any(m in text[:4000].lower() for m in CHALLENGE_MARKERS)
        )
        _append(
            {
                "ts": time.time(),
                "at": datetime.now(UTC).isoformat(),
                "tag": args.tag,
                "host": host,
                "method": args.method,
                "url": args.url,
                "status": status,
                "kind": "fixture",
                "blocked": blocked,
                "bytes": len(text),
            }
        )

    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    fixture = {
        "request": {"method": args.method, "url": args.url, "params": params, "json": body},
        "response": {
            "status": status,
            "content_type": ctype,
            "json": payload,
            "text": None if payload is not None else text,
        },
        "recorded_at": datetime.now(UTC).isoformat(),
        "user_agent": USER_AGENT,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(fixture, indent=1, ensure_ascii=False))
    used = sum(1 for e in _ledger() if e["tag"] == args.tag)
    print(
        f"{'BLOCKED' if blocked else 'OK'} {status} -> {out}  (tag {args.tag}: {used}/{args.budget}, "
        f"global: {len(_ledger())}/{GLOBAL_CAP})"
    )
    if blocked:
        sys.exit(2)


if __name__ == "__main__":
    main()
