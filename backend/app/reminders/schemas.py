from datetime import datetime
from typing import Literal

from pydantic import Field

from app.core.schema import APIModel
from app.jobs.schemas import CompanyRef
from app.models.enums import ReminderKind, ReminderState


class ReminderCreate(APIModel):
    kind: ReminderKind
    title: str = Field(min_length=1, max_length=200)
    notes: str | None = None
    due_at: datetime
    notify_at: datetime | None = None  # default: due_at - ReminderDefaults.notify_lead_minutes
    notify: bool = True
    job_id: int | None = None  # attach to a job's application; omit for standalone tasks
    interview_id: int | None = None


class ReminderUpdate(APIModel):
    kind: ReminderKind | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    notes: str | None = None
    due_at: datetime | None = None
    notify_at: datetime | None = None
    notify: bool | None = None


class SnoozeRequest(APIModel):
    until: datetime | None = None
    preset: Literal["1h", "3h", "tomorrow", "next_week"] | None = None


class ReminderJobRef(APIModel):
    job_id: int
    application_id: int
    title: str
    company: CompanyRef


class ReminderOut(APIModel):
    id: int
    kind: ReminderKind
    title: str
    notes: str | None
    due_at: datetime
    notify_at: datetime | None
    state: ReminderState
    completed_at: datetime | None
    interview_id: int | None
    job: ReminderJobRef | None
    overdue: bool
    created_at: datetime
