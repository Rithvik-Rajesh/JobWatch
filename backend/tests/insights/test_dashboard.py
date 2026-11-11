"""Dashboard aggregates against real Postgres with exact expected numbers."""

from datetime import UTC, datetime, timedelta

import pytest

from app.applications.lifecycle import record_event
from app.dashboard import service
from app.models import Interview, Posting, Reminder
from app.models.enums import (
    Availability,
    EventType,
    HealthStatus,
    ReminderKind,
    ReminderState,
    ScopeMatch,
)
from app.settings.registry import put_setting
from tests.factories import application_for, make_company, make_job, source_id
from tests.insights.helpers import move

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)  # 17:30 on 11 Nov in Asia/Kolkata
D = timedelta


async def _seed_summary_world(session):
    a = await make_company(session, "Alpha", monitoring_enabled=True, health=HealthStatus.failing)
    a.last_success_at = NOW - D(days=2)
    b = await make_company(session, "Beta", monitoring_enabled=True, health=HealthStatus.healthy)
    b.last_success_at = NOW - D(hours=3)
    c = await make_company(session, "Gamma", monitoring_enabled=True, health=HealthStatus.failing)
    c.archived_at = NOW
    c.last_success_at = NOW  # archived: must not count
    await make_company(session, "Delta")  # not monitored

    j1 = await make_job(session, a, first_seen_at=NOW - D(hours=1))  # new today, unreviewed
    j2 = await make_job(session, a, first_seen_at=NOW - D(days=2))
    await move(session, j2, "interested", NOW - D(days=1))
    j3 = await make_job(session, b, first_seen_at=NOW - D(days=3))
    await move(session, j3, "shortlisted", NOW - D(days=1))
    j4 = await make_job(session, b, country_codes=[], first_seen_at=NOW - D(days=5))  # unknown scope
    await move(session, j4, "applied", NOW - D(days=4), applied=True)
    j5 = await make_job(session, b, first_seen_at=NOW - D(days=6))
    await move(session, j5, "interviewing", NOW - D(days=2), applied=True)
    j6 = await make_job(session, a, first_seen_at=NOW - D(days=7), availability=Availability.closed)
    await move(session, j6, "offer", NOW - D(days=1), applied=True)
    j7 = await make_job(session, a, first_seen_at=NOW - D(days=8))
    await move(session, j7, "rejected", NOW - D(days=1), applied=True)
    # excluded: US-only (out_of_scope) and merged
    await make_job(session, a, country_codes=["US"], first_seen_at=NOW - D(hours=1))
    merged = await make_job(session, a, first_seen_at=NOW - D(hours=1))
    merged.merged_into_job_id = j2.id
    await session.flush()
    return j1, j2, j3, j4, j5, j6, j7


async def test_summary_exact_counts_with_scope(session):
    await _seed_summary_world(session)
    s = await service.summary(session, now=NOW)
    assert s.total_discovered == 7  # US-only and merged excluded, unknown included
    assert s.new_today == 1  # only j1: first seen after 00:00 IST today
    assert s.unreviewed == 1
    assert (s.interested, s.shortlisted) == (1, 1)
    assert s.applied == 4  # j4 j5 j6 j7 ever applied
    assert (s.in_process, s.offers, s.rejected) == (1, 1, 1)
    assert s.open_jobs == 6
    assert s.companies_monitored == 2
    assert s.companies_failing == 1
    assert s.last_successful_scan_at == NOW - D(hours=3)


async def test_summary_new_today_uses_timezone_boundary(session):
    co = await make_company(session)
    # 18:00Z on 10 Nov is 23:30 IST on 10 Nov (yesterday); 19:00Z is 00:30 IST on 11 Nov (today)
    await make_job(session, co, first_seen_at=datetime(2026, 11, 10, 18, 0, tzinfo=UTC))
    await make_job(session, co, first_seen_at=datetime(2026, 11, 10, 19, 0, tzinfo=UTC))
    assert (await service.summary(session, now=NOW)).new_today == 1
    await put_setting(session, "timezone", {"name": "UTC"})
    assert (await service.summary(session, now=NOW)).new_today == 0


