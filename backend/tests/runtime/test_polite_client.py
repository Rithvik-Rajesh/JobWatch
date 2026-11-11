"""PoliteClient against httpx.MockTransport (no network). Time is faked: `sleep` advances a fake clock."""

import httpx
import pytest

from app.discovery.adapters.base import (
    USER_AGENT,
    ConfigError,
    PortalBlocked,
    RobotsDisallowed,
    SchemaChanged,
    TransientError,
)
from app.discovery.http import PoliteClient, RobotsRules, parse_retry_after

HOST = "jobs.example.com"
BASE = f"https://{HOST}"


class FakeTime:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class Site:
    """Scriptable server: robots response + per-path handlers; records every request."""

    def __init__(self, robots: tuple[int, str] | Exception = (404, ""), pages=None) -> None:
        self.robots = robots
        self.pages = pages if pages is not None else {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/robots.txt":
            if isinstance(self.robots, Exception):
                raise self.robots
            return httpx.Response(self.robots[0], text=self.robots[1])
        handler = self.pages.get(request.url.path, lambda r: httpx.Response(200, json={"ok": True}))
        if isinstance(handler, list):  # sequence of responses, last one repeats
            item = handler.pop(0) if len(handler) > 1 else handler[0]
            return item(request) if callable(item) else item
        return handler(request) if callable(handler) else handler

    def hits(self, path: str) -> int:
        return sum(1 for r in self.requests if r.url.path == path)


def make(site: Site, ft: FakeTime | None = None, **kw) -> tuple[PoliteClient, FakeTime]:
    ft = ft or FakeTime()
    client = PoliteClient(transport=httpx.MockTransport(site), clock=ft.clock, sleep=ft.sleep, **kw)
    return client, ft


# ───────────────────────────── user agent ─────────────────────────────


async def test_user_agent_is_always_ours_even_if_caller_overrides():
    site = Site()
    client, _ = make(site)
    async with client:
        await client.get_json(f"{BASE}/api", headers={"user-agent": "Evil/1.0", "X-Custom": "1"})
        await client.post_json(f"{BASE}/api", json={}, headers={"User-Agent": "Evil/2.0"})
        await client.get_text(f"{BASE}/page", headers={"USER-AGENT": "Evil/3.0"})
    assert site.requests
    for req in site.requests:  # includes robots.txt
        assert req.headers.get_list("user-agent") == [USER_AGENT]
    assert site.requests[1].headers["x-custom"] == "1"


# ───────────────────────────── robots.txt ─────────────────────────────


async def test_robots_404_means_allowed():
    site = Site(robots=(404, "Disallow: /"))
    client, _ = make(site)
    async with client:
        assert await client.get_json(f"{BASE}/api") == {"ok": True}
        assert client.robots_status(HOST) == "allowed"
        assert client.last_robots_check(HOST) is not None


async def test_robots_503_means_disallowed():
    site = Site(robots=(503, ""))
    client, _ = make(site)
    async with client:
        with pytest.raises(RobotsDisallowed, match="503"):
            await client.get_json(f"{BASE}/api")
        assert client.robots_status(HOST) == "unavailable"
    assert site.hits("/api") == 0


async def test_robots_network_failure_means_disallowed():
    site = Site(robots=httpx.ConnectError("boom"))
    client, _ = make(site)
    async with client:
        with pytest.raises(RobotsDisallowed, match=r"robots\.txt"):
            await client.get_text(f"{BASE}/page")
    assert site.hits("/page") == 0


async def test_robots_disallow_path_raises_with_url_and_allows_others():
    site = Site(robots=(200, "User-agent: *\nDisallow: /private\n"))
    client, _ = make(site)
    async with client:
        with pytest.raises(RobotsDisallowed, match="/private/x"):
            await client.get_json(f"{BASE}/private/x")
        assert await client.get_json(f"{BASE}/public") == {"ok": True}
        assert client.robots_status(HOST) == "disallowed"  # a URL was denied since the last refresh
    assert site.hits("/private/x") == 0


async def test_robots_cached_then_changed_after_force_refresh():
    site = Site(robots=(200, "User-agent: *\nAllow: /\n"))
    client, _ = make(site)
    async with client:
        await client.get_json(f"{BASE}/api")
        await client.get_json(f"{BASE}/api")
        assert site.hits("/robots.txt") == 1  # cached
        site.robots = (200, "User-agent: *\nDisallow: /api\n")
        await client.get_json(f"{BASE}/api")  # still cached -> still allowed
        assert await client.force_refresh_robots(HOST) == "allowed"
        with pytest.raises(RobotsDisallowed):
            await client.get_json(f"{BASE}/api")
        assert site.hits("/robots.txt") == 2


async def test_robots_cache_expires_after_six_hours():
    site = Site(robots=(200, "User-agent: *\nAllow: /\n"))
    client, ft = make(site)
    async with client:
        await client.get_json(f"{BASE}/api")
        ft.now += 6 * 3600 + 1
        await client.get_json(f"{BASE}/api")
    assert site.hits("/robots.txt") == 2


def test_robots_rules_rfc9309_semantics():
    rules = RobotsRules.parse(
        """
        User-agent: *
        Disallow: /
        Crawl-delay: 1

        User-agent: JobWatch
        Disallow: /admin
        Allow: /admin/public
        Disallow: /*.pdf$
        Crawl-delay: 7
        """
    )
    assert rules.crawl_delay == 7
    assert rules.can_fetch("/jobs")
    assert not rules.can_fetch("/admin/x")
    assert rules.can_fetch("/admin/public/x")  # longest match wins
    assert not rules.can_fetch("/files/a.pdf")
    assert rules.can_fetch("/files/a.pdf?x=1")  # '$' anchors the end
    assert not RobotsRules.parse("User-agent: *\nDisallow: /").can_fetch("/anything")
    assert RobotsRules.parse("User-agent: *\nDisallow:").can_fetch("/anything")  # empty Disallow = allow all


# ───────────────────────────── rate limiting ─────────────────────────────


async def test_rate_limit_spaces_requests_to_same_host_but_not_across_hosts():
    site = Site()
    client, ft = make(site, min_interval=1.0)
    async with client:
        await client.get_json(f"{BASE}/a")  # robots + /a: robots 1st, /a waits 1s
        before = ft.now
        await client.get_json(f"{BASE}/b")
        assert ft.now - before >= 1.0 - 1e-9
        t = ft.now
        await client.get_json("https://other.example.org/a")  # other host: its robots fetch + request
        assert ft.sleeps[-1] == pytest.approx(1.0)  # only the same-host waits slept (other host's 2nd call)
    assert t >= before


async def test_crawl_delay_raises_spacing():
    site = Site(robots=(200, "User-agent: *\nCrawl-delay: 5\n"))
    client, ft = make(site, min_interval=1.0)
    async with client:
        await client.get_json(f"{BASE}/a")
        t0 = ft.now
        await client.get_json(f"{BASE}/b")
        assert ft.now - t0 == pytest.approx(5.0)
        assert client.crawl_delay(HOST) == 5.0


# ───────────────────────────── block detection ─────────────────────────────


@pytest.mark.parametrize("status", [401, 403, 406])
async def test_blocking_statuses_raise_portal_blocked_without_retry(status):
    site = Site(pages={"/api": httpx.Response(status, text="no")})
    client, _ = make(site)
    async with client:
        with pytest.raises(PortalBlocked):
            await client.get_json(f"{BASE}/api")
    assert site.hits("/api") == 1


@pytest.mark.parametrize(
    "marker",
    [
        "Just a moment...",
        "cf-chl-bypass",
        "Access Denied",
        "Please solve the CAPTCHA",
        "Attention Required! | Cloudflare",
    ],
)
async def test_challenge_html_raises_portal_blocked_without_retry(marker):
    html = f"<html><head><title>{marker}</title></head><body></body></html>"
    site = Site(pages={"/page": httpx.Response(200, text=html, headers={"content-type": "text/html"})})
    client, _ = make(site)
    async with client:
        with pytest.raises(PortalBlocked):
            await client.get_text(f"{BASE}/page")
    assert site.hits("/page") == 1


async def test_429_without_retry_after_is_blocked_no_retry():
    site = Site(pages={"/api": httpx.Response(429)})
    client, _ = make(site)
    async with client:
        with pytest.raises(PortalBlocked):
            await client.get_json(f"{BASE}/api")
    assert site.hits("/api") == 1


async def test_429_with_huge_retry_after_is_blocked():
    site = Site(pages={"/api": httpx.Response(429, headers={"Retry-After": "3600"})})
    client, _ = make(site)
    async with client:
        with pytest.raises(PortalBlocked):
            await client.get_json(f"{BASE}/api")
    assert site.hits("/api") == 1


async def test_429_with_retry_after_is_retried_and_waits():
    site = Site(pages={"/api": [httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json={"n": 1})]})
    client, ft = make(site)
    async with client:
        assert await client.get_json(f"{BASE}/api") == {"n": 1}
    assert site.hits("/api") == 2
    assert 7.0 in ft.sleeps


