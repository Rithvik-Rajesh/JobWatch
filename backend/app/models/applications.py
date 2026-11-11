"""The user's pipeline record for a job and everything hanging off it."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, IdMixin, TimestampMixin
from app.models.enums import Actor, EventType, ReminderKind, ReminderState
from app.models.types import pg_enum


class Application(IdMixin, TimestampMixin, Base):
    """Exactly one per job, created in the same transaction as the job (status 'new').

    `status_id` is user-owned: the discovery pipeline never writes it.
    """

    __tablename__ = "applications"

    job_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("jobs.id"), unique=True)
    status_id: Mapped[int] = mapped_column(SmallInteger, ForeignKey("statuses.id"))
    status_changed_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    # Inbox membership: unreviewed = reviewed_at IS NULL. Set by any status change or "mark reviewed".
    reviewed_at: Mapped[datetime | None]
    interested_at: Mapped[datetime | None]
    shortlisted_at: Mapped[datetime | None]
    applied_at: Mapped[datetime | None]
    closed_at: Mapped[datetime | None]
    priority: Mapped[int | None] = mapped_column(SmallInteger)
    starred: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    application_url: Mapped[str | None] = mapped_column(Text)
    resume_version_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("resume_versions.id"))
    referral: Mapped[str | None] = mapped_column(Text)
    next_action: Mapped[str | None] = mapped_column(Text)
    next_follow_up_at: Mapped[datetime | None]  # denormalized: earliest pending follow_up reminder
    outcome_reason: Mapped[str | None] = mapped_column(Text)
    offer_details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    board_position: Mapped[float | None] = mapped_column(Float)
    row_version: Mapped[int] = mapped_column(Integer, server_default=text("1"))

    __table_args__ = (
        CheckConstraint("priority is null or priority between 1 and 3", name="priority_range"),
        Index("ix_applications_status_position", "status_id", "board_position"),
        Index("ix_applications_unreviewed", "job_id", postgresql_where=text("reviewed_at is null")),
        Index("ix_applications_follow_up", "next_follow_up_at", postgresql_where=text("next_follow_up_at is not null")),
        Index("ix_applications_applied_at", "applied_at", postgresql_where=text("applied_at is not null")),
    )


class ApplicationEvent(IdMixin, Base):
    """Append-only activity timeline. Rows are never updated or deleted by application code."""

    __tablename__ = "application_events"

    application_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("applications.id", ondelete="CASCADE"))
    event_type: Mapped[EventType] = mapped_column(pg_enum(EventType, "event_type"))
    from_status_id: Mapped[int | None] = mapped_column(SmallInteger, ForeignKey("statuses.id"))
    to_status_id: Mapped[int | None] = mapped_column(SmallInteger, ForeignKey("statuses.id"))
    actor: Mapped[Actor] = mapped_column(pg_enum(Actor, "actor"), server_default=Actor.user.value)
    summary: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    occurred_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))

    __table_args__ = (
        Index("ix_application_events_timeline", "application_id", text("occurred_at desc")),
        Index(
            "ix_application_events_status",
            "to_status_id",
            "occurred_at",
            postgresql_where=text("event_type = 'status_changed'"),
        ),
    )


class Note(IdMixin, TimestampMixin, Base):
    __tablename__ = "notes"

    application_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    body: Mapped[str] = mapped_column(Text)
    pinned: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))


class Contact(IdMixin, Base):
    __tablename__ = "contacts"

    application_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    profile_url: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("role in ('referrer','recruiter','hiring_manager','interviewer','other')", name="role_valid"),
    )


class Interview(IdMixin, TimestampMixin, Base):
    __tablename__ = "interviews"

    application_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    round_name: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, server_default="technical")
    scheduled_at: Mapped[datetime | None]
    duration_min: Mapped[int | None] = mapped_column(Integer)
    location: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(Text, server_default="pending")
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "kind in ('assessment','screen','technical','system_design','behavioral','hr','onsite','other')",
            name="kind_valid",
        ),
        CheckConstraint("outcome in ('pending','passed','failed','cancelled','no_show')", name="outcome_valid"),
        Index("ix_interviews_upcoming", "scheduled_at", postgresql_where=text("outcome = 'pending'")),
    )


class Reminder(IdMixin, TimestampMixin, Base):
    __tablename__ = "reminders"

    application_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    interview_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("interviews.id", ondelete="CASCADE"))
    kind: Mapped[ReminderKind] = mapped_column(pg_enum(ReminderKind, "reminder_kind"))
    title: Mapped[str] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    due_at: Mapped[datetime]
    notify_at: Mapped[datetime | None]
    state: Mapped[ReminderState] = mapped_column(
        pg_enum(ReminderState, "reminder_state"), server_default=ReminderState.pending.value
    )
    completed_at: Mapped[datetime | None]

    __table_args__ = (
        Index("ix_reminders_due_pending", "due_at", postgresql_where=text("state = 'pending'")),
        Index(
            "ix_reminders_notify_pending",
            "notify_at",
            postgresql_where=text("state = 'pending' and notify_at is not null"),
        ),
    )