async def test_summary_hides_unknown_when_setting_off(session):
    co = await make_company(session)
    await make_job(session, co)
    await make_job(session, co, country_codes=[])
    assert (await service.summary(session, now=NOW)).total_discovered == 2
    await put_setting(
        session, "location_scope", {"countries": ["IN"], "include_unknown": False, "include_remote_anywhere": False}
    )
    assert (await service.summary(session, now=NOW)).total_discovered == 1


async def test_summary_empty(session):
    s = await service.summary(session, now=NOW)
    assert s.total_discovered == 0 and s.last_successful_scan_at is None


async def test_scope_override_uses_location_scope_column(session):
    co = await make_company(session)
    await make_job(session, co, country_codes=["US"], location_scope=ScopeMatch.in_scope, scope_overridden=True)
    assert (await service.summary(session, now=NOW)).total_discovered == 1


# ----------------------------------------------------------------------------------- timeseries


async def _points(session, metric, bucket="day", days=7):
    ts = await service.timeseries(session, metric, bucket, days, now=NOW)
    return ts, {p.bucket: p.value for p in ts.points}


async def test_timeseries_discovered_zero_filled_and_timezone_bucketed(session):
    co = await make_company(session)
    await make_job(session, co, first_seen_at=datetime(2026, 11, 10, 20, 0, tzinfo=UTC))  # IST 11 Nov 01:30
    await make_job(session, co, first_seen_at=datetime(2026, 11, 10, 10, 0, tzinfo=UTC))  # IST 10 Nov
    await make_job(session, co, first_seen_at=datetime(2026, 11, 10, 11, 0, tzinfo=UTC))  # IST 10 Nov
    await make_job(session, co, first_seen_at=NOW - D(days=40))  # outside window
    await make_job(session, co, country_codes=["US"], first_seen_at=NOW - D(days=1))  # out of scope
    ts, pts = await _points(session, "discovered")
    assert [p.bucket for p in ts.points] == [f"2026-11-{d:02d}" for d in range(4, 12)]  # oldest first, 8 buckets
    assert pts["2026-11-11"] == 1
    assert pts["2026-11-10"] == 2
    assert sum(pts.values()) == 3
    assert pts["2026-11-05"] == 0  # zero-filled
    assert ts.metric == "discovered" and ts.bucket == "day"


async def test_timeseries_follows_timezone_setting(session):
    co = await make_company(session)
    await make_job(session, co, first_seen_at=datetime(2026, 11, 10, 20, 0, tzinfo=UTC))
    await put_setting(session, "timezone", {"name": "UTC"})
    _, pts = await _points(session, "discovered")
    assert pts["2026-11-10"] == 1 and pts["2026-11-11"] == 0


async def test_timeseries_applied_first_event_with_fallback(session):
    co = await make_company(session)
    j1 = await make_job(session, co)
    await move(session, j1, "applied", datetime(2026, 11, 10, 20, 0, tzinfo=UTC), applied=True)  # IST 11 Nov
    await move(session, j1, "interviewing", datetime(2026, 11, 10, 21, 0, tzinfo=UTC))
    # re-enters 'applied' later: still counted once, at the first event
    await move(session, j1, "applied", datetime(2026, 11, 11, 6, 0, tzinfo=UTC))
    j2 = await make_job(session, co)
    await move(session, j2, "applied", datetime(2026, 11, 8, 6, 0, tzinfo=UTC), applied=True)
    # no event, only applied_at (legacy/imported) -> fallback
    j3 = await make_job(session, co)
    (await application_for(session, j3)).applied_at = datetime(2026, 11, 6, 6, 0, tzinfo=UTC)
    # never applied
    j4 = await make_job(session, co)
    await move(session, j4, "interested", datetime(2026, 11, 9, 6, 0, tzinfo=UTC))
    await session.flush()
    _, pts = await _points(session, "applied")
    assert pts["2026-11-11"] == 1
    assert pts["2026-11-08"] == 1
    assert pts["2026-11-06"] == 1
    assert sum(pts.values()) == 3


