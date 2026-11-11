"""Reminders API."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.reminders.service import snooze_target
from tests.applications.helpers import API, get_application, get_timeline
from tests.factories import make_company, make_job


async def test_create_standalone_and_attached_reminders(client, session):
    r = await client.post(
        f"{API}/reminders",
        json={"kind": "custom", "title": "Update resume", "due_at": "2099-02-01T10:00:00Z"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["job"] is None and body["state"] == "pending" and body["overdue"] is False
    assert body["notify_at"].startswith("2099-02-01T09:00")  # default lead 60 min

    company = await make_company(session, "Hooli")
    job = await make_job(session, company, title="Dev")
    r = await client.post(
        f"{API}/reminders",
        json={
            "kind": "follow_up",
            "title": "Ping recruiter",
            "due_at": "2099-02-03T10:00:00Z",
            "job_id": job.id,
            "notify": False,
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["notify_at"] is None
    assert body["job"]["job_id"] == job.id and body["job"]["company"]["name"] == "Hooli"
    assert body["job"]["title"] == "Dev"
    assert "reminder_created" in [e["event_type"] for e in await get_timeline(client, job.id)]
    assert (await get_application(client, job.id))["next_follow_up_at"].startswith("2099-02-03T10:00")

    r = await client.post(
        f"{API}/reminders",
        json={
            "kind": "custom",
            "title": "x",
            "due_at": "2099-02-03T10:00:00Z",
            "notify_at": "2099-02-03T08:00:00Z",
        },
    )
    assert r.json()["notify_at"].startswith("2099-02-03T08:00")


async def test_create_reminder_errors(client, session):
    base = {"kind": "custom", "title": "x", "due_at": "2099-02-01T10:00:00Z"}
    assert (await client.post(f"{API}/reminders", json={**base, "job_id": 999999})).status_code == 404
    assert (await client.post(f"{API}/reminders", json={**base, "interview_id": 999999})).status_code == 404
    assert (await client.post(f"{API}/reminders", json={**base, "title": ""})).status_code == 422
    job = await make_job(session)
    other = await make_job(session)
    r = await client.post(f"{API}/jobs/{other.id}/interviews", json={"round_name": "R", "create_reminders": False})
    iid = r.json()["id"]
    r = await client.post(f"{API}/reminders", json={**base, "job_id": job.id, "interview_id": iid})
    assert r.status_code == 422


async def test_list_filters_ordering_and_overdue(client, session):
    job = await make_job(session)
    now = datetime.now(UTC)
    ids = {}
    for name, delta, kind in (("late", 3, "custom"), ("overdue", -2, "follow_up"), ("soon", 1, "deadline")):
        r = await client.post(
            f"{API}/reminders",
            json={
                "kind": kind,
                "title": name,
                "due_at": (now + timedelta(days=delta)).isoformat(),
                "job_id": job.id,
            },
        )
        ids[name] = r.json()["id"]
    items = (await client.get(f"{API}/reminders")).json()
    assert [i["title"] for i in items] == ["overdue", "soon", "late"]
    assert [i["overdue"] for i in items] == [True, False, False]
    items = (await client.get(f"{API}/reminders", params={"kind": "deadline"})).json()
    assert [i["title"] for i in items] == ["soon"]
    items = (
        await client.get(
            f"{API}/reminders",
            params={"due_from": (now).isoformat(), "due_to": (now + timedelta(days=2)).isoformat()},
        )
    ).json()
    assert [i["title"] for i in items] == ["soon"]
    await client.post(f"{API}/reminders/{ids['soon']}/complete")
    assert "soon" not in [i["title"] for i in (await client.get(f"{API}/reminders")).json()]
    done = (await client.get(f"{API}/reminders", params={"state": "done"})).json()
    assert [i["title"] for i in done] == ["soon"] and done[0]["overdue"] is False
    both = (await client.get(f"{API}/reminders", params=[("state", "done"), ("state", "pending")])).json()
    assert len(both) == 3


async def test_update_dismiss_delete_and_next_follow_up_sync(client, session):
    job = await make_job(session)
    r = await client.post(
        f"{API}/reminders",
        json={"kind": "follow_up", "title": "FU", "due_at": "2099-05-01T10:00:00Z", "job_id": job.id},
    )
    rid = r.json()["id"]
    assert (await get_application(client, job.id))["next_follow_up_at"].startswith("2099-05-01T10:00")

    r = await client.patch(f"{API}/reminders/{rid}", json={"due_at": "2099-05-02T12:00:00Z", "title": "FU2"})
    assert r.status_code == 200
    assert r.json()["title"] == "FU2" and r.json()["notify_at"].startswith("2099-05-02T11:00")  # lead preserved
    assert (await get_application(client, job.id))["next_follow_up_at"].startswith("2099-05-02T12:00")

    # earliest pending follow_up wins
    r2 = await client.post(
        f"{API}/reminders",
        json={"kind": "follow_up", "title": "earlier", "due_at": "2099-04-01T10:00:00Z", "job_id": job.id},
    )
    assert (await get_application(client, job.id))["next_follow_up_at"].startswith("2099-04-01T10:00")
    # changing kind away from follow_up drops it
    await client.patch(f"{API}/reminders/{r2.json()['id']}", json={"kind": "custom"})
    assert (await get_application(client, job.id))["next_follow_up_at"].startswith("2099-05-02T12:00")
    r = await client.patch(f"{API}/reminders/{rid}", json={"notify": False})
    assert r.json()["notify_at"] is None

    r = await client.post(f"{API}/reminders/{rid}/dismiss")
    assert r.json()["state"] == "dismissed"
    assert (await get_application(client, job.id))["next_follow_up_at"] is None

    assert (await client.delete(f"{API}/reminders/{rid}")).status_code == 204
    assert (await client.delete(f"{API}/reminders/{rid}")).status_code == 404
    for path in ("complete", "dismiss"):
        assert (await client.post(f"{API}/reminders/{rid}/{path}")).status_code == 404
    assert (await client.patch(f"{API}/reminders/{rid}", json={"title": "x"})).status_code == 404


async def test_delete_pending_follow_up_resyncs(client, session):
    job = await make_job(session)
    r = await client.post(
        f"{API}/reminders",
        json={"kind": "follow_up", "title": "FU", "due_at": "2099-05-01T10:00:00Z", "job_id": job.id},
    )
    assert (await client.delete(f"{API}/reminders/{r.json()['id']}")).status_code == 204
    assert (await get_application(client, job.id))["next_follow_up_at"] is None


async def test_snooze_presets_and_errors(client, session):
    r = await client.post(f"{API}/reminders", json={"kind": "custom", "title": "S", "due_at": "2099-01-01T10:00:00Z"})
    rid = r.json()["id"]
    before = datetime.now(UTC)
    r = await client.post(f"{API}/reminders/{rid}/snooze", json={"preset": "1h"})
    assert r.status_code == 200
    due = datetime.fromisoformat(r.json()["due_at"])
    assert before + timedelta(minutes=59) < due < before + timedelta(minutes=61)
    assert r.json()["notify_at"] is not None and r.json()["state"] == "pending"
    r = await client.post(f"{API}/reminders/{rid}/snooze", json={"preset": "tomorrow"})
    due = datetime.fromisoformat(r.json()["due_at"]).astimezone(ZoneInfo("Asia/Kolkata"))
    assert (due.hour, due.minute) == (9, 0)
    r = await client.post(f"{API}/reminders/{rid}/snooze", json={"preset": "next_week"})
    due = datetime.fromisoformat(r.json()["due_at"]).astimezone(ZoneInfo("Asia/Kolkata"))
    assert due.weekday() == 0 and (due.hour, due.minute) == (9, 0)

    assert (await client.post(f"{API}/reminders/{rid}/snooze", json={})).status_code == 422
    assert (await client.post(f"{API}/reminders/{rid}/snooze", json={"preset": "bogus"})).status_code == 422
    assert (await client.post(f"{API}/reminders/999999/snooze", json={"preset": "1h"})).status_code == 404
    await client.post(f"{API}/reminders/{rid}/complete")
    assert (await client.post(f"{API}/reminders/{rid}/snooze", json={"preset": "1h"})).status_code == 409


def test_snooze_target_math_in_local_time():
    tz = ZoneInfo("Asia/Kolkata")
    # Wednesday 2026-11-11 22:00 UTC == Thursday 2026-11-12 03:30 IST
    now = datetime(2026, 11, 11, 22, 0, tzinfo=UTC)
    assert snooze_target("1h", now, tz) == now + timedelta(hours=1)
    assert snooze_target("3h", now, tz) == now + timedelta(hours=3)
    # tomorrow = Friday 09:00 IST = 03:30 UTC
    assert snooze_target("tomorrow", now, tz) == datetime(2026, 11, 13, 3, 30, tzinfo=UTC)
    # next Monday (2026-11-16) 09:00 IST
    assert snooze_target("next_week", now, tz) == datetime(2026, 11, 16, 3, 30, tzinfo=UTC)
    # on a Monday, next_week is the following Monday
    monday = datetime(2026, 11, 16, 5, 0, tzinfo=UTC)
    assert snooze_target("next_week", monday, tz) == datetime(2026, 11, 23, 3, 30, tzinfo=UTC)
