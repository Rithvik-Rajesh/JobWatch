"""Application lifecycle API. Owner: applications agent. Paths/models are the frozen contract."""

from typing import Annotated

from fastapi import APIRouter, Query

from app.applications import board as board_service
from app.applications import service
from app.applications.schemas import (
    ApplicationOut,
    ApplicationUpdate,
    Board,
    BoardMoveRequest,
    BulkStatusRequest,
    BulkStatusResult,
    ContactCreate,
    ContactOut,
    ContactUpdate,
    InterviewCreate,
    InterviewOut,
    InterviewUpdate,
    ManualTimelineEntry,
    NoteCreate,
    NoteOut,
    NoteUpdate,
    StatusChangeRequest,
    StatusChangeResult,
    TimelineEventOut,
    TrackerRow,
)
from app.core.auth import CurrentUser
from app.core.db import SessionDep
from app.core.pagination import Page, PageDep

router = APIRouter(tags=["applications"])


@router.put("/jobs/{job_id}/status", response_model=StatusChangeResult)
async def set_status(job_id: int, body: StatusChangeRequest, session: SessionDep, user: CurrentUser):
    outcome = await service.change_status(
        session,
        job_id,
        body.status,
        occurred_at=body.occurred_at,
        reason=body.reason,
        note=body.note,
        expected_row_version=body.expected_row_version,
    )
    return StatusChangeResult(
        application=await service.application_out(session, outcome.application),
        changed=outcome.changed,
        created_reminder_id=outcome.created_reminder_id,
    )


@router.get("/jobs/{job_id}/application", response_model=ApplicationOut)
async def get_application(job_id: int, session: SessionDep, user: CurrentUser):
    return await service.get_application(session, job_id)


@router.patch("/jobs/{job_id}/application", response_model=ApplicationOut)
async def update_application(job_id: int, body: ApplicationUpdate, session: SessionDep, user: CurrentUser):
    return await service.update_application(session, job_id, body)


@router.post("/applications/bulk-status", response_model=BulkStatusResult)
async def bulk_status(body: BulkStatusRequest, session: SessionDep, user: CurrentUser):
    return await service.bulk_change_status(session, body.job_ids, body.status, reason=body.reason)


@router.get("/applications", response_model=Page[TrackerRow])
async def tracker(
    session: SessionDep,
    user: CurrentUser,
    paging: PageDep,
    status: Annotated[list[str], Query()] = [],
    company_id: Annotated[list[int], Query()] = [],
    q: str | None = None,
    sort: str = "latest_event_desc",
):
    """Jobs whose status category is not 'inbox' or 'archived' (archived via status=not_interested)."""
    return await service.list_tracker(session, paging, statuses=status, company_ids=company_id, q=q, sort=sort)


@router.get("/applications/board", response_model=Board)
async def board(
    session: SessionDep,
    user: CurrentUser,
    company_id: Annotated[list[int], Query()] = [],
    q: str | None = None,
    cards_per_column: int = Query(default=100, ge=1, le=500),
):
    return await board_service.get_board(session, company_ids=company_id, q=q, cards_per_column=cards_per_column)


@router.patch("/applications/{application_id}/position", response_model=ApplicationOut)
async def move_card(application_id: int, body: BoardMoveRequest, session: SessionDep, user: CurrentUser):
    app = await board_service.move_card(session, application_id, body)
    return await service.application_out(session, app)


@router.get("/jobs/{job_id}/timeline", response_model=list[TimelineEventOut])
async def timeline(job_id: int, session: SessionDep, user: CurrentUser):
    """Newest first. Includes events of jobs merged into this one."""
    return await service.list_timeline(session, job_id)


@router.post("/jobs/{job_id}/timeline", response_model=TimelineEventOut, status_code=201)
async def add_timeline_entry(job_id: int, body: ManualTimelineEntry, session: SessionDep, user: CurrentUser):
    return await service.add_timeline_entry(session, job_id, body)


@router.get("/jobs/{job_id}/notes", response_model=list[NoteOut])
async def list_notes(job_id: int, session: SessionDep, user: CurrentUser):
    return await service.list_notes(session, job_id)


@router.post("/jobs/{job_id}/notes", response_model=NoteOut, status_code=201)
async def create_note(job_id: int, body: NoteCreate, session: SessionDep, user: CurrentUser):
    return await service.create_note(session, job_id, body)


@router.patch("/notes/{note_id}", response_model=NoteOut)
async def update_note(note_id: int, body: NoteUpdate, session: SessionDep, user: CurrentUser):
    return await service.update_note(session, note_id, body)


@router.delete("/notes/{note_id}", status_code=204)
async def delete_note(note_id: int, session: SessionDep, user: CurrentUser):
    await service.delete_note(session, note_id)


@router.get("/jobs/{job_id}/contacts", response_model=list[ContactOut])
async def list_contacts(job_id: int, session: SessionDep, user: CurrentUser):
    return await service.list_contacts(session, job_id)


@router.post("/jobs/{job_id}/contacts", response_model=ContactOut, status_code=201)
async def create_contact(job_id: int, body: ContactCreate, session: SessionDep, user: CurrentUser):
    return await service.create_contact(session, job_id, body)


@router.patch("/contacts/{contact_id}", response_model=ContactOut)
async def update_contact(contact_id: int, body: ContactUpdate, session: SessionDep, user: CurrentUser):
    return await service.update_contact(session, contact_id, body)


@router.delete("/contacts/{contact_id}", status_code=204)
async def delete_contact(contact_id: int, session: SessionDep, user: CurrentUser):
    await service.delete_contact(session, contact_id)


@router.get("/jobs/{job_id}/interviews", response_model=list[InterviewOut])
async def list_interviews(job_id: int, session: SessionDep, user: CurrentUser):
    return await service.list_interviews(session, job_id)


@router.post("/jobs/{job_id}/interviews", response_model=InterviewOut, status_code=201)
async def create_interview(job_id: int, body: InterviewCreate, session: SessionDep, user: CurrentUser):
    return await service.create_interview(session, job_id, body)


@router.patch("/interviews/{interview_id}", response_model=InterviewOut)
async def update_interview(interview_id: int, body: InterviewUpdate, session: SessionDep, user: CurrentUser):
    return await service.update_interview(session, interview_id, body)


@router.delete("/interviews/{interview_id}", status_code=204)
async def delete_interview(interview_id: int, session: SessionDep, user: CurrentUser):
    await service.delete_interview(session, interview_id)