async def test_timeseries_status_changes_counts_only_status_events(session):
    co = await make_company(session)
    j = await make_job(session, co)  # 'discovered' event must not count
    await move(session, j, "interested", NOW - D(days=1))
    await move(session, j, "shortlisted", NOW - D(days=1, hours=1))
    app = await application_for(session, j)
    await record_event(session, app.id, EventType.note_added, "n", occurred_at=NOW - D(days=1))
    out = await make_job(session, co, country_codes=["US"])
    await move(session, out, "interested", NOW - D(days=1))
    _, pts = await _points(session, "status_changes")
    assert sum(pts.values()) == 2


async def test_timeseries_week_and_month_buckets(session):
    co = await make_company(session)
    await make_job(session, co, first_seen_at=NOW - D(hours=1))  # Wed 11 Nov -> week of Mon 9 Nov
    await make_job(session, co, first_seen_at=NOW - D(days=10))  # Sun 1 Nov -> week of Mon 26 Oct
    ts, pts = await _points(session, "discovered", "week", 14)
    assert [p.bucket for p in ts.points] == ["2026-10-26", "2026-11-02", "2026-11-09"]
    assert pts == {"2026-10-26": 1, "2026-11-02": 0, "2026-11-09": 1}
    ts, pts = await _points(session, "discovered", "month", 60)
    assert [p.bucket for p in ts.points] == ["2026-09-01", "2026-10-01", "2026-11-01"]
    assert pts == {"2026-09-01": 0, "2026-10-01": 0, "2026-11-01": 2}


# -------------------------------------------------------------------------------------- funnel


async def _seed_funnel(session):
    co = await make_company(session)
    f1 = await make_job(session, co, first_seen_at=NOW - D(days=10))
    for key, d in [("interested", 9), ("shortlisted", 8), ("applied", 7), ("interviewing", 5), ("offer", 2)]:
        await move(session, f1, key, NOW - D(days=d), applied=key == "applied")
    f2 = await make_job(session, co, first_seen_at=NOW - D(days=10))
    await move(session, f2, "interested", NOW - D(days=9))
    await move(session, f2, "new", NOW - D(days=8))  # back to inbox
    await move(session, f2, "interested", NOW - D(days=7))  # re-enters: counted once
    await move(session, f2, "not_interested", NOW - D(days=6))
    f3 = await make_job(session, co, first_seen_at=NOW - D(days=10))
    await move(session, f3, "applied", NOW - D(days=9), applied=True)  # direct: only 'applied'
    f4 = await make_job(session, co, first_seen_at=NOW - D(days=10))
    await move(session, f4, "shortlisted", NOW - D(days=9))  # considering => interested too
    await make_job(session, co, first_seen_at=NOW - D(days=100))  # discovered only, old
    out = await make_job(session, co, country_codes=["US"])
    await move(session, out, "offer", NOW - D(days=1))
    merged = await make_job(session, co)
    merged.merged_into_job_id = f1.id
    await move(session, merged, "interested", NOW - D(days=1))


async def test_funnel_semantics(session):
    await _seed_funnel(session)
    stages = await service.funnel(session, now=NOW)
    assert [s.key for s in stages] == ["discovered", "interested", "shortlisted", "applied", "in_process", "offer"]
    assert [s.count for s in stages] == [5, 3, 2, 2, 1, 1]
    assert [s.conversion_from_previous for s in stages] == [None, 0.6, 0.6667, 1.0, 0.5, 1.0]
    assert stages[0].label == "Discovered"


async def test_funnel_days_window_limits_to_recent_jobs(session):
    await _seed_funnel(session)
    stages = await service.funnel(session, days=30, now=NOW)
    assert [s.count for s in stages] == [4, 3, 2, 2, 1, 1]
    assert stages[1].conversion_from_previous == 0.75


async def test_funnel_conversion_with_empty_stages(session):
    co = await make_company(session)
    j = await make_job(session, co)
    await move(session, j, "applied", NOW - D(days=1), applied=True)
    stages = await service.funnel(session, now=NOW)
    assert [s.count for s in stages] == [1, 0, 0, 1, 0, 0]
    assert stages[1].conversion_from_previous == 0.0
    assert stages[3].conversion_from_previous is None  # previous stage (shortlisted) is empty
    assert stages[4].conversion_from_previous == 0.0


