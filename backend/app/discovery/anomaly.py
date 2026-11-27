"""Run anomaly checks. A suspicious run is `quarantined`: nothing is trusted, so no jobs are closed.

Rules (any one quarantines):
  * 0 in-scope postings while the baseline is > 0
  * count < 50% of the baseline and the absolute drop is > 10
  * more than 20% of listed postings are missing a title or url
Baseline = rolling median of the in-scope counts of the site's last 5 succeeded, complete runs.
"""

from dataclasses import dataclass
from statistics import median

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ScrapeRun
from app.models.enums import RunStatus

BASELINE_RUNS = 5
DROP_RATIO = 0.5
MIN_DROP = 10
MAX_MISSING_RATIO = 0.2


@dataclass(frozen=True)
class AnomalyResult:
    quarantined: bool
    reason: str | None = None


async def compute_baseline(
    session: AsyncSession, career_site_id: int, *, exclude_run_id: int | None = None
) -> int | None:
    """Median in-scope count of the last successful complete runs (None when there is no history)."""
    query = (
        select(ScrapeRun.postings_in_scope)
        .where(
            ScrapeRun.career_site_id == career_site_id,
            ScrapeRun.status == RunStatus.succeeded,
            ScrapeRun.complete.is_(True),
        )
        .order_by(ScrapeRun.queued_at.desc(), ScrapeRun.id.desc())
        .limit(BASELINE_RUNS)
    )
    if exclude_run_id is not None:
        query = query.where(ScrapeRun.id != exclude_run_id)
    counts = list((await session.scalars(query)).all())
    return round(median(counts)) if counts else None


def check_run(*, in_scope_count: int, listed: int, missing_fields: int, baseline: int | None) -> AnomalyResult:
    if listed > 0 and missing_fields / listed > MAX_MISSING_RATIO:
        return AnomalyResult(True, f"{missing_fields}/{listed} postings are missing a title or url")
    if baseline and baseline > 0:
        if in_scope_count == 0:
            return AnomalyResult(True, f"0 postings but baseline is {baseline}")
        if in_scope_count < baseline * DROP_RATIO and baseline - in_scope_count > MIN_DROP:
            return AnomalyResult(True, f"{in_scope_count} postings is a drop of more than 50% from baseline {baseline}")
    return AnomalyResult(False)
