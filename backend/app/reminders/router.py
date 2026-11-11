"""Reminders API. Owner: applications agent."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from app.core.auth import CurrentUser
from app.core.db import SessionDep
from app.models.enums import ReminderKind, ReminderState
from app.reminders import service
from app.reminders.schemas import ReminderCreate, ReminderOut, ReminderUpdate, SnoozeRequest

router = APIRouter(prefix="/reminders", tags=["reminders"])


@router.get("", response_model=list[ReminderOut])
async def list_reminders(
    session: SessionDep,
    user: CurrentUser,
    due_from: datetime | None = None,
    due_to: datetime | None = None,
    state: Annotated[list[ReminderState], Query()] = [ReminderState.pending],
    kind: Annotated[list[ReminderKind], Query()] = [],
    job_id: int | None = None,
):
    return await service.list_reminders(
        session, due_from=due_from, due_to=due_to, states=state, kinds=kind, job_id=job_id
    )


@router.post("", response_model=ReminderOut, status_code=201)
async def create_reminder(body: ReminderCreate, session: SessionDep, user: CurrentUser):
    return await service.create_reminder(session, body)


@router.patch("/{reminder_id}", response_model=ReminderOut)
async def update_reminder(reminder_id: int, body: ReminderUpdate, session: SessionDep, user: CurrentUser):
    return await service.update_reminder(session, reminder_id, body)


@router.post("/{reminder_id}/complete", response_model=ReminderOut)
async def complete_reminder(reminder_id: int, session: SessionDep, user: CurrentUser):
    return await service.complete_reminder(session, reminder_id)


@router.post("/{reminder_id}/dismiss", response_model=ReminderOut)
async def dismiss_reminder(reminder_id: int, session: SessionDep, user: CurrentUser):
    return await service.dismiss_reminder(session, reminder_id)


@router.post("/{reminder_id}/snooze", response_model=ReminderOut)
async def snooze_reminder(reminder_id: int, body: SnoozeRequest, session: SessionDep, user: CurrentUser):
    return await service.snooze_reminder(session, reminder_id, body)


@router.delete("/{reminder_id}", status_code=204)
async def delete_reminder(reminder_id: int, session: SessionDep, user: CurrentUser):
    await service.delete_reminder(session, reminder_id)
