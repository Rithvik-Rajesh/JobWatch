"""PoliteClient: the production `Fetcher`.

Guarantees (see docs/phase3/02-scraping-architecture.md):
  * Always sends the honest `base.USER_AGENT`; a caller-supplied User-Agent is dropped.
  * robots.txt (RFC 9309 semantics) is checked before EVERY request; cached per host for at most 6 h.
      4xx -> allow everything; 5xx / network failure -> disallow (RobotsDisallowed).
  * Per-host rate limit: >= `min_interval` seconds between requests, raised to the robots crawl-delay.
  * Block detection (401/403/406, 429 without a usable Retry-After, HTML challenge pages) raises
    PortalBlocked and is NEVER retried; no header is ever altered to get past a block.
  * Transient failures (timeouts, connection errors, 5xx) are retried (3 attempts, exponential backoff
    with jitter, Retry-After honoured when <= 120 s), then TransientError.
  * `request_count` counts every HTTP request actually sent (including robots.txt fetches and retries).
"""

import asyncio
import contextlib
import random
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from app.core.logging import get_logger
from app.discovery.adapters.base import (
    USER_AGENT,
    ConfigError,
    PortalBlocked,
    RobotsDisallowed,
    SchemaChanged,
    TransientError,
)

log = get_logger(__name__)

ROBOTS_TTL_S = 6 * 3600
ROBOTS_UNAVAILABLE_TTL_S = 300  # re-try a failing robots.txt sooner than a good one
MAX_ATTEMPTS = 3
MAX_RETRY_AFTER_S = 120
MAX_REDIRECTS = 5
CHALLENGE_MARKERS = ("just a moment", "cf-chl", "access denied", "captcha", "attention required")
UA_TOKEN = "jobwatch"  # product token matched against robots.txt `User-agent:` lines


# ───────────────────────────── robots.txt (RFC 9309) ─────────────────────────────


