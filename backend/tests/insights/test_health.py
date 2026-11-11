"""Scraper-health aggregates and endpoints."""

from datetime import UTC, datetime, timedelta

import pytest

from app.health import service
from app.models import NotificationOutbox, ScrapeRun
from app.models.enums import HealthStatus, OutboxState, RunStatus, RunTrigger
from tests.factories import make_company

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
D = timedelta
API = "/api/v1/health"


def _run(company, status, queued_at, duration=None, **kw):
    finished = queued_at + D(seconds=5) if duration is not None else None
    return ScrapeRun(
        company_id=company.id if company else None,
        trigger=RunTrigger.schedule,
        status=status,
        queued_at=queued_at,
        started_at=queued_at,
        finished_at=finished,
        duration_ms=duration,
        **kw,
    )


async def _seed(session, *, at=NOW):
    a = await make_company(session, "A", monitoring_enabled=True, health=HealthStatus.healthy)
    b = await make_company(session, "B", monitoring_enabled=True, health=HealthStatus.degraded)
    c = await make_company(session, "C", monitoring_enabled=True, health=HealthStatus.failing)
    d = await make_company(session, "D", health=HealthStatus.failing)  # not monitored
    e = await make_company(session, "E", monitoring_enabled=True, health=HealthStatus.failing)
    e.archived_at = at  # excluded everywhere
    runs = [
        _run(a, RunStatus.succeeded, at - D(hours=1), 1000),
        _run(a, RunStatus.succeeded, at - D(hours=2), 3000),
        _run(b, RunStatus.failed, at - D(hours=3), 2000),
        _run(c, RunStatus.quarantined, at - D(hours=4), 4000),
        _run(c, RunStatus.partial, at - D(hours=5), 5000),
        _run(d, RunStatus.skipped, at - D(hours=6)),
        _run(a, RunStatus.queued, at - D(minutes=5)),
        _run(b, RunStatus.running, at - D(minutes=1)),
        # older than 24h but within 7d
        _run(a, RunStatus.succeeded, at - D(days=3), 800),
        _run(b, RunStatus.failed, at - D(days=3), 900),
        # older than 7d: ignored by both rates
        _run(a, RunStatus.failed, at - D(days=9), 900),
    ]
    session.add_all(runs)
    session.add_all(
        [
            NotificationOutbox(dedupe_key="n1", event_type="new_jobs", payload={}, state=OutboxState.pending),
            NotificationOutbox(dedupe_key="n2", event_type="new_jobs", payload={}, state=OutboxState.pending),
            NotificationOutbox(
                dedupe_key="n3",
                event_type="scraper_failure",
                payload={},
                state=OutboxState.failed,
                attempts=3,
                last_error="boom",
            ),
            NotificationOutbox(dedupe_key="n4", event_type="new_jobs", payload={}, state=OutboxState.sent),
        ]
    )
    await session.flush()
    return runs


async def test_overview_exact_numbers(session):
    await _seed(session)
    o = await service.overview(session, now=NOW)
    assert (o.companies_total, o.companies_active) == (4, 3)
    assert (o.companies_healthy, o.companies_degraded, o.companies_failing) == (1, 1, 2)
    assert o.runs_24h == 8  # succeeded 2, failed 1, quarantined 1, partial 1, skipped 1, queued 1, running 1
    assert o.runs_failed_24h == 1 and o.runs_quarantined_24h == 1
    assert o.success_rate_24h == 0.4  # 2 / (2 + 1 + 1 + 1)
    assert o.success_rate_7d == pytest.approx(3 / 7, abs=1e-4)  # 3 / (5 + 2)
    assert o.avg_duration_ms_24h == 3000  # (1000 + 3000 + 2000 + 4000 + 5000) / 5, finished runs only
    assert o.last_successful_run_at == NOW - D(hours=1) + D(seconds=5)
    assert o.pending_tasks == 2
    assert (o.notifications_pending, o.notifications_failed) == (2, 1)


async def test_overview_empty_has_null_rates(session):
    o = await service.overview(session, now=NOW)
    assert o.success_rate_24h is None and o.success_rate_7d is None
    assert o.avg_duration_ms_24h is None and o.last_successful_run_at is None
    assert o.runs_24h == 0 and o.pending_tasks == 0


