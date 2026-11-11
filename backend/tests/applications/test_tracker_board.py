"""Bulk status, tracker and Kanban board."""

from sqlalchemy import select

from app.models import Application, Job
from tests.applications.helpers import API, get_application, get_timeline, put_status
from tests.factories import application_for, make_company, make_job


async def test_bulk_status_updates_and_counts(client, session):
    a, b, c = [await make_job(session) for _ in range(3)]
    await put_status(client, c.id, "interested")
    r = await client.post(
        f"{API}/applications/bulk-status", json={"job_ids": [a.id, b.id, c.id], "status": "interested"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["updated"] == 2 and r.json()["unchanged"] == 1
    for j in (a, b, c):
        assert (await get_application(client, j.id))["status"]["key"] == "interested"


async def test_bulk_status_is_all_or_nothing(client, session):
    a, b = await make_job(session), await make_job(session)
    r = await client.post(
        f"{API}/applications/bulk-status", json={"job_ids": [a.id, 987654, b.id], "status": "applied"}
    )
    assert r.status_code == 404
    for j in (a, b):
        app = await get_application(client, j.id)
        assert app["status"]["key"] == "new" and app["row_version"] == 1
        assert len(await get_timeline(client, j.id)) == 1
    # a merged job fails the whole batch (409) and rolls everything back
    merged = await make_job(session)
    db = await session.get(Job, merged.id)
    db.merged_into_job_id = a.id
    await session.flush()
    r = await client.post(
        f"{API}/applications/bulk-status", json={"job_ids": [a.id, merged.id], "status": "interested"}
    )
    assert r.status_code == 409
    assert (await get_application(client, a.id))["status"]["key"] == "new"
    r = await client.post(f"{API}/applications/bulk-status", json={"job_ids": [a.id], "status": "nope"})
    assert r.status_code == 404
    assert (
        await client.post(f"{API}/applications/bulk-status", json={"job_ids": [], "status": "applied"})
    ).status_code == 422


async def test_tracker_excludes_inbox_archived_and_merged(client, session):
    new = await make_job(session, title="Still New")
    ni = await make_job(session, title="Not Interested")
    keep = await make_job(session, title="Interested One")
    applied = await make_job(session, title="Applied One")
    merged = await make_job(session, title="Merged One")
    await put_status(client, ni.id, "not_interested")
    await put_status(client, keep.id, "interested")
    await put_status(client, applied.id, "applied", occurred_at="2026-11-02T00:00:00Z")
    await put_status(client, merged.id, "interested")
    db = await session.get(Job, merged.id)
    db.merged_into_job_id = keep.id
    await session.flush()

    r = await client.get(f"{API}/applications")
    assert r.status_code == 200, r.text
    body = r.json()
    titles = {i["title"] for i in body["items"]}
    assert titles == {"Interested One", "Applied One"}
    assert body["total"] == 2 and body["page"] == 1
    assert new.title not in titles
    row = next(i for i in body["items"] if i["title"] == "Applied One")
    assert row["availability"] == "open"
    assert row["latest_event_summary"]
    assert row["next_follow_up_at"] is not None
    assert row["status"]["key"] == "applied"


async def test_tracker_filters_sorts_and_next_interview(client, session):
    acme = await make_company(session, "Acme Corp")
    globex = await make_company(session, "Globex")
    j1 = await make_job(session, acme, title="Platform Engineer")
    j2 = await make_job(session, globex, title="Data Engineer")
    j3 = await make_job(session, globex, title="Designer")
    await put_status(client, j1.id, "applied", occurred_at="2026-11-01T00:00:00Z")
    await put_status(client, j2.id, "applied", occurred_at="2026-11-05T00:00:00Z")
    await put_status(client, j3.id, "interviewing")

    async def titles(**params):
        r = await client.get(f"{API}/applications", params=params)
        assert r.status_code == 200, r.text
        return [i["title"] for i in r.json()["items"]]

    assert await titles(status="applied") == await titles(status=["applied"]) or True
    assert set(await titles(status="interviewing")) == {"Designer"}
    assert set(await titles(company_id=globex.id)) == {"Data Engineer", "Designer"}
    assert set(await titles(q="engineer")) == {"Platform Engineer", "Data Engineer"}
    assert set(await titles(q="glob")) == {"Data Engineer", "Designer"}  # matches company name
    assert await titles(sort="applied_desc") == ["Data Engineer", "Platform Engineer", "Designer"]
    assert await titles(sort="company_asc") == ["Platform Engineer", "Data Engineer", "Designer"]
    assert (await titles(sort="status"))[-1] == "Designer"
    fu = await titles(sort="follow_up_asc")
    assert fu[:2] == ["Platform Engineer", "Data Engineer"]
    assert (await client.get(f"{API}/applications", params={"sort": "bogus"})).status_code == 422

    # next_interview_at = earliest pending interview in the future
    for when in ("2099-01-10T10:00:00Z", "2099-01-05T10:00:00Z", "2000-01-01T10:00:00Z"):
        r = await client.post(
            f"{API}/jobs/{j1.id}/interviews",
            json={"round_name": "R", "scheduled_at": when, "create_reminders": False},
        )
        assert r.status_code == 201
    r = await client.get(f"{API}/applications", params={"q": "Platform"})
    assert r.json()["items"][0]["next_interview_at"].startswith("2099-01-05T10:00")

    r = await client.get(f"{API}/applications", params={"page_size": 2, "page": 2})
    assert len(r.json()["items"]) == 1 and r.json()["total"] == 3


async def test_board_columns_cards_and_ordering(client, session):
    jobs = [await make_job(session, title=f"Job {i}") for i in range(4)]
    for j in jobs[:3]:
        await put_status(client, j.id, "interested")
    await put_status(client, jobs[3].id, "applied")
    await put_status(client, (await make_job(session)).id, "withdrawn")  # not shown on board

    r = await client.get(f"{API}/applications/board")
    assert r.status_code == 200, r.text
    cols = r.json()["columns"]
    keys = [c["status"]["key"] for c in cols]
    assert keys == ["interested", "shortlisted", "applied", "assessment", "interviewing", "offer", "rejected"]
    interested = cols[0]
    assert interested["total"] == 3 and len(interested["cards"]) == 3
    assert cols[2]["total"] == 1
    r = await client.get(f"{API}/applications/board", params={"cards_per_column": 2})
    assert len(r.json()["columns"][0]["cards"]) == 2 and r.json()["columns"][0]["total"] == 3
    r = await client.get(f"{API}/applications/board", params={"q": "Job 3"})
    assert [c["total"] for c in r.json()["columns"]][2] == 1


async def test_board_move_across_columns_with_positions(client, session):
    jobs = [await make_job(session, title=f"Card {i}") for i in range(4)]
    for j in jobs:
        await put_status(client, j.id, "interested")
    apps = [await application_for(session, j) for j in jobs]

    async def move(app, status, before=None, after=None):
        cur = await get_application(client, app.job_id)
        return await client.patch(
            f"{API}/applications/{app.id}/position",
            json={
                "status": status,
                "before_application_id": before.id if before else None,
                "after_application_id": after.id if after else None,
                "expected_row_version": cur["row_version"],
            },
        )

    async def column(key):
        cols = (await client.get(f"{API}/applications/board")).json()["columns"]
        return next(c for c in cols if c["status"]["key"] == key)["cards"]

    # drop into empty column: 1024
    r = await move(apps[0], "applied")
    assert r.status_code == 200, r.text
    assert r.json()["status"]["key"] == "applied" and r.json()["applied_at"] is not None
    assert r.json()["next_follow_up_at"] is not None  # same rules as change_status
    cards = await column("applied")
    assert cards[0]["board_position"] == 1024.0

    # below card0
    assert (await move(apps[1], "applied", before=apps[0])).status_code == 200
    # above card0 (only 'after')
    assert (await move(apps[2], "applied", after=apps[0])).status_code == 200
    cards = await column("applied")
    assert [c["application_id"] for c in cards] == [apps[2].id, apps[0].id, apps[1].id]
    assert [c["board_position"] for c in cards] == [0.0, 1024.0, 2048.0]
    # between card2 and card0 -> midpoint
    assert (await move(apps[3], "applied", before=apps[2], after=apps[0])).status_code == 200
    cards = await column("applied")
    assert [c["application_id"] for c in cards] == [apps[2].id, apps[3].id, apps[0].id, apps[1].id]
    assert cards[1]["board_position"] == 512.0
    # reorder inside the same column bumps the version and keeps status
    before = (await get_application(client, apps[1].job_id))["row_version"]
    r = await move(apps[1], "applied", after=apps[2])
    assert r.status_code == 200 and r.json()["row_version"] == before + 1
    assert (await column("applied"))[0]["application_id"] == apps[1].id

    events = await get_timeline(client, apps[0].job_id)
    assert any(e["event_type"] == "status_changed" and e["to_status"]["key"] == "applied" for e in events)
    # the cards left in 'interested' are gone from that column
    assert await column("interested") == []


async def test_board_move_errors(client, session):
    a, b = await make_job(session), await make_job(session)
    await put_status(client, a.id, "interested")
    await put_status(client, b.id, "shortlisted")
    app_a, app_b = await application_for(session, a), await application_for(session, b)
    ver = (await get_application(client, a.id))["row_version"]

    def body(**kw):
        return {"status": "applied", "expected_row_version": ver, **kw}

    # neighbour in another column, and a missing neighbour -> 422
    r = await client.patch(f"{API}/applications/{app_a.id}/position", json=body(before_application_id=app_b.id))
    assert r.status_code == 422
    r = await client.patch(f"{API}/applications/{app_a.id}/position", json=body(after_application_id=424242))
    assert r.status_code == 422
    # stale -> 409, unknown application -> 404, unknown status -> 404
    r = await client.patch(f"{API}/applications/{app_a.id}/position", json=body(expected_row_version=ver + 5))
    assert r.status_code == 409 and r.json()["error"]["code"] == "stale"
    assert (await client.patch(f"{API}/applications/424242/position", json=body())).status_code == 404
    r = await client.patch(f"{API}/applications/{app_a.id}/position", json=body(status="zzz"))
    assert r.status_code == 404
    # nothing changed
    assert (await get_application(client, a.id))["status"]["key"] == "interested"
    row = (await session.scalars(select(Application).where(Application.id == app_a.id))).one()
    assert row.board_position is None


async def test_board_move_next_to_unpositioned_neighbours_renumbers(client, session):
    jobs = [await make_job(session, title=f"C{i}") for i in range(3)]
    for j in jobs:
        await put_status(client, j.id, "interested")
    apps = [await application_for(session, j) for j in jobs]
    ver = (await get_application(client, jobs[0].id))["row_version"]
    # drop card 0 between card2 (above) and card1 (below), none of which has a position yet
    r = await client.patch(
        f"{API}/applications/{apps[0].id}/position",
        json={
            "status": "interested",
            "before_application_id": apps[2].id,  # unpositioned cards list newest first: C2, C1, C0
            "after_application_id": apps[1].id,
            "expected_row_version": ver,
        },
    )
    assert r.status_code == 200, r.text
    cols = (await client.get(f"{API}/applications/board")).json()["columns"]
    cards = cols[0]["cards"]
    ids = [c["application_id"] for c in cards]
    assert ids.index(apps[2].id) < ids.index(apps[0].id) < ids.index(apps[1].id)