def _pattern_to_regex(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    if anchored:
        pattern = pattern[:-1]
    body = ".*".join(re.escape(part) for part in pattern.split("*"))
    return re.compile("^" + body + ("$" if anchored else ""))


@dataclass
class RobotsRules:
    """Rules of the group that applies to us. Longest matching pattern wins; Allow wins ties."""

    rules: list[tuple[bool, str, re.Pattern[str]]] = field(default_factory=list)  # (allow, pattern, regex)
    crawl_delay: float | None = None

    @classmethod
    def allow_all(cls) -> "RobotsRules":
        return cls()

    @classmethod
    def parse(cls, text: str) -> "RobotsRules":
        groups: list[tuple[list[str], list[tuple[str, str]]]] = []  # (agents, [(directive, value)])
        agents: list[str] = []
        body: list[tuple[str, str]] = []
        in_rules = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (p.strip() for p in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if in_rules:
                    groups.append((agents, body))
                    agents, body, in_rules = [], [], False
                agents.append(value.lower())
            elif key in ("allow", "disallow", "crawl-delay"):
                in_rules = True
                body.append((key, value))
        if agents:
            groups.append((agents, body))

        # Most specific group: our product token; else '*'.
        chosen = [g for g in groups if any(a.split("/")[0] == UA_TOKEN for a in g[0])]
        if not chosen:
            chosen = [g for g in groups if "*" in g[0]]
        parsed = cls()
        for _, directives in chosen:  # multiple groups for the same agent are merged (RFC 9309 2.2.1)
            for key, value in directives:
                if key == "crawl-delay":
                    with contextlib.suppress(ValueError):
                        parsed.crawl_delay = max(parsed.crawl_delay or 0.0, float(value))
                elif value:  # empty Disallow allows everything; empty Allow is a no-op
                    parsed.rules.append((key == "allow", value, _pattern_to_regex(value)))
        return parsed

    def can_fetch(self, path_and_query: str) -> bool:
        best: tuple[int, bool] | None = None
        for allow, pattern, regex in self.rules:
            if regex.match(path_and_query):
                cand = (len(pattern), allow)
                if best is None or cand > best:  # longer wins; on a tie allow (True) wins
                    best = cand
        return True if best is None else best[1]


@dataclass
class _RobotsEntry:
    rules: RobotsRules
    status: str  # allowed | disallowed | unavailable
    checked_mono: float
    checked_at: datetime
    ttl: float
    reason: str = ""
    denied_any: bool = False


def _path_and_query(url: str) -> str:
    parts = urlsplit(url)
    return (parts.path or "/") + (f"?{parts.query}" if parts.query else "")


def _host_key(host_or_url: str) -> str:
    if "//" in host_or_url:
        return urlsplit(host_or_url).netloc.lower()
    return host_or_url.lower()


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


# ───────────────────────────── client ─────────────────────────────


class PoliteClient:
    def __init__(
        self,
        *,
        timeout: float = 20.0,
        min_interval: float = 1.0,
        backoff_base: float = 1.0,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client = httpx.AsyncClient(
            timeout=timeout, transport=transport, follow_redirects=False, headers={"User-Agent": USER_AGENT}
        )
        self.min_interval = min_interval
        self.backoff_base = backoff_base
        self._clock = clock
        self._sleep = sleep
        self.request_count = 0
        self._rate_locks: dict[str, asyncio.Lock] = {}
        self._last_request: dict[str, float] = {}
        self._robots_locks: dict[str, asyncio.Lock] = {}
        self._robots: dict[str, _RobotsEntry] = {}
        self._schemes: dict[str, str] = {}

    # lifecycle
    async def __aenter__(self) -> "PoliteClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ───────── public Fetcher API ─────────

    async def get_json(
        self, url: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None
    ) -> Any:
        resp = await self._request("GET", url, params=params, headers=headers, accept="application/json")
        return self._decode_json(resp, url)

    async def post_json(
        self,
        url: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        resp = await self._request("POST", url, params=params, headers=headers, json=json, accept="application/json")
        return self._decode_json(resp, url)

    async def get_text(
        self, url: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None
    ) -> str:
        resp = await self._request("GET", url, params=params, headers=headers, accept="text/html,*/*;q=0.8")
        return resp.text

    # ───────── robots API ─────────

    def robots_status(self, host: str) -> str | None:
        """'allowed' | 'disallowed' | 'unavailable' for the last check of `host`; None if never checked."""
        entry = self._robots.get(_host_key(host))
        if entry is None:
            return None
        return "disallowed" if entry.denied_any and entry.status == "allowed" else entry.status

    def last_robots_check(self, host: str) -> datetime | None:
        entry = self._robots.get(_host_key(host))
        return entry.checked_at if entry else None

    def crawl_delay(self, host: str) -> float | None:
        entry = self._robots.get(_host_key(host))
        return entry.rules.crawl_delay if entry else None

    async def force_refresh_robots(self, host: str) -> str:
        """Re-fetch robots.txt now (called at the start of every site run). `host` may be a host or URL.
        Returns the resulting status; never raises for robots problems (the next request will)."""
        key = _host_key(host)
        if "//" in host:
            self._schemes[key] = urlsplit(host).scheme
        async with self._robots_lock(key):
            entry = await self._fetch_robots(key)
        return entry.status

    # ───────── internals ─────────

    @staticmethod
    def _decode_json(resp: httpx.Response, url: str) -> Any:
        try:
            return resp.json()
        except ValueError as exc:
            raise SchemaChanged(f"Response from {url} is not valid JSON: {exc}") from exc

    def _robots_lock(self, key: str) -> asyncio.Lock:
        return self._robots_locks.setdefault(key, asyncio.Lock())

    def _rate_lock(self, key: str) -> asyncio.Lock:
        return self._rate_locks.setdefault(key, asyncio.Lock())

    async def _throttle(self, key: str) -> None:
        """One lock per host; spaces request START times by max(min_interval, crawl-delay)."""
        entry = self._robots.get(key)
        delay = max(self.min_interval, (entry.rules.crawl_delay or 0.0) if entry else 0.0)
        async with self._rate_lock(key):
            last = self._last_request.get(key)
            if last is not None:
                wait = last + delay - self._clock()
                if wait > 0:
                    await self._sleep(wait)
            self._last_request[key] = self._clock()

    def _headers(self, extra: dict[str, str] | None, accept: str) -> dict[str, str]:
        out = {"Accept": accept}
        for k, v in (extra or {}).items():
            if k.lower() != "user-agent":  # the UA is not negotiable
                out[k] = v
        out["User-Agent"] = USER_AGENT
        return out

    async def _send_once(self, method: str, url: str, **kw: Any) -> httpx.Response:
        self.request_count += 1
        return await self._client.request(method, url, **kw)

    async def _fetch_robots(self, key: str) -> _RobotsEntry:
        """Caller holds the robots lock for `key`."""
        scheme = self._schemes.get(key, "https")
        url = f"{scheme}://{key}/robots.txt"
        await self._throttle(key)
        now_mono, now_wall = self._clock(), datetime.now(UTC)
        try:
            resp = await self._send_once(
                "GET", url, headers=self._headers(None, "text/plain,*/*;q=0.8"), follow_redirects=True
            )
        except httpx.HTTPError as exc:
            entry = _RobotsEntry(
                RobotsRules(), "unavailable", now_mono, now_wall, ROBOTS_UNAVAILABLE_TTL_S,
                reason=f"robots.txt fetch failed ({type(exc).__name__}: {exc})",
            )  # fmt: skip
        else:
            code = resp.status_code
            if 200 <= code < 300:
                rules = RobotsRules.parse(resp.text)
                status = "allowed" if rules.can_fetch("/") else "disallowed"
                entry = _RobotsEntry(rules, status, now_mono, now_wall, ROBOTS_TTL_S)
            elif 400 <= code < 500:  # RFC 9309 2.3.1.3: unavailable (4xx) -> no restrictions
                entry = _RobotsEntry(RobotsRules.allow_all(), "allowed", now_mono, now_wall, ROBOTS_TTL_S)
            else:  # 5xx / anything else -> assume complete disallow (RFC 9309 2.3.1.4)
                entry = _RobotsEntry(
                    RobotsRules(), "unavailable", now_mono, now_wall, ROBOTS_UNAVAILABLE_TTL_S,
                    reason=f"robots.txt returned HTTP {code}",
                )  # fmt: skip
        self._robots[key] = entry
        log.info("robots_checked", host=key, status=entry.status, crawl_delay=entry.rules.crawl_delay)
        return entry

    async def _check_robots(self, url: str) -> None:
        parts = urlsplit(url)
        key = parts.netloc.lower()
        self._schemes.setdefault(key, parts.scheme)
        entry = self._robots.get(key)
        if entry is None or self._clock() - entry.checked_mono >= entry.ttl:
            async with self._robots_lock(key):
                entry = self._robots.get(key)
                if entry is None or self._clock() - entry.checked_mono >= entry.ttl:
                    entry = await self._fetch_robots(key)
        if entry.status == "unavailable":
            raise RobotsDisallowed(f"{entry.reason} for {key}; treating as disallowed (RFC 9309). Not fetching {url}")
        if not entry.rules.can_fetch(_path_and_query(url)):
            entry.denied_any = True
            raise RobotsDisallowed(f"robots.txt on {key} disallows {url} for our User-Agent")

    @staticmethod
    def _looks_like_challenge(resp: httpx.Response) -> bool:
        head = resp.content[:4096].decode("utf-8", errors="ignore").lower()
        ctype = resp.headers.get("content-type", "").lower()
        if "html" not in ctype and not head.lstrip().startswith("<"):
            return False
        return any(m in head for m in CHALLENGE_MARKERS)

    async def _backoff(self, attempt: int, retry_after: float | None = None) -> None:
        if retry_after is not None:
            await self._sleep(retry_after)
        else:
            await self._sleep(self.backoff_base * (2**attempt) * (0.5 + random.random()))

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json: Any = None,
        accept: str,
    ) -> httpx.Response:
        hdrs = self._headers(headers, accept)
        redirects = 0
        last_error = "unknown error"
        attempt = 0
        while attempt < MAX_ATTEMPTS:
            await self._check_robots(url)  # also re-checked for redirect targets and retries
            await self._throttle(urlsplit(url).netloc.lower())
            kw: dict[str, Any] = {"headers": hdrs}
            if params is not None:
                kw["params"] = params
            if json is not None:
                kw["json"] = json
            try:
                resp = await self._send_once(method, url, **kw)
            except httpx.HTTPError as exc:  # timeouts, connection errors, protocol errors
                last_error = f"{type(exc).__name__}: {exc}"
                attempt += 1
                if attempt < MAX_ATTEMPTS:
                    await self._backoff(attempt - 1)
                continue

            code = resp.status_code
            if code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                redirects += 1
                if redirects > MAX_REDIRECTS:
                    raise ConfigError(f"Too many redirects fetching {url}")
                url = urljoin(str(resp.request.url), resp.headers["location"])
                params = None  # already part of the redirect target
                if code == 303 or (code in (301, 302) and method == "POST"):
                    method, json = "GET", None
                continue
            if code in (401, 403, 406):
                raise PortalBlocked(f"HTTP {code} from {url}; not retrying or working around")
            if code == 429:
                delay = parse_retry_after(resp.headers.get("retry-after"))
                if delay is None or delay > MAX_RETRY_AFTER_S:
                    raise PortalBlocked(f"HTTP 429 from {url} without a usable Retry-After; not retrying")
                last_error = f"HTTP 429 from {url} (Retry-After {delay:.0f}s)"
                attempt += 1
                if attempt < MAX_ATTEMPTS:
                    await self._backoff(attempt - 1, delay)
                continue
            if code in (200, 503) and self._looks_like_challenge(resp):
                raise PortalBlocked(f"Challenge / access-denied page from {url} (HTTP {code}); not retrying")
            if code >= 500:
                last_error = f"HTTP {code} from {url}"
                delay = parse_retry_after(resp.headers.get("retry-after")) if code == 503 else None
                if delay is not None and delay > MAX_RETRY_AFTER_S:
                    raise TransientError(f"{last_error}; Retry-After {delay:.0f}s exceeds the limit")
                attempt += 1
                if attempt < MAX_ATTEMPTS:
                    await self._backoff(attempt - 1, delay)
                continue
            if code in (404, 422):
                raise ConfigError(f"HTTP {code} from {url}")
            if 300 <= code < 500:
                raise ConfigError(f"Unexpected HTTP {code} from {url}")
            return resp
        raise TransientError(f"{last_error} (gave up after {MAX_ATTEMPTS} attempts)")
