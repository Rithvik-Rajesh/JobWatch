"""Status lifecycle: change_status via HTTP, milestones, idempotency, concurrency, availability."""

from datetime import UTC, datetime

from sqlalchemy import select

from app.applications import service
from app.models import ApplicationEvent, Job, Reminder
from app.models.enums import Availability, ReminderKind, ReminderState
from tests.applications.helpers import API, get_application, get_timeline, put_status
from tests.factories import application_for, make_job


async def test_full_lifecycle_timeline_and_milestones(client, session):
    job = await make_job(session)
    steps = [
        ("interested", "2026-11-01T09:00:00Z"),
        ("shortlisted", "2026-11-02T09:00:00Z"),
        ("applied", "2026-11-03T09:00:00Z"),
        ("assessment", "2026-11-04T09:00:00Z"),
        ("interviewing", "2026-11-05T09:00:00Z"),
        ("offer", "2026-11-06T09:00:00Z"),
    ]
    for key, at in steps:
        r = await put_status(client, job.id, key, occurred_at=at)
        assert r.status_code == 200, r.text
        assert r.json()["changed"] is True
        assert r.json()["application"]["status"]["key"] == key

    app = await get_application(client, job.id)
    assert app["status"]["key"] == "offer"
    assert app["interested_at"].startswith("2026-11-01T09:00")
    assert app["shortlisted_at"].startswith("2026-11-02T09:00")
    assert app["applied_at"].startswith("2026-11-03T09:00")
    assert app["closed_at"] is None
    assert app["reviewed_at"] is not None
    assert app["row_version"] == 7  # 1 + six changes

    # Re-entering a category never overwrites the milestone.
    await put_status(client, job.id, "applied", occurred_at="2026-11-09T09:00:00Z")
    await put_status(client, job.id, "interested", occurred_at="2026-11-10T09:00:00Z")
    app = await get_application(client, job.id)
    assert app["applied_at"].startswith("2026-11-03T09:00")
    assert app["interested_at"].startswith("2026-11-01T09:00")

    events = [e for e in await get_timeline(client, job.id) if e["event_type"] == "status_changed"]
    assert [e["to_status"]["key"] for e in events][:3] == ["interested", "applied", "offer"]
    ordered = list(reversed(events))
    assert [e["to_status"]["key"] for e in ordered] == [
        "interested",
        "shortlisted",
        "applied",
        "assessment",
        "interviewing",
        "offer",
        "applied",
        "interested",
    ]
    assert ordered[0]["from_status"]["key"] == "new"
    assert ordered[0]["summary"] == "Marked Interested"
    assert ordered[1]["from_status"]["key"] == "interested"
    all_events = await get_timeline(client, job.id)
    times = [e["occurred_at"] for e in all_events]
    assert times == sorted(times, reverse=True)
    assert "discovered" in [e["event_type"] for e in all_events]


async def test_same_status_is_idempotent_and_marks_reviewed(client, session):
    job = await make_job(session)
    before = await application_for(session, job)
    assert before.reviewed_at is None

    r = await put_status(client, job.id, "new")
    assert r.status_code == 200
    body = r.json()
    assert body["changed"] is False
    assert body["application"]["reviewed_at"] is not None  # acting on it takes it out of the inbox
    assert body["application"]["row_version"] == 1
    assert len(await get_timeline(client, job.id)) == 1  # only 'discovered'

    r = await put_status(client, job.id, "interested")
    assert r.json()["changed"] is True
    r = await put_status(client, job.id, "interested")
    assert r.json()["changed"] is False
    assert r.json()["application"]["row_version"] == 2
    events = await get_timeline(client, job.id)
    assert [e["event_type"] for e in events].count("status_changed") == 1


async def test_back_dated_occurred_at(client, session):
    job = await make_job(session)
    r = await put_status(client, job.id, "applied", occurred_at="2026-11-01T08:30:00Z")
    app = r.json()["application"]
    assert app["applied_at"].startswith("2026-11-01T08:30")
    assert app["status_changed_at"].startswith("2026-11-01T08:30")
    events = await get_timeline(client, job.id)
    changed = next(e for e in events if e["event_type"] == "status_changed")
    assert changed["occurred_at"].startswith("2026-11-01T08:30")
    # follow-up is due applied_at + 7 days
    reminder_id = r.json()["created_reminder_id"]
    reminder = await session.get(Reminder, reminder_id)
    assert reminder.due_at == datetime(2026, 11, 8, 8, 30, tzinfo=UTC)


async def test_stale_row_version_conflicts_and_changes_nothing(client, session):
    job = await make_job(session)
    r = await put_status(client, job.id, "interested")
    version = r.json()["application"]["row_version"]
    r = await put_status(client, job.id, "applied", expected_row_version=version - 1)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "stale"
    app = await get_application(client, job.id)
    assert app["status"]["key"] == "interested"
    assert app["row_version"] == version
    assert app["applied_at"] is None
    assert [e["event_type"] for e in await get_timeline(client, job.id)].count("status_changed") == 1
    assert (await session.scalars(select(Reminder))).all() == []

    r = await put_status(client, job.id, "applied", expected_row_version=version)
    assert r.status_code == 200 and r.json()["changed"] is True


