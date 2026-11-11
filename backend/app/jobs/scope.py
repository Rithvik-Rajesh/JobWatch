"""LocationScope policy — the ONE place that decides whether a job is "in scope" (default: India).

CENTRAL CONTRACT. Consumers:
  * ingest / manual entry / capture -> set jobs.location_scope via `evaluate`
  * job list defaults, inbox, dashboard "discovered" counts -> filter with `visible_scopes`
  * notifications -> only notify for `in_scope` jobs (never `unknown`)
  * discovery (Phase 3) -> Workday country facet resolved from `LocationScopeSettings.countries`
"""

from collections.abc import Iterable

from sqlalchemy import false, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Job
from app.models.enums import ScopeMatch, WorkArrangement
from app.settings.registry import LocationScopeSettings


def evaluate(country_codes: Iterable[str], work_arrangement: WorkArrangement, cfg: LocationScopeSettings) -> ScopeMatch:
    codes = {c.upper() for c in country_codes if c}
    if codes & set(cfg.countries):
        return ScopeMatch.in_scope
    if work_arrangement == WorkArrangement.remote and cfg.include_remote_anywhere:
        return ScopeMatch.in_scope
    if not codes:
        return ScopeMatch.unknown
    return ScopeMatch.out_of_scope


def visible_scopes(cfg: LocationScopeSettings) -> list[ScopeMatch]:
    """Scopes shown by default in job lists and the inbox."""
    return [ScopeMatch.in_scope, ScopeMatch.unknown] if cfg.include_unknown else [ScopeMatch.in_scope]


NOTIFIABLE_SCOPES = (ScopeMatch.in_scope,)


async def reevaluate_all(session: AsyncSession, cfg: LocationScopeSettings) -> None:
    """Recompute jobs.location_scope after the scope settings change (skips user overrides)."""
    countries = list(cfg.countries)
    base = update(Job).where(Job.scope_overridden.is_(False))
    await session.execute(base.where(Job.country_codes.overlap(countries)).values(location_scope=ScopeMatch.in_scope))
    remote_clause = Job.work_arrangement == WorkArrangement.remote if cfg.include_remote_anywhere else false()
    await session.execute(
        base.where(~Job.country_codes.overlap(countries), remote_clause).values(location_scope=ScopeMatch.in_scope)
    )
    await session.execute(
        base.where(~Job.country_codes.overlap(countries), ~remote_clause, Job.country_codes == []).values(
            location_scope=ScopeMatch.unknown
        )
    )
    await session.execute(
        base.where(~Job.country_codes.overlap(countries), ~remote_clause, Job.country_codes != []).values(
            location_scope=ScopeMatch.out_of_scope
        )
    )
