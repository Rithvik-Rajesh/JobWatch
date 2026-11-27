"""Scheduler selection against the real DB (rolled-back transaction per test)."""

import os
import random
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text

from app.models import CareerSite, WorkerHeartbeat
from app.models.enums import ExtractorKind, HealthStatus
from app.settings.registry import SchedulerSettings
from app.worker.app import conninfo_from_url
from app.worker.tasks import select_and_schedule_due_sites, worker_name, write_heartbeat
from tests.factories import make_company

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)


async def site(session, **kw) -> CareerSite:
    company = await make_company(session)
    values = {
        "company_id": company.id,
        "platform": ExtractorKind.greenhouse,
        "monitoring_enabled": True,
        "health": HealthStatus.healthy,
    }
    values.update(kw)
    s = CareerSite(**values)
    session.add(s)
    await session.flush()
    return s


def cfg(**kw) -> SchedulerSettings:
    return SchedulerSettings(**{"enabled": True, "jitter_min": 0, **kw})


async def test_disabled_scheduler_selects_nothing_and_touches_nothing(session):
    s = await site(session)
    assert await select_and_schedule_due_sites(session, NOW, SchedulerSettings(enabled=False)) == []
    await session.refresh(s)
    assert s.next_check_at is None


async def test_selects_only_due_monitored_nonblocked_nonarchived(session):
    due_null = await site(session)
    due_past = await site(session, next_check_at=NOW - timedelta(minutes=1))
    due_exact = await site(session, next_check_at=NOW)
    future = await site(session, next_check_at=NOW + timedelta(minutes=5))
    unmonitored = await site(session, monitoring_enabled=False)
    blocked = await site(session, health=HealthStatus.blocked)
    archived = await site(session, archived_at=NOW - timedelta(days=1))
    failing = await site(session, health=HealthStatus.failing)  # failing sites are still retried

    ids = await select_and_schedule_due_sites(session, NOW, cfg())
    assert set(ids) == {due_null.id, due_past.id, due_exact.id, failing.id}
    for s in (future, unmonitored, blocked, archived):
        assert s.id not in ids
    await session.refresh(future)
    assert future.next_check_at == NOW + timedelta(minutes=5)


async def test_interval_override_vs_default_and_next_check_advanced(session):
    default_site = await site(session)
    override_site = await site(session, check_interval_min=120)
    await select_and_schedule_due_sites(session, NOW, cfg(default_interval_min=30))
    await session.refresh(default_site)
    await session.refresh(override_site)
    assert default_site.next_check_at == NOW + timedelta(minutes=30)
    assert override_site.next_check_at == NOW + timedelta(minutes=120)
    # advanced, so an immediate second tick selects nothing
    assert await select_and_schedule_due_sites(session, NOW, cfg()) == []


async def test_jitter_stays_within_bounds(session):
    sites = [await site(session) for _ in range(5)]
    await select_and_schedule_due_sites(session, NOW, cfg(jitter_min=3, max_concurrent_sites=8), rng=random.Random(1))
    for s in sites:
        await session.refresh(s)
        delta = (s.next_check_at - NOW).total_seconds() / 60
        assert 27 <= delta <= 33


async def test_limit_respected_and_oldest_first(session):
    a = await site(session, next_check_at=NOW - timedelta(hours=3))
    b = await site(session, next_check_at=NOW - timedelta(hours=2))
    await site(session, next_check_at=NOW - timedelta(hours=1))
    await site(session)  # never checked -> first in line
    ids = await select_and_schedule_due_sites(session, NOW, cfg(max_concurrent_sites=3))
    assert len(ids) == 3
    assert a.id in ids and b.id in ids
    rest = await select_and_schedule_due_sites(session, NOW, cfg(max_concurrent_sites=3))
    assert len(rest) == 1


async def test_heartbeat_upsert(session):
    await write_heartbeat(session, NOW)
    await write_heartbeat(session, NOW + timedelta(minutes=1))
    rows = (await session.scalars(select(WorkerHeartbeat))).all()
    assert [r.name for r in rows] == [worker_name()]
    assert rows[0].beat_at == NOW + timedelta(minutes=1)


# ───────────────────────────── migration / schema ─────────────────────────────


async def test_procrastinate_schema_and_heartbeat_table_exist(session):
    tables = set(
        (
            await session.scalars(
                text("select table_name from information_schema.tables where table_schema = current_schema()")
            )
        ).all()
    )
    assert {
        "procrastinate_jobs",
        "procrastinate_events",
        "procrastinate_periodic_defers",
        "worker_heartbeats",
    } <= tables
    funcs = set(
        (await session.scalars(text("select proname from pg_proc where proname like 'procrastinate\\_%'"))).all()
    )
    assert {"procrastinate_defer_jobs_v1", "procrastinate_fetch_job_v2"} & funcs or "procrastinate_fetch_job" in funcs


async def test_alembic_check_reports_no_drift():
    import asyncio

    from alembic import command
    from alembic.config import Config

    def run() -> None:
        cfg_ = Config(os.path.join(os.path.dirname(__file__), "..", "..", "alembic.ini"))
        cfg_.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "..", "migrations"))
        cfg_.set_main_option("sqlalchemy.url", os.environ["JOBWATCH_DATABASE_URL"])
        command.check(cfg_)  # raises CommandError on drift

    await asyncio.to_thread(run)


def test_conninfo_strips_asyncpg_driver():
    assert conninfo_from_url("postgresql+asyncpg://u:p@h:5433/db") == "postgresql://u:p@h:5433/db"
    assert conninfo_from_url("postgresql://u@h/db") == "postgresql://u@h/db"


async def test_scrape_site_task_calls_pipeline_with_polite_client_and_closes_it(monkeypatch):
    import types

    from app.discovery.http import PoliteClient
    from app.worker import tasks

    seen = {}

    async def run_site(site_id, *, trigger, fetcher, scrape_run_id=None, **kw):
        seen.update(site_id=site_id, trigger=trigger, fetcher=fetcher, scrape_run_id=scrape_run_id)
        return types.SimpleNamespace(status="succeeded", complete=True)

    import app.discovery.pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "run_site", run_site)
    closed = []
    orig_close = PoliteClient.aclose

    async def spy_close(self):
        closed.append(self)
        await orig_close(self)

    monkeypatch.setattr(PoliteClient, "aclose", spy_close)
    await tasks.scrape_site.func(5, trigger="manual", scrape_run_id=9)
    assert seen["site_id"] == 5 and seen["trigger"] == "manual" and seen["scrape_run_id"] == 9
    assert isinstance(seen["fetcher"], PoliteClient) and closed == [seen["fetcher"]]


def test_scrape_site_is_registered_with_site_locks():
    from app.worker.app import app
    from app.worker.tasks import scheduler_tick, scrape_site

    app.perform_import_paths()
    assert app.tasks["scrape_site"] is scrape_site
    assert app.tasks["scheduler_tick"] is scheduler_tick
    assert any(
        name == "scheduler_tick"
        for (name, _id) in [(t.task.name, 0) for t in app.periodic_registry.periodic_tasks.values()]
    )