async def test_availability_and_status_are_independent(client, session):
    job = await make_job(session)
    await put_status(client, job.id, "interviewing")
    # System closes the posting; the user's status must stay untouched ...
    db_job = await session.get(Job, job.id)
    db_job.availability = Availability.closed
    await session.flush()
    assert (await get_application(client, job.id))["status"]["key"] == "interviewing"

    # ... and a closed job can still move through statuses without reopening it.
    r = await put_status(client, job.id, "offer")
    assert r.status_code == 200
    assert r.json()["application"]["status"]["key"] == "offer"
    await session.refresh(db_job)
    assert db_job.availability == Availability.closed

    r = await put_status(client, job.id, "rejected", reason="Position filled")
    assert r.json()["application"]["status"]["key"] == "rejected"
    await session.refresh(db_job)
    assert db_job.availability == Availability.closed

    # Vice versa: changing availability never changes status.
    other = await make_job(session)
    await put_status(client, other.id, "shortlisted")
    db_other = await session.get(Job, other.id)
    db_other.availability = Availability.open
    await session.flush()
    assert (await get_application(client, other.id))["status"]["key"] == "shortlisted"


async def test_closed_categories_set_and_clear_closed_at(client, session):
    job = await make_job(session)
    r = await put_status(client, job.id, "rejected", reason="Not a fit", occurred_at="2026-11-05T10:00:00Z")
    app = r.json()["application"]
    assert app["closed_at"].startswith("2026-11-05T10:00")
    assert app["outcome_reason"] == "Not a fit"

    r = await put_status(client, job.id, "interviewing")
    app = r.json()["application"]
    assert app["closed_at"] is None  # moving out of a closed category clears it
    assert app["outcome_reason"] == "Not a fit"  # not part of the contract to clear it

    r = await put_status(client, job.id, "withdrawn", occurred_at="2026-11-07T10:00:00Z")
    assert r.json()["application"]["closed_at"].startswith("2026-11-07T10:00")


async def test_status_change_with_note_adds_note_and_event(client, session):
    job = await make_job(session)
    r = await put_status(client, job.id, "interested", note="Looks great")
    assert r.status_code == 200
    notes = (await client.get(f"{API}/jobs/{job.id}/notes")).json()
    assert [n["body"] for n in notes] == ["Looks great"]
    types = [e["event_type"] for e in await get_timeline(client, job.id)]
    assert "note_added" in types and "status_changed" in types


async def test_auto_follow_up_and_next_follow_up_sync(client, session):
    job = await make_job(session, title="Backend Engineer")
    r = await put_status(client, job.id, "applied", occurred_at="2026-11-01T10:00:00Z")
    rid = r.json()["created_reminder_id"]
    assert rid is not None
    rem = await session.get(Reminder, rid)
    assert rem.kind == ReminderKind.follow_up
    assert rem.title.startswith("Follow up on Backend Engineer at ")
    assert rem.due_at == datetime(2026, 11, 8, 10, 0, tzinfo=UTC)
    assert rem.notify_at == datetime(2026, 11, 8, 9, 0, tzinfo=UTC)  # 60 min lead
    app = await get_application(client, job.id)
    assert app["next_follow_up_at"].startswith("2026-11-08T10:00")
    types = [e["event_type"] for e in await get_timeline(client, job.id)]
    assert "reminder_created" in types

    # Snooze moves next_follow_up_at
    r = await client.post(f"{API}/reminders/{rid}/snooze", json={"until": "2026-11-20T10:00:00Z"})
    assert r.status_code == 200
    assert r.json()["state"] == "pending"
    assert (await get_application(client, job.id))["next_follow_up_at"].startswith("2026-11-20T10:00")

    # Complete clears it
    r = await client.post(f"{API}/reminders/{rid}/complete")
    assert r.json()["state"] == "done" and r.json()["completed_at"]
    assert (await get_application(client, job.id))["next_follow_up_at"] is None
    types = [e["event_type"] for e in await get_timeline(client, job.id)]
    assert "reminder_completed" in types

    # Re-entering applied does not create a second reminder (applied_at already set)
    await put_status(client, job.id, "interviewing")
    r = await put_status(client, job.id, "applied")
    assert r.json()["created_reminder_id"] is None
    count = len((await session.scalars(select(Reminder).where(Reminder.application_id == rem.application_id))).all())
    assert count == 1


async def test_auto_follow_up_can_be_disabled(client, session):
    from app.settings.registry import put_setting

    await put_setting(session, "reminder_defaults", {"follow_up_after_applied_days": None})
    job = await make_job(session)
    r = await put_status(client, job.id, "applied")
    assert r.json()["created_reminder_id"] is None
    assert (await get_application(client, job.id))["next_follow_up_at"] is None