async def test_funnel_empty(session):
    stages = await service.funnel(session, now=NOW)
    assert [s.count for s in stages] == [0] * 6
    assert all(s.conversion_from_previous is None for s in stages)


# ------------------------------------------------------------------------------------ breakdown


async def test_breakdown_status_includes_zero_counts_in_sort_order(session):
    await _seed_summary_world(session)
    bd = await service.breakdown(session, "status")
    assert [i.key for i in bd.items] == [
        "new",
        "interested",
        "shortlisted",
        "applied",
        "assessment",
        "interviewing",
        "offer",
        "rejected",
        "withdrawn",
        "not_interested",
    ]
    assert {i.key: i.count for i in bd.items} == {
        "new": 1,
        "interested": 1,
        "shortlisted": 1,
        "applied": 1,
        "assessment": 0,
        "interviewing": 1,
        "offer": 1,
        "rejected": 1,
        "withdrawn": 0,
        "not_interested": 0,
    }
    assert bd.items[0].label == "New" and bd.items[0].color == "blue"
    assert bd.by == "status"


async def test_breakdown_company_top_n(session):
    x = await make_company(session, "Xco")
    y = await make_company(session, "Yco")
    z = await make_company(session, "Zco")
    for _ in range(3):
        await make_job(session, x)
    for _ in range(2):
        await make_job(session, y)
    await make_job(session, z)
    await make_job(session, z, country_codes=["US"])  # out of scope
    keep = await make_job(session, y)  # Y now has 3 live jobs
    gone = await make_job(session, z)
    gone.merged_into_job_id = keep.id  # merged: excluded
    await session.flush()
    bd = await service.breakdown(session, "company", limit=10)
    assert [(i.label, i.count) for i in bd.items] == [("Xco", 3), ("Yco", 3), ("Zco", 1)]
    top2 = await service.breakdown(session, "company", limit=2)
    assert [(i.label, i.count) for i in top2.items] == [("Xco", 3), ("Yco", 3)]


async def test_breakdown_source_counts_job_once_per_source(session):
    co = await make_company(session)
    j1 = await make_job(session, co)  # career_site
    await make_job(session, co, source_key="manual")
    # j1 also posted on manual, plus a second career_site posting: still counted once per source
    manual = await source_id(session, "manual")
    career = await source_id(session, "career_site")
    for sid, ext in [(manual, "X1"), (career, "X2")]:
        session.add(
            Posting(job_id=j1.id, source_id=sid, company_id=co.id, external_id=ext, source_url=f"https://x/{ext}")
        )
    await make_job(session, co, source_key="manual", country_codes=["US"])  # out of scope
    await session.flush()
    bd = await service.breakdown(session, "source")
    assert {i.key: i.count for i in bd.items} == {"career_site": 1, "manual": 2}
    assert bd.items[0].key == "manual"  # ordered by count desc


# ------------------------------------------------------------------------------------ upcoming


