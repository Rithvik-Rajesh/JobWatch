"""Reminders API. Owner: applications agent."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from app.core.auth import CurrentUser
from app.core.db import SessionDep
from app.core.errors import todo
from app.models.enums import ReminderKind, ReminderState
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
    raise todo()


@router.post("", response_model=ReminderOut, status_code=201)
async def create_reminder(body: ReminderCreate, session: SessionDep, user: CurrentUser):
    raise todo()


@router.patch("/{reminder_id}", response_model=ReminderOut)
async def update_reminder(reminder_id: int, body: ReminderUpdate, session: SessionDep, user: CurrentUser):
    raise todo()


@router.post("/{reminder_id}/complete", response_model=ReminderOut)
async def complete_reminder(reminder_id: int, session: SessionDep, user: CurrentUser):
    raise todo()


@router.post("/{reminder_id}/dismiss", response_model=ReminderOut)
async def dismiss_reminder(reminder_id: int, session: SessionDep, user: CurrentUser):
    raise todo()


@router.post("/{reminder_id}/snooze", response_model=ReminderOut)
async def snooze_reminder(reminder_id: int, body: SnoozeRequest, session: SessionDep, user: CurrentUser):
    raise todo()


@router.delete("/{reminder_id}", status_code=204)
async def delete_reminder(reminder_id: int, session: SessionDep, user: CurrentUser):
    raise todo()