async def test_status_change_errors(client, session):
    job = await make_job(session)
    assert (await put_status(client, 999999, "applied")).status_code == 404
    r = await put_status(client, job.id, "no_such_status")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_status"

    from app.models import Status

    st = await session.scalar(select(Status).where(Status.key == "assessment"))
    from datetime import UTC, datetime

    st.archived_at = datetime.now(UTC)
    await session.flush()
    r = await put_status(client, job.id, "assessment")
    assert r.status_code == 422

    merged = await make_job(session)
    db = await session.get(Job, merged.id)
    db.merged_into_job_id = job.id
    await session.flush()
    r = await put_status(client, merged.id, "applied")
    assert r.status_code == 409
    assert (await client.get(f"{API}/jobs/999999/application")).status_code == 404


async def test_service_change_status_contract(session):
    """The cross-module contract used by the Jobs agent."""
    job = await make_job(session)
    outcome = await service.change_status(session, job.id, "applied")
    assert outcome.changed is True
    assert outcome.application.status_id
    assert outcome.created_reminder_id is not None
    again = await service.change_status(session, job.id, "applied")
    assert again.changed is False and again.created_reminder_id is None
    n = len(
        (await session.scalars(select(ApplicationEvent).where(ApplicationEvent.event_type == "status_changed"))).all()
    )
    assert n == 1
    r = await session.get(Reminder, outcome.created_reminder_id)
    assert r.state == ReminderState.pending


async def test_patch_application_fields_events_and_versions(client, session):
    job = await make_job(session)
    app = await get_application(client, job.id)
    r = await client.patch(
        f"{API}/jobs/{job.id}/application",
        json={
            "expected_row_version": app["row_version"],
            "priority": 2,
            "starred": True,
            "next_action": "Email recruiter",
            "applied_at": "2026-11-02T00:00:00Z",
            "offer_details": {"base": 100},
        },
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["priority"] == 2 and out["starred"] is True
    assert out["row_version"] == app["row_version"] + 1
    assert out["next_action"] == "Email recruiter"
    events = [e for e in await get_timeline(client, job.id) if e["event_type"] == "field_changed"]
    fields = {e["payload"]["field"]: e["payload"] for e in events}
    assert set(fields) == {"priority", "starred", "next_action", "applied_at", "offer_details"}
    assert fields["priority"]["before"] is None and fields["priority"]["after"] == 2

    # Only provided fields are applied; unchanged values add no event
    r = await client.patch(
        f"{API}/jobs/{job.id}/application", json={"expected_row_version": out["row_version"], "priority": 2}
    )
    assert r.status_code == 200 and r.json()["row_version"] == out["row_version"]
    r = await client.patch(
        f"{API}/jobs/{job.id}/application", json={"expected_row_version": out["row_version"], "priority": None}
    )
    assert r.json()["priority"] is None and r.json()["next_action"] == "Email recruiter"

    # Stale -> 409, missing resume version -> 422
    r = await client.patch(f"{API}/jobs/{job.id}/application", json={"expected_row_version": 1, "priority": 3})
    assert r.status_code == 409 and r.json()["error"]["code"] == "stale"
    cur = (await get_application(client, job.id))["row_version"]
    r = await client.patch(
        f"{API}/jobs/{job.id}/application", json={"expected_row_version": cur, "resume_version_id": 424242}
    )
    assert r.status_code == 422
    r = await client.patch(f"{API}/jobs/{job.id}/application", json={"priority": 1})
    assert r.status_code == 422  # expected_row_version is required
    r = await client.patch(f"{API}/jobs/999999/application", json={"expected_row_version": 1})
    assert r.status_code == 404


async def test_timeline_manual_entry(client, session):
    job = await make_job(session)
    r = await client.post(
        f"{API}/jobs/{job.id}/timeline",
        json={"summary": "Phone call with recruiter", "occurred_at": "2026-11-03T10:00:00Z", "payload": {"x": 1}},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["event_type"] == "manual" and body["actor"] == "user"
    assert body["payload"] == {"x": 1}
    events = await get_timeline(client, job.id)
    assert events[0]["summary"] == "Phone call with recruiter" or events[1]["summary"] == "Phone call with recruiter"
    assert (await client.post(f"{API}/jobs/999999/timeline", json={"summary": "x"})).status_code == 404
    assert (await client.post(f"{API}/jobs/{job.id}/timeline", json={"summary": ""})).status_code == 422


async def test_timeline_is_append_only_no_edit_routes(client, session):
    job = await make_job(session)
    events = await get_timeline(client, job.id)
    r = await client.patch(f"{API}/jobs/{job.id}/timeline/{events[0]['id']}", json={"summary": "x"})
    assert r.status_code in (404, 405)
    r = await client.delete(f"{API}/jobs/{job.id}/timeline/{events[0]['id']}")
    assert r.status_code in (404, 405)