# ───────────────────────────── retries & errors ─────────────────────────────


async def test_5xx_retried_three_times_then_transient_error():
    site = Site(pages={"/api": httpx.Response(502)})
    client, _ = make(site)
    async with client:
        with pytest.raises(TransientError):
            await client.get_json(f"{BASE}/api")
        assert site.hits("/api") == 3
        assert client.request_count == 4  # robots + 3 attempts


async def test_5xx_then_success():
    site = Site(pages={"/api": [httpx.Response(500), httpx.Response(200, json=[1])]})
    client, _ = make(site)
    async with client:
        assert await client.get_json(f"{BASE}/api") == [1]
    assert site.hits("/api") == 2


async def test_503_retry_after_over_limit_is_not_retried():
    site = Site(pages={"/api": httpx.Response(503, headers={"Retry-After": "900"})})
    client, _ = make(site)
    async with client:
        with pytest.raises(TransientError):
            await client.get_json(f"{BASE}/api")
    assert site.hits("/api") == 1


async def test_timeouts_and_connection_errors_are_retried():
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("slow") if calls["n"] == 1 else httpx.ConnectError("down")

    site = Site(pages={"/api": flaky})
    client, _ = make(site)
    async with client:
        with pytest.raises(TransientError, match="3 attempts"):
            await client.get_json(f"{BASE}/api")
    assert calls["n"] == 3


