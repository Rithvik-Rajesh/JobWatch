"""Salary records: employer-provided (from posting text), user-entered, and the per-job salary view."""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.applications.lifecycle import record_job_event
from app.core.errors import Conflict, NotFound
from app.discovery.normalize.salary import parse_compensation
from app.models import Job, SalaryRecord
from app.models.enums import Actor, EventType, SalaryPeriod, SalaryProvenance
from app.salary.schemas import JobSalaryView, SalaryRecordOut, UserSalaryCreate

EMPLOYER_PROVIDER = "job_posting"


async def current_employer_record(session: AsyncSession, job_id: int) -> SalaryRecord | None:
    return await session.scalar(
        select(SalaryRecord).where(
            SalaryRecord.job_id == job_id,
            SalaryRecord.provenance == SalaryProvenance.employer_provided,
            SalaryRecord.is_current.is_(True),
        )
    )


async def sync_employer_salary(
    session: AsyncSession, job: Job, salary_text: str | None, *, now: datetime | None = None
) -> bool:
    """Make the job's current employer-provided record reflect `salary_text` (None/unparseable clears it).
    Returns has_employer_salary and updates the denormalized flag on the job."""
    now = now or datetime.now(UTC)
    parsed = parse_compensation(salary_text) if salary_text else None
    existing = await current_employer_record(session, job.id)
    raw = parsed.raw_text if parsed else None
    if salary_text and len(salary_text.strip()) <= 200:
        raw = salary_text.strip()
    if parsed is None:
        if existing is not None:
            existing.is_current = False
            await session.flush()
        job.has_employer_salary = False
        return False
    if (
        existing is not None
        and existing.currency == parsed.currency
        and existing.period == parsed.period
        and existing.amount_min == parsed.amount_min
        and existing.amount_max == parsed.amount_max
        and existing.raw_text == raw
    ):
        job.has_employer_salary = True
        return True
    if existing is not None:
        existing.is_current = False
        await session.flush()
    session.add(
        SalaryRecord(
            job_id=job.id,
            provenance=SalaryProvenance.employer_provided,
            provider=EMPLOYER_PROVIDER,
            is_current=True,
            currency=parsed.currency,
            period=parsed.period,
            amount_min=parsed.amount_min,
            amount_max=parsed.amount_max,
            amount_avg=(parsed.amount_min + parsed.amount_max) / 2,
            raw_text=raw,
            retrieved_at=now,
        )
    )
    await session.flush()
    job.has_employer_salary = True
    return True


async def _job(session: AsyncSession, job_id: int) -> Job:
    job = await session.get(Job, job_id)
    if job is None:
        raise NotFound(f"Job {job_id} not found", code="job_not_found")
    return job


async def get_job_salary_view(session: AsyncSession, job_id: int) -> JobSalaryView:
    await _job(session, job_id)
    rows = (
        await session.scalars(
            select(SalaryRecord)
            .where(SalaryRecord.job_id == job_id, SalaryRecord.is_current.is_(True))
            .order_by(SalaryRecord.retrieved_at.desc(), SalaryRecord.id.desc())
        )
    ).all()
    employer = [SalaryRecordOut.model_validate(r) for r in rows if r.provenance == SalaryProvenance.employer_provided]
    estimates = [SalaryRecordOut.model_validate(r) for r in rows if r.provenance != SalaryProvenance.employer_provided]
    return JobSalaryView(employer_provided=employer, estimates=estimates, available=bool(rows))


async def add_user_salary(
    session: AsyncSession, job_id: int, body: UserSalaryCreate, *, now: datetime | None = None
) -> JobSalaryView:
    now = now or datetime.now(UTC)
    await _job(session, job_id)
    source_name = body.source_name.strip()
    provider = f"user:{source_name.lower()}"
    await session.execute(
        update(SalaryRecord)
        .where(SalaryRecord.job_id == job_id, SalaryRecord.provider == provider, SalaryRecord.is_current.is_(True))
        .values(is_current=False)
    )
    amounts = [a for a in (body.amount_min, body.amount_max) if a is not None]
    avg = body.amount_avg if body.amount_avg is not None else (sum(amounts) / len(amounts) if amounts else None)
    session.add(
        SalaryRecord(
            job_id=job_id,
            provenance=SalaryProvenance.user_entered,
            provider=provider,
            is_current=True,
            currency=body.currency.upper(),
            period=body.period,
            amount_min=body.amount_min,
            amount_max=body.amount_max,
            amount_avg=Decimal(avg) if avg is not None else None,
            role_matched=body.role_matched,
            role_match=body.role_match,
            experience_band=body.experience_band,
            experience_match=body.experience_match,
            sample_size=body.sample_size,
            confidence=body.confidence,
            source_url=str(body.source_url) if body.source_url else None,
            retrieved_at=now,
            notes=body.notes,
        )
    )
    await session.flush()
    await record_job_event(
        session,
        job_id,
        EventType.salary_added,
        f"Salary added from {source_name}",
        actor=Actor.user,
        occurred_at=now,
        payload={"provider": provider, "currency": body.currency.upper(), "period": body.period.value},
    )
    return await get_job_salary_view(session, job_id)


async def delete_user_salary(session: AsyncSession, job_id: int, record_id: int) -> None:
    record = await session.get(SalaryRecord, record_id)
    if record is None or record.job_id != job_id:
        raise NotFound("Salary record not found", code="salary_record_not_found")
    if record.provenance != SalaryProvenance.user_entered:
        raise Conflict("Only user-entered salary records can be deleted", code="salary_not_deletable")
    record.is_current = False
    await session.flush()


def annualized(amount: Decimal | None, period: SalaryPeriod) -> Decimal | None:
    if amount is None:
        return None
    return amount * 12 if period == SalaryPeriod.monthly else amount
