"""Shared arrange helpers for the insights tests."""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.applications.lifecycle import record_event
from app.models import Job
from app.models.enums import EventType
from tests.factories import application_for, status_id


async def move(
    session: AsyncSession,
    job: Job,
    key: str,
    at: datetime,
    *,
    applied: bool = False,
    reviewed: bool = True,
    set_current: bool = True,
) -> None:
    """Record a status_changed event (explicit occurred_at) and optionally make it the current status."""
    app = await application_for(session, job)
    to_id = await status_id(session, key)
    await record_event(
        session,
        app.id,
        EventType.status_changed,
        f"-> {key}",
        occurred_at=at,
        from_status_id=app.status_id,
        to_status_id=to_id,
    )
    if set_current:
        app.status_id = to_id
        app.status_changed_at = at
    if reviewed and app.reviewed_at is None:
        app.reviewed_at = at
    if applied and app.applied_at is None:
        app.applied_at = at
    await session.flush()