async def test_upcoming_merged_sorted_with_overdue(session):
    co = await make_company(session, "Acme")
    j_int = await make_job(session, co, title="Backend Dev")
    app = await application_for(session, j_int)

    def iv(name, when, outcome="pending"):
        i = Interview(application_id=app.id, round_name=name, scheduled_at=when, outcome=outcome)
        session.add(i)
        return i

    i_future = iv("Round 2", NOW + D(days=2))
    i_recent = iv("Screen", NOW - D(hours=3))  # overdue but within 1 day -> included
    iv("Ancient", NOW - D(days=2))  # excluded: overdue > 1 day
    iv("Done", NOW + D(days=1), outcome="passed")  # excluded
    iv("Far", NOW + D(days=20))  # outside window

    def rem(title, due, state=ReminderState.pending, application_id=app.id):
        r = Reminder(application_id=application_id, kind=ReminderKind.custom, title=title, due_at=due, state=state)
        session.add(r)
        return r

    r_overdue = rem("Follow up", NOW - D(days=5))
    r_soon = rem("Prep", NOW + D(days=1))
    r_loose = rem("Update CV", NOW + D(days=4), application_id=None)
    rem("Finished", NOW + D(days=1), state=ReminderState.done)
    rem("Far", NOW + D(days=30))

    j_dl = await make_job(session, co, title="Deadline Job", deadline_at=NOW + D(days=3))
    await move(session, j_dl, "interested", NOW - D(days=1))
    await make_job(session, co, title="Inbox Job", deadline_at=NOW + D(days=3))  # status new: excluded
    j_rej = await make_job(session, co, title="Rejected Job", deadline_at=NOW + D(days=3))
    await move(session, j_rej, "rejected", NOW - D(days=1))
    j_far = await make_job(session, co, title="Far Job", deadline_at=NOW + D(days=40))
    await move(session, j_far, "interested", NOW - D(days=1))
    await session.flush()

    items = await service.upcoming(session, days=14, now=NOW)
    assert [(i.kind, i.id, i.overdue) for i in items] == [
        ("reminder", r_overdue.id, True),
        ("interview", i_recent.id, True),
        ("reminder", r_soon.id, False),
        ("interview", i_future.id, False),
        ("deadline", j_dl.id, False),
        ("reminder", r_loose.id, False),
    ]
    assert [i.at for i in items] == sorted(i.at for i in items)
    by_kind = {(i.kind, i.id): i for i in items}
    iv_item = by_kind[("interview", i_future.id)]
    assert iv_item.job_id == j_int.id and iv_item.company.name == "Acme" and "Round 2" in iv_item.title
    loose = by_kind[("reminder", r_loose.id)]
    assert loose.job_id is None and loose.company is None
    dl = by_kind[("deadline", j_dl.id)]
    assert dl.job_id == j_dl.id and dl.company.id == co.id


async def test_upcoming_window_days(session):
    co = await make_company(session)
    j = await make_job(session, co)
    app = await application_for(session, j)
    session.add(Reminder(application_id=app.id, kind=ReminderKind.custom, title="a", due_at=NOW + D(days=5)))
    await session.flush()
    assert await service.upcoming(session, days=3, now=NOW) == []
    assert len(await service.upcoming(session, days=7, now=NOW)) == 1


# --------------------------------------------------------------------------------------- HTTP

API = "/api/v1/dashboard"


@pytest.mark.parametrize("path", ["/summary", "/timeseries", "/funnel", "/breakdown", "/upcoming"])
async def test_dashboard_requires_auth(anon_client, path):
    assert (await anon_client.get(API + path)).status_code == 401


async def test_http_summary_and_funnel(client, session):
    co = await make_company(session)
    j = await make_job(session, co)  # first seen now
    await move(session, j, "interested", datetime.now(UTC))
    r = await client.get(f"{API}/summary")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_discovered"] == 1 and body["interested"] == 1
    r = await client.get(f"{API}/funnel", params={"days": 7})
    assert r.status_code == 200
    assert [s["count"] for s in r.json()] == [1, 1, 0, 0, 0, 0]


async def test_http_timeseries_breakdown_upcoming(client, session):
    co = await make_company(session, "Http Co")
    await make_job(session, co)
    r = await client.get(f"{API}/timeseries", params={"metric": "discovered", "bucket": "day", "days": 7})
    assert r.status_code == 200
    points = r.json()["points"]
    assert len(points) in (7, 8) and sum(p["value"] for p in points) == 1
    for by in ("status", "company", "source"):
        r = await client.get(f"{API}/breakdown", params={"by": by})
        assert r.status_code == 200 and r.json()["by"] == by
    r = await client.get(f"{API}/breakdown", params={"by": "company"})
    assert r.json()["items"][0]["label"] == "Http Co"
    r = await client.get(f"{API}/upcoming")
    assert r.status_code == 200 and r.json() == []
    assert (await client.get(f"{API}/timeseries", params={"days": 1})).status_code == 422  # contract: days >= 7
