"""Statuses service: configurable application statuses (behaviour keys off `category`)."""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound, ValidationFailed
from app.models import Application, Status
from app.statuses.schemas import StatusCreate, StatusOut, StatusReorder, StatusUpdate


async def _get(session: AsyncSession, status_id: int) -> Status:
    # ids are smallint: out-of-range values are simply "not found" (avoid a DB error)
    status = await session.get(Status, status_id) if 0 < status_id < 32768 else None
    if status is None:
        raise NotFound("Status not found")
    return status


async def _active(session: AsyncSession) -> list[Status]:
    rows = await session.scalars(
        select(Status).where(Status.archived_at.is_(None)).order_by(Status.sort_order, Status.id)
    )
    return list(rows.all())


async def list_statuses(session: AsyncSession, include_archived: bool = False) -> list[StatusOut]:
    q = select(Status).order_by(Status.sort_order, Status.id)
    if not include_archived:
        q = q.where(Status.archived_at.is_(None))
    return [StatusOut.model_validate(s) for s in (await session.scalars(q)).all()]


async def create_status(session: AsyncSession, body: StatusCreate) -> StatusOut:
    if await session.scalar(select(Status.id).where(func.lower(Status.key) == body.key.lower())) is not None:
        raise Conflict(f"Status key '{body.key}' already exists")
    last = await session.scalar(select(func.max(Status.sort_order))) or 0
    status = Status(
        key=body.key,
        label=body.label,
        category=body.category,
        color=body.color,
        show_on_board=body.show_on_board,
        sort_order=last + 1,
        is_system=False,
    )
    session.add(status)
    await session.flush()
    return StatusOut.model_validate(status)


async def update_status(session: AsyncSession, status_id: int, body: StatusUpdate) -> StatusOut:
    status = await _get(session, status_id)
    provided = body.model_fields_set
    if "category" in provided and body.category is not None and body.category != status.category:
        if status.is_system:
            raise Conflict("The category of a system status cannot be changed")
        status.category = body.category
    if body.label is not None:
        status.label = body.label
    if body.color is not None:
        status.color = body.color
    if body.show_on_board is not None:
        status.show_on_board = body.show_on_board
    await session.flush()
    return StatusOut.model_validate(status)


async def reorder_statuses(session: AsyncSession, body: StatusReorder) -> list[StatusOut]:
    active = await _active(session)
    expected = {s.id for s in active}
    ids = body.ordered_ids
    if len(ids) != len(set(ids)) or set(ids) != expected:
        raise ValidationFailed(
            "ordered_ids must contain every non-archived status id exactly once",
            details={"expected": sorted(expected), "received": ids},
        )
    by_id = {s.id: s for s in active}
    for position, sid in enumerate(ids, start=1):
        by_id[sid].sort_order = position
    await session.flush()
    return [StatusOut.model_validate(by_id[sid]) for sid in ids]


async def archive_status(session: AsyncSession, status_id: int, *, now: datetime | None = None) -> StatusOut:
    status = await _get(session, status_id)
    if status.is_system:
        raise Conflict("System statuses cannot be archived")
    if status.archived_at is None:
        in_use = await session.scalar(
            select(func.count()).select_from(Application).where(Application.status_id == status.id)
        )
        if in_use:
            raise Conflict(f"Status is used by {in_use} application(s)", details={"applications": in_use})
        status.archived_at = now or datetime.now(UTC)
        await session.flush()
    return StatusOut.model_validate(status)
