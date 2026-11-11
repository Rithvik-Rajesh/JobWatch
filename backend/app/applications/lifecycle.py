"""Application lifecycle primitives — CENTRAL CONTRACT used by ingest, manual entry, merge and the
applications service. Signatures here are frozen for Phase 2 agents; implementations may be extended.

Invariants:
  * Exactly one Application per Job, created with the job (status 'new', unreviewed).
  * The timeline (application_events) is append-only and is the history of record.
  * System code (discovery/ingest) may add events but NEVER changes Application.status_id.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound
from app.models import Application, ApplicationEvent, Status
from app.models.enums import Actor, EventType

INITIAL_STATUS_KEY = "new"


async def status_by_key(session: AsyncSession, key: str) -> Status:
    status = await session.scalar(select(Status).where(Status.key == key))
    if status is None:
        raise NotFound(f"Unknown status '{key}'", code="unknown_status")
    return status


async def create_tracking_record(
    session: AsyncSession,
    job_id: int,
    *,
    discovered_at: datetime,
    summary: str,
    actor: Actor = Actor.system,
    payload: dict[str, Any] | None = None,
) -> Application:
    """Create the job's Application (status 'new') plus its 'discovered' timeline event."""
    status = await status_by_key(session, INITIAL_STATUS_KEY)
    app = Application(job_id=job_id, status_id=status.id, status_changed_at=discovered_at)
    session.add(app)
    await session.flush()
    await record_event(
        session,
        app.id,
        EventType.discovered,
        summary,
        actor=actor,
        occurred_at=discovered_at,
        to_status_id=status.id,
        payload=payload,
    )
    return app


async def record_event(
    session: AsyncSession,
    application_id: int,
    event_type: EventType,
    summary: str,
    *,
    actor: Actor = Actor.user,
    occurred_at: datetime | None = None,
    from_status_id: int | None = None,
    to_status_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> ApplicationEvent:
    event = ApplicationEvent(
        application_id=application_id,
        event_type=event_type,
        summary=summary,
        actor=actor,
        from_status_id=from_status_id,
        to_status_id=to_status_id,
        payload=payload or {},
    )
    if occurred_at is not None:
        event.occurred_at = occurred_at
    session.add(event)
    await session.flush()
    return event


async def record_job_event(
    session: AsyncSession,
    job_id: int,
    event_type: EventType,
    summary: str,
    *,
    actor: Actor = Actor.system,
    occurred_at: datetime | None = None,
    payload: dict[str, Any] | None = None,
) -> ApplicationEvent:
    """Add a timeline event addressed by job id (used by ingest for job_updated/closed/reopened)."""
    app_id = await session.scalar(select(Application.id).where(Application.job_id == job_id))
    if app_id is None:
        raise NotFound(f"Job {job_id} has no application record")
    return await record_event(
        session, app_id, event_type, summary, actor=actor, occurred_at=occurred_at, payload=payload
    )