@pytest.mark.parametrize("status", [404, 422, 400, 410])
async def test_client_errors_raise_config_error(status):
    site = Site(pages={"/api": httpx.Response(status)})
    client, _ = make(site)
    async with client:
        with pytest.raises(ConfigError):
            await client.get_json(f"{BASE}/api")
    assert site.hits("/api") == 1


async def test_bad_json_raises_schema_changed():
    site = Site(pages={"/api": httpx.Response(200, text="not json", headers={"content-type": "application/json"})})
    client, _ = make(site)
    async with client:
        with pytest.raises(SchemaChanged):
            await client.get_json(f"{BASE}/api")


async def test_post_json_sends_body_and_params():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        seen["query"] = dict(request.url.params)
        seen["method"] = request.method
        return httpx.Response(200, json={"r": 1})

    site = Site(pages={"/search": handler})
    client, _ = make(site)
    async with client:
        assert await client.post_json(f"{BASE}/search", json={"q": "x"}, params={"p": "1"}) == {"r": 1}
    assert seen["method"] == "POST" and seen["query"] == {"p": "1"} and b'"q"' in seen["body"]


async def test_redirect_target_is_robots_checked():
    def redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://other.example.org/private/x"})

    other_robots = {"https://other.example.org": "User-agent: *\nDisallow: /private\n"}

    def server(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=other_robots.get(f"https://{request.url.host}", ""))
        return redirect(request)

    ft = FakeTime()
    client = PoliteClient(transport=httpx.MockTransport(server), clock=ft.clock, sleep=ft.sleep)
    async with client:
        with pytest.raises(RobotsDisallowed, match=r"other\.example\.org"):
            await client.get_json(f"{BASE}/go")


def test_parse_retry_after():
    assert parse_retry_after("5") == 5
    assert parse_retry_after(None) is None
    assert parse_retry_after("garbage") is None
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0
