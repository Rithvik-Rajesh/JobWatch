"""Kanban board: columns, cards and drag/drop moves (fractional indexing)."""

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.applications.schemas import Board, BoardCard, BoardColumn, BoardMoveRequest
from app.applications.service import (
    _application_for_job,
    _check_version,
    change_status,
    next_interview_at_expr,
    search_conditions,
)
from app.core.errors import Conflict, NotFound, ValidationFailed
from app.jobs.schemas import CompanyRef, StatusRef
from app.models import Application, Company, Job, Status
from app.reminders.service import as_utc, utcnow

POSITION_STEP = 1024.0


def compute_position(before: float | None, after: float | None) -> float:
    """Position for a card dropped below `before` and above `after` (ascending order = top to bottom)."""
    if before is not None and after is not None:
        if before >= after:
            raise ValidationFailed("Neighbouring cards are not in board order", code="invalid_neighbours")
        return (before + after) / 2
    if before is not None:
        return before + POSITION_STEP
    if after is not None:
        return after - POSITION_STEP
    return POSITION_STEP


async def get_board(
    session: AsyncSession,
    *,
    company_ids: Sequence[int] = (),
    q: str | None = None,
    cards_per_column: int = 100,
    now: datetime | None = None,
) -> Board:
    now = as_utc(now) or utcnow()
    statuses = (
        await session.scalars(
            select(Status)
            .where(Status.show_on_board.is_(True), Status.archived_at.is_(None))
            .order_by(Status.sort_order, Status.id)
        )
    ).all()
    status_ids = [s.id for s in statuses]
    conds = [
        Application.status_id.in_(status_ids),
        Job.merged_into_job_id.is_(None),
        *search_conditions(company_ids, q),
    ]

    totals = dict(
        (
            await session.execute(
                select(Application.status_id, func.count())
                .join(Job, Job.id == Application.job_id)
                .join(Company, Company.id == Job.company_id)
                .where(*conds)
                .group_by(Application.status_id)
            )
        ).all()
    )

    ranked = (
        select(
            Application.id.label("aid"),
            func.row_number()
            .over(
                partition_by=Application.status_id,
                order_by=(
                    Application.board_position.asc().nulls_last(),
                    Application.status_changed_at.desc(),
                    Application.id.desc(),
                ),
            )
            .label("rn"),
        )
        .join(Job, Job.id == Application.job_id)
        .join(Company, Company.id == Job.company_id)
        .where(*conds)
        .subquery()
    )
    rows = (
        await session.execute(
            select(Application, Job, Company, next_interview_at_expr(now))
            .join(ranked, ranked.c.aid == Application.id)
            .join(Job, Job.id == Application.job_id)
            .join(Company, Company.id == Job.company_id)
            .where(ranked.c.rn <= cards_per_column)
            .order_by(Application.status_id, ranked.c.rn)
            .execution_options(populate_existing=True)
        )
    ).all()

    cards: dict[int, list[BoardCard]] = {sid: [] for sid in status_ids}
    for app, job, company, next_interview in rows:
        cards[app.status_id].append(
            BoardCard(
                application_id=app.id,
                job_id=job.id,
                company=CompanyRef.model_validate(company),
                title=job.title,
                location_text=job.location_text,
                availability=job.availability,
                applied_at=app.applied_at,
                next_follow_up_at=app.next_follow_up_at,
                next_interview_at=next_interview,
                starred=app.starred,
                priority=app.priority,
                board_position=app.board_position,
                row_version=app.row_version,
            )
        )
    return Board(
        columns=[
            BoardColumn(status=StatusRef.model_validate(s), cards=cards[s.id], total=totals.get(s.id, 0))
            for s in statuses
        ]
    )


async def _column_in_order(session: AsyncSession, status_id: int) -> list[Application]:
    return list(
        (
            await session.scalars(
                select(Application)
                .join(Job, Job.id == Application.job_id)
                .where(Application.status_id == status_id, Job.merged_into_job_id.is_(None))
                .order_by(
                    Application.board_position.asc().nulls_last(),
                    Application.status_changed_at.desc(),
                    Application.id.desc(),
                )
                .execution_options(populate_existing=True)
            )
        ).all()
    )


async def _neighbour(session: AsyncSession, application_id: int | None, status_id: int, moving_id: int):
    if application_id is None:
        return None
    if application_id == moving_id:
        raise ValidationFailed("A card cannot be its own neighbour", code="invalid_neighbours")
    app = await session.scalar(
        select(Application)
        .join(Job, Job.id == Application.job_id)
        .where(
            Application.id == application_id,
            Application.status_id == status_id,
            Job.merged_into_job_id.is_(None),
        )
        .execution_options(populate_existing=True)
    )
    if app is None:
        raise ValidationFailed(
            f"Neighbour application {application_id} is missing or not in the target column",
            code="invalid_neighbours",
        )
    return app


async def move_card(
    session: AsyncSession, application_id: int, data: BoardMoveRequest, *, now: datetime | None = None
) -> Application:
    now = as_utc(now) or utcnow()
    app = await session.scalar(
        select(Application)
        .where(Application.id == application_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if app is None:
        raise NotFound(f"Application {application_id} not found", code="application_not_found")
    _check_version(app, data.expected_row_version)
    job = await session.get(Job, app.job_id)
    assert job is not None
    if job.merged_into_job_id is not None:
        raise Conflict("Job was merged into another job", code="merged")

    target = await session.scalar(select(Status).where(Status.key == data.status))
    if target is None:
        raise NotFound(f"Unknown status '{data.status}'", code="unknown_status")
    if target.archived_at is not None:
        raise ValidationFailed(f"Status '{data.status}' is archived", code="status_archived")

    before = await _neighbour(session, data.before_application_id, target.id, app.id)
    after = await _neighbour(session, data.after_application_id, target.id, app.id)

    status_changed = app.status_id != target.id
    if status_changed:
        await change_status(
            session,
            job.id,
            target.key,
            expected_row_version=data.expected_row_version,
            now=now,
        )
        app = await _application_for_job(session, job.id, lock=True)
    elif app.reviewed_at is None:
        app.reviewed_at = now

    # Neighbours without a stored position (ordered by recency) cannot be indexed: renumber the column.
    if (before is not None and before.board_position is None) or (after is not None and after.board_position is None):
        column = [c for c in await _column_in_order(session, target.id) if c.id != app.id]
        for i, card in enumerate(column, start=1):
            card.board_position = i * POSITION_STEP
        await session.flush()

    app.board_position = compute_position(
        before.board_position if before is not None else None,
        after.board_position if after is not None else None,
    )
    if not status_changed:
        app.row_version += 1
    await session.flush()
    return app