async def test_overview_rate_null_when_only_skipped(session):
    co = await make_company(session)
    session.add(_run(co, RunStatus.skipped, NOW - D(hours=1)))
    await session.flush()
    o = await service.overview(session, now=NOW)
    assert o.runs_24h == 1 and o.success_rate_24h is None


async def test_http_overview(client, session):
    co = await make_company(session)
    now = datetime.now(UTC)
    session.add_all(
        [_run(co, RunStatus.succeeded, now - D(minutes=10), 1500), _run(co, RunStatus.failed, now - D(minutes=5), 500)]
    )
    await session.flush()
    r = await client.get(f"{API}/overview")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["runs_24h"] == 2 and body["success_rate_24h"] == 0.5 and body["avg_duration_ms_24h"] == 1000


async def test_http_runs_paginated_newest_first_with_filters(client, session):
    real_now = datetime.now(UTC)
    runs = await _seed(session, at=real_now)
    other = await make_company(session, "Other")
    session.add(_run(other, RunStatus.failed, real_now - D(hours=7), 100))
    await session.flush()

    r = await client.get(f"{API}/runs", params={"page_size": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 12 and body["page"] == 1 and body["page_size"] == 5
    queued = [i["queued_at"] for i in body["items"]]
    assert queued == sorted(queued, reverse=True)
    assert body["items"][0]["status"] == "running"

    page3 = (await client.get(f"{API}/runs", params={"page_size": 5, "page": 3})).json()
    assert len(page3["items"]) == 2

    r = await client.get(f"{API}/runs", params=[("status", "failed"), ("status", "quarantined")])
    got = r.json()
    assert got["total"] == 5 and {i["status"] for i in got["items"]} == {"failed", "quarantined"}

    r = await client.get(f"{API}/runs", params={"company_id": other.id})
    assert r.json()["total"] == 1

    r = await client.get(f"{API}/runs", params={"company_id": runs[2].company_id, "status": "failed"})
    assert r.json()["total"] == 2  # company B: one in 24h, one 3 days ago
    assert (await client.get(f"{API}/runs", params={"status": "bogus"})).status_code == 422


async def test_http_notifications_newest_first_and_state_filter(client, session):
    base = datetime.now(UTC)
    for i, state in enumerate([OutboxState.pending, OutboxState.failed, OutboxState.sent, OutboxState.failed]):
        session.add(
            NotificationOutbox(
                dedupe_key=f"k{i}",
                event_type="new_jobs",
                payload={},
                state=state,
                created_at=base - D(minutes=10 - i),
                last_error="x" if state == OutboxState.failed else None,
            )
        )
    await session.flush()
    body = (await client.get(f"{API}/notifications")).json()
    assert [i["dedupe_key"] for i in body["items"]] == ["k3", "k2", "k1", "k0"]
    assert body["total"] == 4
    failed = (await client.get(f"{API}/notifications", params={"state": "failed"})).json()
    assert [i["dedupe_key"] for i in failed["items"]] == ["k3", "k1"]
    assert failed["items"][0]["last_error"] == "x" and failed["total"] == 2
    paged = (await client.get(f"{API}/notifications", params={"page_size": 1, "page": 2})).json()
    assert [i["dedupe_key"] for i in paged["items"]] == ["k2"] and paged["total"] == 4
    assert (await client.get(f"{API}/notifications", params={"state": "nonsense"})).status_code == 422


async def test_http_salary_providers(client):
    r = await client.get(f"{API}/salary-providers")
    assert r.status_code == 200, r.text
    items = r.json()
    assert isinstance(items, list) and items
    assert {"key", "name", "provenance", "enabled", "authorized", "description"} <= set(items[0])


@pytest.mark.parametrize("path", ["/overview", "/runs", "/notifications", "/salary-providers"])
async def test_health_requires_auth(anon_client, path):
    assert (await anon_client.get(API + path)).status_code == 401


async def test_probes_remain_public(anon_client):
    assert (await anon_client.get("/healthz")).status_code == 200
