from datetime import datetime
from typing import Any, Literal

from pydantic import Field, HttpUrl

from app.core.schema import APIModel
from app.jobs.schemas import CompanyRef, StatusRef
from app.models.enums import Actor, Availability, EventType


class StatusChangeRequest(APIModel):
    """PUT /jobs/{job_id}/status. Idempotent: setting the current status again is a no-op (200)."""

    status: str  # status key
    occurred_at: datetime | None = None  # back-date, e.g. "applied on Nov 14"; default now
    reason: str | None = None  # stored as outcome_reason for closed_* categories
    note: str | None = None  # optional note added in the same transaction
    expected_row_version: int | None = None  # optimistic concurrency; 409 when stale


class ApplicationOut(APIModel):
    id: int
    job_id: int
    status: StatusRef
    status_changed_at: datetime
    reviewed_at: datetime | None
    interested_at: datetime | None
    shortlisted_at: datetime | None
    applied_at: datetime | None
    closed_at: datetime | None
    priority: int | None
    starred: bool
    application_url: str | None
    resume_version_id: int | None
    resume_version_label: str | None = None
    referral: str | None
    next_action: str | None
    next_follow_up_at: datetime | None
    outcome_reason: str | None
    offer_details: dict[str, Any] | None
    row_version: int
    updated_at: datetime


class ApplicationUpdate(APIModel):
    """PATCH /jobs/{job_id}/application. Requires expected_row_version. Milestone dates are editable.
    Each changed field is recorded as a 'field_changed' timeline event."""

    expected_row_version: int
    priority: int | None = Field(default=None, ge=1, le=3)
    starred: bool | None = None
    application_url: HttpUrl | None = None
    resume_version_id: int | None = None
    referral: str | None = None
    next_action: str | None = None
    outcome_reason: str | None = None
    offer_details: dict[str, Any] | None = None
    interested_at: datetime | None = None
    shortlisted_at: datetime | None = None
    applied_at: datetime | None = None


class StatusChangeResult(APIModel):
    application: ApplicationOut
    changed: bool  # False when the status was already the requested one
    created_reminder_id: int | None = None  # auto follow-up after Applied


class BulkStatusRequest(APIModel):
    job_ids: list[int] = Field(min_length=1, max_length=500)
    status: str
    reason: str | None = None


class BulkStatusResult(APIModel):
    updated: int
    unchanged: int
    job_ids: list[int]


class TrackerRow(APIModel):
    """GET /applications — the tracker table (jobs whose status category is beyond 'inbox')."""

    application_id: int
    job_id: int
    company: CompanyRef
    title: str
    status: StatusRef
    availability: Availability
    applied_at: datetime | None
    next_action: str | None
    next_follow_up_at: datetime | None
    next_interview_at: datetime | None
    latest_event_at: datetime
    latest_event_summary: str
    starred: bool
    priority: int | None
    row_version: int


class BoardCard(APIModel):
    application_id: int
    job_id: int
    company: CompanyRef
    title: str
    location_text: str | None
    availability: Availability
    applied_at: datetime | None
    next_follow_up_at: datetime | None
    next_interview_at: datetime | None
    starred: bool
    priority: int | None
    board_position: float | None
    row_version: int


class BoardColumn(APIModel):
    status: StatusRef
    cards: list[BoardCard]
    total: int


class Board(APIModel):
    columns: list[BoardColumn]


class BoardMoveRequest(APIModel):
    """PATCH /applications/{id}/position — drag on the Kanban. Changes status (if column differs)
    and position atomically. Position is between the neighbours' positions (fractional index)."""

    status: str
    before_application_id: int | None = None  # card that will be above
    after_application_id: int | None = None  # card that will be below
    expected_row_version: int


class TimelineEventOut(APIModel):
    id: int
    event_type: EventType
    actor: Actor
    summary: str
    from_status: StatusRef | None
    to_status: StatusRef | None
    payload: dict[str, Any]
    occurred_at: datetime
    created_at: datetime


class ManualTimelineEntry(APIModel):
    summary: str = Field(min_length=1, max_length=500)
    occurred_at: datetime | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class NoteCreate(APIModel):
    body: str = Field(min_length=1)
    pinned: bool = False


class NoteUpdate(APIModel):
    body: str | None = Field(default=None, min_length=1)
    pinned: bool | None = None


class NoteOut(APIModel):
    id: int
    application_id: int
    body: str
    pinned: bool
    created_at: datetime
    updated_at: datetime


ContactRole = Literal["referrer", "recruiter", "hiring_manager", "interviewer", "other"]


class ContactCreate(APIModel):
    role: ContactRole
    name: str = Field(min_length=1)
    title: str | None = None
    email: str | None = None
    phone: str | None = None
    profile_url: HttpUrl | None = None
    notes: str | None = None


class ContactUpdate(APIModel):
    role: ContactRole | None = None
    name: str | None = Field(default=None, min_length=1)
    title: str | None = None
    email: str | None = None
    phone: str | None = None
    profile_url: HttpUrl | None = None
    notes: str | None = None


class ContactOut(APIModel):
    id: int
    application_id: int
    role: str
    name: str
    title: str | None
    email: str | None
    phone: str | None
    profile_url: str | None
    notes: str | None
    created_at: datetime


InterviewKind = Literal["assessment", "screen", "technical", "system_design", "behavioral", "hr", "onsite", "other"]
InterviewOutcome = Literal["pending", "passed", "failed", "cancelled", "no_show"]


class InterviewCreate(APIModel):
    round_name: str = Field(min_length=1)
    kind: InterviewKind = "technical"
    scheduled_at: datetime | None = None
    duration_min: int | None = Field(default=None, ge=1, le=1440)
    location: str | None = None
    notes: str | None = None
    create_reminders: bool = True  # interview reminder + prep reminder per ReminderDefaults


class InterviewUpdate(APIModel):
    round_name: str | None = Field(default=None, min_length=1)
    kind: InterviewKind | None = None
    scheduled_at: datetime | None = None
    duration_min: int | None = Field(default=None, ge=1, le=1440)
    location: str | None = None
    outcome: InterviewOutcome | None = None
    notes: str | None = None


class InterviewOut(APIModel):
    id: int
    application_id: int
    round_name: str
    kind: str
    scheduled_at: datetime | None
    duration_min: int | None
    location: str | None
    outcome: str
    notes: str | None
    created_at: datetime
    updated_at: datetime
