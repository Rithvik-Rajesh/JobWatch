"""Scraper health aggregates (read-only)."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationFailed
from app.core.pagination import PageParams
from app.health.schemas import HealthOverview
from app.models import Company, NotificationOutbox, ScrapeRun
from app.models.enums import HealthStatus, OutboxState, RunStatus

_RATED = (RunStatus.succeeded, RunStatus.failed, RunStatus.partial, RunStatus.quarantined)


def _rate(succeeded: int, rated: int) -> float | None:
    return round(succeeded / rated, 4) if rated else None


async def overview(session: AsyncSession, *, now: datetime | None = None) -> HealthOverview:
    now = now.astimezone(UTC) if now else datetime.now(UTC)
    d1, d7 = now - timedelta(days=1), now - timedelta(days=7)

    c = (
        await session.execute(
            select(
                func.count(Company.id),
                func.count(Company.id).filter(Company.monitoring_enabled),
                func.count(Company.id).filter(Company.health == HealthStatus.healthy),
                func.count(Company.id).filter(Company.health == HealthStatus.degraded),
                func.count(Company.id).filter(Company.health == HealthStatus.failing),
            ).where(Company.archived_at.is_(None))
        )
    ).one()

    in24 = ScrapeRun.queued_at >= d1
    in7 = ScrapeRun.queued_at >= d7
    ok = ScrapeRun.status == RunStatus.succeeded
    rated = ScrapeRun.status.in_(_RATED)
    r = (
        await session.execute(
            select(
                func.count(ScrapeRun.id).filter(in24),
                func.count(ScrapeRun.id).filter(in24, ScrapeRun.status == RunStatus.failed),
                func.count(ScrapeRun.id).filter(in24, ScrapeRun.status == RunStatus.quarantined),
                func.count(ScrapeRun.id).filter(in24, ok),
                func.count(ScrapeRun.id).filter(in24, rated),
                func.count(ScrapeRun.id).filter(in7, ok),
                func.count(ScrapeRun.id).filter(in7, rated),
                func.avg(ScrapeRun.duration_ms).filter(in24, ScrapeRun.finished_at.is_not(None)),
                func.max(func.coalesce(ScrapeRun.finished_at, ScrapeRun.started_at, ScrapeRun.queued_at)).filter(ok),
                func.count(ScrapeRun.id).filter(ScrapeRun.status.in_([RunStatus.queued, RunStatus.running])),
            )
        )
    ).one()

    n = (
        await session.execute(
            select(
                func.count(NotificationOutbox.id).filter(NotificationOutbox.state == OutboxState.pending),
                func.count(NotificationOutbox.id).filter(NotificationOutbox.state == OutboxState.failed),
            )
        )
    ).one()

    return HealthOverview(
        companies_total=c[0],
        companies_active=c[1],
        companies_healthy=c[2],
        companies_degraded=c[3],
        companies_failing=c[4],
        runs_24h=r[0],
        runs_failed_24h=r[1],
        runs_quarantined_24h=r[2],
        success_rate_24h=_rate(r[3], r[4]),
        success_rate_7d=_rate(r[5], r[6]),
        avg_duration_ms_24h=round(r[7]) if r[7] is not None else None,
        last_successful_run_at=r[8],
        pending_tasks=r[9],
        notifications_pending=n[0],
        notifications_failed=n[1],
    )


async def list_runs(
    session: AsyncSession, paging: PageParams, *, statuses: list[RunStatus] | None = None, company_id: int | None = None
) -> tuple[list[ScrapeRun], int]:
    conds = []
    if statuses:
        conds.append(ScrapeRun.status.in_(statuses))
    if company_id is not None:
        conds.append(ScrapeRun.company_id == company_id)
    total = await session.scalar(select(func.count(ScrapeRun.id)).where(*conds)) or 0
    rows = (
        await session.scalars(
            select(ScrapeRun)
            .where(*conds)
            .order_by(ScrapeRun.queued_at.desc(), ScrapeRun.id.desc())
            .limit(paging.page_size)
            .offset(paging.offset)
        )
    ).all()
    return list(rows), total


async def list_notifications(
    session: AsyncSession, paging: PageParams, *, state: str | None = None
) -> tuple[list[NotificationOutbox], int]:
    conds = []
    if state is not None:
        try:
            conds.append(NotificationOutbox.state == OutboxState(state))
        except ValueError:
            raise ValidationFailed(
                f"Unknown notification state '{state}'", details={"allowed": [s.value for s in OutboxState]}
            ) from None
    total = await session.scalar(select(func.count(NotificationOutbox.id)).where(*conds)) or 0
    rows = (
        await session.scalars(
            select(NotificationOutbox)
            .where(*conds)
            .order_by(NotificationOutbox.created_at.desc(), NotificationOutbox.id.desc())
            .limit(paging.page_size)
            .offset(paging.offset)
        )
    ).all()
    return list(rows), total


async def salary_providers(session: AsyncSession) -> list:
    from app.salary.providers import list_providers  # owned by the Jobs agent

    result = list_providers()
    return await result if hasattr(result, "__await__") else result
