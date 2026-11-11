"""Notes, contacts, interviews (+ their reminders)."""

from sqlalchemy import select

from app.models import Reminder
from app.models.enums import ReminderKind, ReminderState
from tests.applications.helpers import API, get_application, get_timeline
from tests.factories import make_company, make_job


async def test_notes_crud_and_events(client, session):
    job = await make_job(session)
    r = await client.post(f"{API}/jobs/{job.id}/notes", json={"body": "First note"})
    assert r.status_code == 201, r.text
    nid = r.json()["id"]
    await client.post(f"{API}/jobs/{job.id}/notes", json={"body": "Pinned one", "pinned": True})
    notes = (await client.get(f"{API}/jobs/{job.id}/notes")).json()
    assert [n["body"] for n in notes] == ["Pinned one", "First note"]
    assert [e["event_type"] for e in await get_timeline(client, job.id)].count("note_added") == 2

    r = await client.patch(f"{API}/notes/{nid}", json={"body": "Edited", "pinned": True})
    assert r.status_code == 200 and r.json()["body"] == "Edited" and r.json()["pinned"] is True
    assert (await client.delete(f"{API}/notes/{nid}")).status_code == 204
    assert len((await client.get(f"{API}/jobs/{job.id}/notes")).json()) == 1
    # note_added events stay (append-only history)
    assert [e["event_type"] for e in await get_timeline(client, job.id)].count("note_added") == 2

    assert (await client.patch(f"{API}/notes/{nid}", json={"body": "x"})).status_code == 404
    assert (await client.delete(f"{API}/notes/{nid}")).status_code == 404
    assert (await client.get(f"{API}/jobs/999999/notes")).status_code == 404
    assert (await client.post(f"{API}/jobs/999999/notes", json={"body": "x"})).status_code == 404
    assert (await client.post(f"{API}/jobs/{job.id}/notes", json={"body": ""})).status_code == 422


async def test_contacts_crud(client, session):
    job = await make_job(session)
    r = await client.post(
        f"{API}/jobs/{job.id}/contacts",
        json={"role": "recruiter", "name": "Priya", "email": "priya@example.com", "profile_url": "https://l.in/p"},
    )
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    assert r.json()["profile_url"].startswith("https://l.in/p")
    r = await client.patch(f"{API}/contacts/{cid}", json={"title": "Talent Lead", "role": "hiring_manager"})
    assert r.status_code == 200 and r.json()["title"] == "Talent Lead" and r.json()["role"] == "hiring_manager"
    assert r.json()["name"] == "Priya"
    assert len((await client.get(f"{API}/jobs/{job.id}/contacts")).json()) == 1
    assert (await client.post(f"{API}/jobs/{job.id}/contacts", json={"role": "bogus", "name": "x"})).status_code == 422
    assert (await client.delete(f"{API}/contacts/{cid}")).status_code == 204
    assert (await client.delete(f"{API}/contacts/{cid}")).status_code == 404
    assert (await client.patch(f"{API}/contacts/{cid}", json={"name": "z"})).status_code == 404
    assert (await client.get(f"{API}/jobs/999999/contacts")).status_code == 404


async def _reminders(session, interview_id):
    return (
        await session.scalars(select(Reminder).where(Reminder.interview_id == interview_id).order_by(Reminder.kind))
    ).all()


async def test_interview_reminders_created_moved_and_dismissed(client, session):
    company = await make_company(session, "Initech")
    job = await make_job(session, company, title="SRE")
    r = await client.post(
        f"{API}/jobs/{job.id}/interviews",
        json={"round_name": "System design", "kind": "system_design", "scheduled_at": "2099-03-10T10:00:00Z"},
    )
    assert r.status_code == 201, r.text
    iid = r.json()["id"]
    assert r.json()["outcome"] == "pending"
    assert "interview_scheduled" in [e["event_type"] for e in await get_timeline(client, job.id)]

    rems = {x.kind: x for x in await _reminders(session, iid)}
    assert set(rems) == {ReminderKind.interview, ReminderKind.interview_prep}
    from datetime import UTC, datetime

    assert rems[ReminderKind.interview].due_at == datetime(2099, 3, 10, 10, 0, tzinfo=UTC)
    assert rems[ReminderKind.interview].notify_at == datetime(2099, 3, 10, 9, 0, tzinfo=UTC)
    assert rems[ReminderKind.interview_prep].due_at == datetime(2099, 3, 9, 10, 0, tzinfo=UTC)
    assert "Initech" in rems[ReminderKind.interview].title

    # Reschedule moves both pending reminders and records an event
    r = await client.patch(f"{API}/interviews/{iid}", json={"scheduled_at": "2099-03-12T14:00:00Z"})
    assert r.status_code == 200 and r.json()["scheduled_at"].startswith("2099-03-12T14:00")
    rems = {x.kind: x for x in await _reminders(session, iid)}
    for x in rems.values():
        await session.refresh(x)
    assert rems[ReminderKind.interview].due_at == datetime(2099, 3, 12, 14, 0, tzinfo=UTC)
    assert rems[ReminderKind.interview].notify_at == datetime(2099, 3, 12, 13, 0, tzinfo=UTC)
    assert rems[ReminderKind.interview_prep].due_at == datetime(2099, 3, 11, 14, 0, tzinfo=UTC)
    assert "interview_updated" in [e["event_type"] for e in await get_timeline(client, job.id)]

    # Cancel dismisses them; they no longer show in the default (pending) list
    r = await client.patch(f"{API}/interviews/{iid}", json={"outcome": "cancelled"})
    assert r.status_code == 200 and r.json()["outcome"] == "cancelled"
    for x in await _reminders(session, iid):
        await session.refresh(x)
        assert x.state == ReminderState.dismissed
    pending = (await client.get(f"{API}/reminders", params={"job_id": job.id})).json()
    assert pending == []

    assert len((await client.get(f"{API}/jobs/{job.id}/interviews")).json()) == 1


async def test_interview_without_reminders_or_time(client, session):
    job = await make_job(session)
    r = await client.post(f"{API}/jobs/{job.id}/interviews", json={"round_name": "Screen", "scheduled_at": None})
    assert r.status_code == 201
    assert await _reminders(session, r.json()["id"]) == []
    r2 = await client.post(
        f"{API}/jobs/{job.id}/interviews",
        json={"round_name": "HR", "scheduled_at": "2099-01-01T10:00:00Z", "create_reminders": False},
    )
    assert await _reminders(session, r2.json()["id"]) == []


async def test_interview_delete_and_errors(client, session):
    job = await make_job(session)
    r = await client.post(
        f"{API}/jobs/{job.id}/interviews", json={"round_name": "Tech", "scheduled_at": "2099-01-01T10:00:00Z"}
    )
    iid = r.json()["id"]
    assert (await client.delete(f"{API}/interviews/{iid}")).status_code == 204
    assert (await client.delete(f"{API}/interviews/{iid}")).status_code == 404
    assert (await client.patch(f"{API}/interviews/{iid}", json={"notes": "x"})).status_code == 404
    assert (await client.get(f"{API}/reminders", params={"job_id": job.id})).json() == []
    assert (await client.post(f"{API}/jobs/999999/interviews", json={"round_name": "x"})).status_code == 404
    assert (
        await client.post(f"{API}/jobs/{job.id}/interviews", json={"round_name": "x", "kind": "zzz"})
    ).status_code == 422
    app = await get_application(client, job.id)
    assert app["next_follow_up_at"] is None
