"""Test support for discovery (lead-owned): replay recorded fixtures through the `Fetcher` protocol.

FixtureFetcher matches requests by (method, url, params, json) — canonicalised — against fixture files
recorded by scripts/record_fixture.py, or against in-memory responses registered with `add()`. Unmatched
requests raise AssertionError listing what was requested, so adapters can't silently hit the network.
It can also simulate robots/blocks/transient errors via `fail()`.
"""

import json
from pathlib import Path
from typing import Any

from app.discovery.adapters.base import PortalBlocked, RobotsDisallowed, TransientError

FIXTURES = Path(__file__).parent / "fixtures"


def _key(method: str, url: str, params: dict | None, body: Any) -> str:
    return json.dumps(
        [method.upper(), url.rstrip("/"), sorted((params or {}).items()), body], sort_keys=True, default=str
    )


class FixtureFetcher:
    def __init__(self) -> None:
        self._responses: dict[str, Any] = {}
        self._failures: dict[str, Exception] = {}
        self.calls: list[tuple[str, str, dict | None, Any]] = []

    # -- arranging ---------------------------------------------------------------------------------
    def load(self, *relative_paths: str) -> "FixtureFetcher":
        """Register recorded fixture files (paths relative to tests/fixtures)."""
        for rel in relative_paths:
            data = json.loads((FIXTURES / rel).read_text())
            req, resp = data["request"], data["response"]
            value = resp["json"] if resp["json"] is not None else resp["text"]
            if resp["status"] in (401, 403, 406, 429):
                self._failures[_key(req["method"], req["url"], req["params"], req["json"])] = PortalBlocked(
                    f"HTTP {resp['status']}"
                )
            else:
                self._responses[_key(req["method"], req["url"], req["params"], req["json"])] = value
        return self

    def add(self, method: str, url: str, response: Any, *, params: dict | None = None, json_body: Any = None):
        self._responses[_key(method, url, params, json_body)] = response
        return self

    def fail(self, method: str, url: str, exc: Exception, *, params: dict | None = None, json_body: Any = None):
        self._failures[_key(method, url, params, json_body)] = exc
        return self

    # -- Fetcher protocol --------------------------------------------------------------------------
    async def _lookup(self, method: str, url: str, params: dict | None, body: Any) -> Any:
        self.calls.append((method, url, params, body))
        key = _key(method, url, params, body)
        if key in self._failures:
            raise self._failures[key]
        if key not in self._responses:
            raise AssertionError(f"Unexpected request (no fixture): {method} {url} params={params} json={body}")
        return self._responses[key]

    async def get_json(self, url, *, params=None, headers=None):
        return await self._lookup("GET", url, params, None)

    async def post_json(self, url, *, json=None, params=None, headers=None):
        return await self._lookup("POST", url, params, json)

    async def get_text(self, url, *, params=None, headers=None):
        value = await self._lookup("GET", url, params, None)
        return value if isinstance(value, str) else json.dumps(value)


__all__ = ["FIXTURES", "FixtureFetcher", "PortalBlocked", "RobotsDisallowed", "TransientError"]
