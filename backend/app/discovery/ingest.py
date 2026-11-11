"""The single write path for discovered jobs (see app/discovery/contracts.py for the contract).

Invariants enforced here:
  * Idempotent upsert keyed on (source, company, external_id).
  * A new `job_versions` row only when the content hash changes.
  * Auto-attach to an existing job only on strong identity (same requisition id / canonical apply URL);
    merely similar jobs get a `duplicate_suggestions` row and stay separate.
  * Absolute posting dates are never overwritten by approximate ones; `first_seen_at` is written once.
  * NEVER writes applications.status_id or applications.reviewed_at.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.applications.lifecycle import create_tracking_record, record_job_event
from app.core.errors import ValidationFailed
from app.discovery.contracts import IngestOutcome, LocationInput, MissesOutcome, PostingInput
from app.discovery.normalize import (
    country_codes,
    extract_skills,
    html_to_text,
    infer_employment_type,
    infer_work_arrangement,
    normalize_title,
    parse_compensation,
    parse_experience,
    parse_locations,
    parse_relative_posted,
    sanitize_html,
    text_to_html,
)
from app.jobs import scope as scope_policy
from app.models import Company, DuplicateSuggestion, Job, JobVersion, Posting, Source
from app.models.enums import (
    Actor,
    Availability,
    DatePrecision,
    DuplicateState,
    EmploymentType,
    EventType,
    WorkArrangement,
)
from app.salary.service import sync_employer_salary
from app.settings.registry import LocationScopeSettings, get_setting

SIMILARITY_THRESHOLD = 0.6
SIMILARITY_WINDOW = timedelta(days=60)
CLOSE_AFTER_MISSES = 2
CLOSE_AFTER_SPAN = timedelta(hours=6)
ABSOLUTE_PRECISIONS = (DatePrecision.exact, DatePrecision.day)
_TRACKING_PARAMS = {"gclid", "fbclid", "ref", "source", "msclkid", "mc_cid", "mc_eid", "ref_src", "_ga", "trk"}
_SOURCE_RANK = {"career_site": 0, "capture": 2, "manual": 3}

# fields that make up a version and (together) the content hash
SIGNIFICANT_FIELDS = (
    "title",
    "department",
    "location_text",
    "locations",
    "employment_type",
    "work_arrangement",
    "experience_text",
    "experience_min_years",
    "experience_max_years",
    "qualifications_required",
    "qualifications_preferred",
    "salary_text",
    "deadline_at",
    "description_text",
)


# ------------------------------------------------------------------------------------------ helpers
def canonical_apply_url(url: str | None) -> str | None:
    """Strip tracking params / fragment, lowercase scheme+host, drop trailing slash."""
    if not url:
        return None
    parts = urlsplit(str(url).strip())
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not (k.lower().startswith("utm_") or k.lower() in _TRACKING_PARAMS)
    ]
    path = parts.path.rstrip("/") or ("/" if parts.path else "")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def merge_posted_date(
    cur_at: datetime | None, cur_prec: DatePrecision, new_at: datetime | None, new_prec: DatePrecision
) -> tuple[datetime | None, DatePrecision, bool]:
    """Absolute dates win over approximate ones, never the reverse. Returns (at, precision, took_new)."""
    if new_at is None or new_prec == DatePrecision.unknown:
        return cur_at, cur_prec, False
    if cur_at is None or cur_prec == DatePrecision.unknown:
        return new_at, new_prec, True
    if cur_prec in ABSOLUTE_PRECISIONS:
        if new_prec == DatePrecision.exact and cur_prec == DatePrecision.day and new_at.date() == cur_at.date():
            return new_at, new_prec, True
        return cur_at, cur_prec, False
    if new_prec in ABSOLUTE_PRECISIONS:
        return new_at, new_prec, True
    return cur_at, cur_prec, False  # both approximate: keep the first estimate


@dataclass
class Normalized:
    title: str
    department: str | None
    location_text: str | None
    locations: list[dict[str, Any]]
    employment_type: EmploymentType
    work_arrangement: WorkArrangement
    experience_text: str | None
    experience_min_years: Decimal | None
    experience_max_years: Decimal | None
    skills: list[str]
    qualifications_required: str | None
    qualifications_preferred: str | None
    salary_text: str | None
    posted_at: datetime | None
    posted_at_precision: DatePrecision
    posted_at_raw: str | None
    deadline_at: datetime | None
    description_html: str | None
    description_text: str | None
    raw_payload: dict[str, Any] | None
    content_hash: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def country_codes(self) -> list[str]:
        return country_codes(self.locations)


def _clean(s: str | None) -> str | None:
    if s is None:
        return None
    s = s.strip()
    return s or None


def normalize_input(data: PostingInput, now: datetime) -> Normalized:
    description_html = sanitize_html(data.description_html)
    description_text = _clean(data.description_text) or html_to_text(description_html)
    if description_html is None and description_text:
        description_html = text_to_html(description_text)
    title = re.sub(r"\s+", " ", data.title).strip()

    # locations
    location_text = _clean(data.location_text)
    if data.locations:
        locs: list[LocationInput] = []
        for loc in data.locations:
            if loc.country_code is None:
                reparsed = next((p for p in parse_locations(loc.raw) if p.country_code), None)
                if reparsed:
                    loc = LocationInput(
                        raw=loc.raw,
                        city=loc.city or reparsed.city,
                        region=loc.region or reparsed.region,
                        country_code=reparsed.country_code,
                    )
            else:
                loc = loc.model_copy(update={"country_code": loc.country_code.upper()})
            locs.append(loc)
        if location_text is None:
            location_text = "; ".join(loc.raw for loc in locs) or None
    else:
        locs = parse_locations(location_text)
    locations = [loc.model_dump() for loc in locs]

    work_arrangement = data.work_arrangement or infer_work_arrangement(
        title, location_text, description=description_text
    )
    employment_type = data.employment_type or infer_employment_type(None, title=title)

    exp_text, exp_min, exp_max = _clean(data.experience_text), data.experience_min_years, data.experience_max_years
    if exp_min is None and exp_max is None:
        parsed = parse_experience(exp_text) or parse_experience(
            "\n".join(t for t in (data.qualifications_required, description_text) if t)
        )
        if parsed:
            exp_min, exp_max = parsed.min_years, parsed.max_years
            exp_text = exp_text or parsed.sentence

    skills = list(dict.fromkeys(data.skills)) if data.skills else extract_skills(title, description_text)

    salary_text = _clean(data.salary_text)
    if salary_text is None and description_text:
        detected = parse_compensation(description_text, require_context=True)
        salary_text = detected.raw_text if detected else None

    posted_at, precision, posted_raw = data.posted_at, data.posted_at_precision, data.posted_at_raw
    if posted_at is None and posted_raw:
        rel = parse_relative_posted(posted_raw, now)
        if rel:
            posted_at, precision = rel
    if posted_at is None:
        precision = DatePrecision.unknown

    norm = Normalized(
        title=title,
        department=_clean(data.department),
        location_text=location_text,
        locations=locations,
        employment_type=employment_type,
        work_arrangement=work_arrangement,
        experience_text=exp_text,
        experience_min_years=exp_min,
        experience_max_years=exp_max,
        skills=skills,
        qualifications_required=_clean(data.qualifications_required),
        qualifications_preferred=_clean(data.qualifications_preferred),
        salary_text=salary_text,
        posted_at=posted_at,
        posted_at_precision=precision,
        posted_at_raw=posted_raw,
        deadline_at=data.deadline_at,
        description_html=description_html,
        description_text=description_text,
        raw_payload=data.raw_payload,
    )
    norm.content_hash = content_hash(_hash_view(norm))
    return norm


def _dec(v: Decimal | None) -> str | None:
    return None if v is None else format(Decimal(v).quantize(Decimal("0.1")), "f")


def _ws(s: str | None) -> str | None:
    return None if s is None else re.sub(r"\s+", " ", s).strip()


def _hash_view(n: Normalized | JobVersion) -> dict[str, Any]:
    locations = sorted(
        (json.dumps(loc, sort_keys=True, default=str) for loc in (n.locations or [])),
    )
    deadline = n.deadline_at.astimezone(UTC).isoformat() if n.deadline_at else None
    return {
        "title": _ws(n.title),
        "department": _ws(n.department),
        "location_text": _ws(n.location_text),
        "locations": locations,
        "employment_type": getattr(n.employment_type, "value", n.employment_type),
        "work_arrangement": getattr(n.work_arrangement, "value", n.work_arrangement),
        "experience_text": _ws(n.experience_text),
        "experience_min_years": _dec(n.experience_min_years),
        "experience_max_years": _dec(n.experience_max_years),
        "qualifications_required": _ws(n.qualifications_required),
        "qualifications_preferred": _ws(n.qualifications_preferred),
        "salary_text": _ws(n.salary_text),
        "deadline_at": deadline,
        "description_text": _ws(n.description_text),
    }


def content_hash(view: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(view, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def changed_fields(prev: JobVersion, new_view: dict[str, Any]) -> list[str]:
    old_view = _hash_view(prev)
    return [f for f in SIGNIFICANT_FIELDS if old_view[f] != new_view[f]]


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------------------- job view maintenance
def _posting_rank(p: Posting, source_keys: dict[int, str]) -> tuple:
    return (
        0 if p.availability == Availability.open else 1,
        _SOURCE_RANK.get(source_keys.get(p.source_id, ""), 1),
        p.id,
    )


async def update_company_open_count(session: AsyncSession, company_id: int) -> None:
    count = await session.scalar(
        select(func.count())
        .select_from(Job)
        .where(Job.company_id == company_id, Job.merged_into_job_id.is_(None), Job.availability == Availability.open)
    )
    company = await session.get(Company, company_id)
    if company is not None and company.open_jobs_count != (count or 0):
        company.open_jobs_count = count or 0
        await session.flush()


async def refresh_job(
    session: AsyncSession,
    job: Job,
    *,
    now: datetime,
    cfg: LocationScopeSettings | None = None,
    scope_only: bool = False,
) -> dict[str, bool]:
    """Recompute the job's denormalized current view from its primary posting, plus availability, last_seen_at,
    scope and employer-salary flag. Never touches the application. Returns {'closed': bool, 'reopened': bool}
    describing an availability transition of the job."""
    postings = list((await session.scalars(select(Posting).where(Posting.job_id == job.id).order_by(Posting.id))).all())
    transitions = {"closed": False, "reopened": False}
    if not postings:
        return transitions
    source_keys = {sid: key for sid, key in (await session.execute(select(Source.id, Source.key))).all()}
    primary = min(postings, key=lambda p: _posting_rank(p, source_keys))
    job.primary_posting_id = primary.id

    version = await session.get(JobVersion, primary.current_version_id) if primary.current_version_id else None
    if version is not None and not scope_only:
        job.title = version.title
        job.title_normalized = normalize_title(version.title)
        job.department = version.department
        job.location_text = version.location_text
        job.locations = version.locations
        job.country_codes = country_codes(version.locations)
        job.employment_type = version.employment_type
        job.work_arrangement = version.work_arrangement
        job.experience_min_years = version.experience_min_years
        job.experience_max_years = version.experience_max_years
        job.skills = version.skills
        job.description_text = version.description_text
        job.deadline_at = version.deadline_at
        salary_text = version.salary_text
        if salary_text is None:  # fall back to another posting of the same job that states compensation
            for other in sorted(postings, key=lambda p: _posting_rank(p, source_keys)):
                if other.id != primary.id and other.current_version_id:
                    other_version = await session.get(JobVersion, other.current_version_id)
                    if other_version is not None and other_version.salary_text:
                        salary_text = other_version.salary_text
                        break
        await sync_employer_salary(session, job, salary_text, now=now)
    job.apply_url = primary.apply_url or primary.source_url
    job.last_seen_at = max(p.last_seen_at for p in postings)

    if not job.scope_overridden:
        cfg = cfg or await get_setting(session, LocationScopeSettings)
        job.location_scope = scope_policy.evaluate(job.country_codes, job.work_arrangement, cfg)

    any_open = any(p.availability == Availability.open for p in postings)
    if any_open and job.availability != Availability.open:
        job.availability, job.closed_at = Availability.open, None
        transitions["reopened"] = True
    elif not any_open and job.availability != Availability.closed:
        job.availability = Availability.closed
        job.closed_at = max((p.closed_at for p in postings if p.closed_at), default=now)
        transitions["closed"] = True
    await session.flush()
    return transitions


# ---------------------------------------------------------------------------------------- the service
class Ingest:
    async def ingest(
        self,
        session: AsyncSession,
        data: PostingInput,
        *,
        seen_at: datetime | None = None,
        scrape_run_id: int | None = None,
    ) -> IngestOutcome:
        now = seen_at or _utcnow()
        source = await session.scalar(select(Source).where(Source.key == data.source_key))
        if source is None:
            raise ValidationFailed(f"Unknown source '{data.source_key}'", code="unknown_source")
        norm = normalize_input(data, now)
        apply_url = str(data.apply_url) if data.apply_url else None
        canonical = canonical_apply_url(apply_url)
        cfg = await get_setting(session, LocationScopeSettings)

        posting = await session.scalar(
            select(Posting).where(
                Posting.source_id == source.id,
                Posting.company_id == data.company_id,
                Posting.external_id == data.external_id,
            )
        )
        if posting is not None:
            return await self._update_existing(
                session, posting, data, norm, apply_url, canonical, now, cfg, scrape_run_id
            )
        return await self._create_posting(session, source, data, norm, apply_url, canonical, now, cfg, scrape_run_id)

    # -- existing posting ------------------------------------------------------------------------
    async def _update_existing(
        self,
        session: AsyncSession,
        posting: Posting,
        data: PostingInput,
        norm: Normalized,
        apply_url: str | None,
        canonical: str | None,
        now: datetime,
        cfg: LocationScopeSettings,
        scrape_run_id: int | None,
    ) -> IngestOutcome:
        job = await session.get(Job, posting.job_id)
        assert job is not None
        reopened = posting.availability == Availability.closed
        posting.last_seen_at = max(posting.last_seen_at, now)
        posting.availability = Availability.open
        posting.missing_since = None
        posting.consecutive_misses = 0
        posting.closed_at = None
        posting.source_url = str(data.source_url)
        if apply_url:
            posting.apply_url, posting.canonical_apply_url = apply_url, canonical
        if data.requisition_id:
            posting.requisition_id = data.requisition_id
        if data.listing_fingerprint:
            posting.listing_fingerprint = data.listing_fingerprint

        new_version = False
        changed: list[str] = []
        if posting.content_hash != norm.content_hash or posting.current_version_id is None:
            previous = await session.get(JobVersion, posting.current_version_id) if posting.current_version_id else None
            changed = changed_fields(previous, _hash_view(norm)) if previous else []
            await self._add_version(session, posting, norm, previous, changed, now, scrape_run_id)
            new_version = True

        self._apply_posted_date(job, norm)
        was_primary = job.primary_posting_id == posting.id
        transitions = await refresh_job(session, job, now=now, cfg=cfg)
        if new_version and changed and (was_primary or job.primary_posting_id == posting.id):
            job.last_changed_at = now
            await record_job_event(
                session,
                job.id,
                EventType.job_updated,
                "Posting updated: " + ", ".join(changed),
                actor=Actor.system,
                occurred_at=now,
                payload={"changed_fields": changed, "posting_id": posting.id},
            )
        if transitions["reopened"]:
            await record_job_event(
                session,
                job.id,
                EventType.job_reopened,
                "Posting reopened",
                actor=Actor.system,
                occurred_at=now,
                payload={"posting_id": posting.id},
            )
        if reopened or transitions["reopened"] or transitions["closed"]:
            await update_company_open_count(session, job.company_id)
        return IngestOutcome(
            job_id=job.id,
            posting_id=posting.id,
            created_job=False,
            created_posting=False,
            new_version=new_version,
            changed_fields=changed,
            reopened=reopened,
        )

    # -- new posting -----------------------------------------------------------------------------
    async def _create_posting(
        self,
        session: AsyncSession,
        source: Source,
        data: PostingInput,
        norm: Normalized,
        apply_url: str | None,
        canonical: str | None,
        now: datetime,
        cfg: LocationScopeSettings,
        scrape_run_id: int | None,
    ) -> IngestOutcome:
        job = await self._find_strong_match(session, data, canonical)
        created_job = job is None
        if job is None:
            job = Job(
                company_id=data.company_id,
                title=norm.title,
                title_normalized=normalize_title(norm.title),
                first_seen_at=now,
                last_seen_at=now,
                last_changed_at=now,
                posted_at=norm.posted_at,
                posted_at_precision=norm.posted_at_precision,
                posted_at_raw=norm.posted_at_raw,
                availability=Availability.open,
            )
            session.add(job)
            await session.flush()
        else:
            self._apply_posted_date(job, norm)

        posting = Posting(
            job_id=job.id,
            source_id=source.id,
            company_id=data.company_id,
            external_id=data.external_id,
            requisition_id=data.requisition_id,
            source_url=str(data.source_url),
            apply_url=apply_url,
            canonical_apply_url=canonical,
            listing_fingerprint=data.listing_fingerprint,
            availability=Availability.open,
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add(posting)
        await session.flush()
        await self._add_version(session, posting, norm, None, [], now, scrape_run_id)

        if created_job:
            job.primary_posting_id = posting.id
            await session.flush()
            payload: dict[str, Any] = {"source": source.key, "posting_id": posting.id}
            origin = (data.raw_payload or {}).get("origin_platform")
            if origin:
                payload["origin_platform"] = origin
            await create_tracking_record(
                session,
                job.id,
                discovered_at=now,
                summary=f"Discovered via {source.name}",
                payload=payload,
            )
        await refresh_job(session, job, now=now, cfg=cfg)
        await update_company_open_count(session, job.company_id)

        suggestion_ids: list[int] = []
        if created_job:
            suggestion_ids = await self._suggest_duplicates(session, job, now, source_id=source.id)
        return IngestOutcome(
            job_id=job.id,
            posting_id=posting.id,
            created_job=created_job,
            created_posting=True,
            new_version=True,
            attached_to_existing_job=not created_job,
            duplicate_suggestion_ids=suggestion_ids,
        )

    # -- helpers ---------------------------------------------------------------------------------
    @staticmethod
    def _apply_posted_date(job: Job, norm: Normalized) -> None:
        at, prec, took = merge_posted_date(
            job.posted_at, job.posted_at_precision, norm.posted_at, norm.posted_at_precision
        )
        if took:
            job.posted_at, job.posted_at_precision, job.posted_at_raw = at, prec, norm.posted_at_raw

    @staticmethod
    async def _add_version(
        session: AsyncSession,
        posting: Posting,
        norm: Normalized,
        previous: JobVersion | None,
        changed: list[str],
        now: datetime,
        scrape_run_id: int | None,
    ) -> JobVersion:
        p_at, p_prec, p_raw = norm.posted_at, norm.posted_at_precision, norm.posted_at_raw
        if previous is not None:
            at, prec, took = merge_posted_date(previous.posted_at, previous.posted_at_precision, p_at, p_prec)
            p_at, p_prec = at, prec
            p_raw = p_raw if took else previous.posted_at_raw
        max_no = await session.scalar(
            select(func.coalesce(func.max(JobVersion.version_no), 0)).where(JobVersion.posting_id == posting.id)
        )
        version_no = (max_no or 0) + 1
        version = JobVersion(
            posting_id=posting.id,
            version_no=version_no,
            content_hash=norm.content_hash,
            captured_at=now,
            scrape_run_id=scrape_run_id,
            title=norm.title,
            department=norm.department,
            location_text=norm.location_text,
            locations=norm.locations,
            employment_type=norm.employment_type,
            work_arrangement=norm.work_arrangement,
            experience_text=norm.experience_text,
            experience_min_years=norm.experience_min_years,
            experience_max_years=norm.experience_max_years,
            skills=norm.skills,
            qualifications_required=norm.qualifications_required,
            qualifications_preferred=norm.qualifications_preferred,
            salary_text=norm.salary_text,
            posted_at=p_at,
            posted_at_precision=p_prec,
            posted_at_raw=p_raw,
            deadline_at=norm.deadline_at,
            description_html=norm.description_html,
            description_text=norm.description_text,
            raw_payload=norm.raw_payload,
            changed_fields=changed,
        )
        session.add(version)
        await session.flush()
        posting.current_version_id = version.id
        posting.content_hash = norm.content_hash
        await session.flush()
        return version

    @staticmethod
    async def _find_strong_match(session: AsyncSession, data: PostingInput, canonical: str | None) -> Job | None:
        clauses = []
        if data.requisition_id:
            clauses.append(func.lower(Posting.requisition_id) == data.requisition_id.strip().lower())
        if canonical:
            clauses.append(Posting.canonical_apply_url == canonical)
        if not clauses:
            return None
        return await session.scalar(
            select(Job)
            .join(Posting, Posting.job_id == Job.id)
            .where(Job.company_id == data.company_id, Job.merged_into_job_id.is_(None), or_(*clauses))
            .order_by(Job.id)
            .limit(1)
        )

    @staticmethod
    async def _suggest_duplicates(session: AsyncSession, job: Job, now: datetime, *, source_id: int) -> list[int]:
        """Suggest only CROSS-SOURCE pairs: two different external ids on the same source are distinct
        requisitions by definition (e.g. "Software Engineer" vs "Senior Software Engineer" on one
        career site), so the other job must have at least one posting from a different source."""
        sim = func.similarity(Job.title_normalized, job.title_normalized)
        other_source = select(Posting.id).where(Posting.job_id == Job.id, Posting.source_id != source_id).exists()
        rows = (
            await session.execute(
                select(Job.id, Job.country_codes, sim.label("score")).where(
                    Job.company_id == job.company_id,
                    Job.id != job.id,
                    Job.merged_into_job_id.is_(None),
                    Job.last_seen_at >= now - SIMILARITY_WINDOW,
                    sim >= SIMILARITY_THRESHOLD,
                    other_source,
                )
            )
        ).all()
        ids: list[int] = []
        mine = set(job.country_codes)
        for other_id, other_codes, score in rows:
            theirs = set(other_codes)
            if mine and theirs:
                if not mine & theirs:
                    continue
                location_reason = "same_location"
            elif not mine and not theirs:
                location_reason = "location_unknown"
            else:
                continue
            a, b = sorted((job.id, other_id))
            stmt = (
                pg_insert(DuplicateSuggestion)
                .values(
                    job_a_id=a,
                    job_b_id=b,
                    score=round(float(score), 3),
                    reasons=[f"title_similarity:{float(score):.2f}", location_reason],
                    state=DuplicateState.pending,
                )
                .on_conflict_do_nothing(constraint="uq_duplicate_suggestions_pair")
                .returning(DuplicateSuggestion.id)
            )
            new_id = await session.scalar(stmt)
            if new_id is not None:
                ids.append(new_id)
        return ids

    # -- absences --------------------------------------------------------------------------------
    async def apply_misses(
        self,
        session: AsyncSession,
        *,
        source_key: str,
        company_id: int,
        seen_external_ids: set[str],
        run_complete: bool,
        run_trusted: bool,
        now: datetime | None = None,
    ) -> MissesOutcome:
        outcome = MissesOutcome()
        if not (run_complete and run_trusted):
            return outcome
        now = now or _utcnow()
        source = await session.scalar(select(Source).where(Source.key == source_key))
        if source is None:
            raise ValidationFailed(f"Unknown source '{source_key}'", code="unknown_source")
        query = select(Posting).where(
            Posting.source_id == source.id,
            Posting.company_id == company_id,
            Posting.availability == Availability.open,
        )
        if seen_external_ids:
            query = query.where(Posting.external_id.not_in(seen_external_ids))
        postings = list((await session.scalars(query.order_by(Posting.id))).all())
        affected_jobs: dict[int, list[Posting]] = {}
        for p in postings:
            p.consecutive_misses += 1
            if p.missing_since is None:
                p.missing_since = now
            outcome.misses_recorded += 1
            if p.consecutive_misses >= CLOSE_AFTER_MISSES and now - p.missing_since >= CLOSE_AFTER_SPAN:
                p.availability = Availability.closed
                p.closed_at = now
                outcome.closed_posting_ids.append(p.id)
                affected_jobs.setdefault(p.job_id, []).append(p)
        await session.flush()
        cfg = await get_setting(session, LocationScopeSettings) if affected_jobs else None
        for job_id in affected_jobs:
            job = await session.get(Job, job_id)
            assert job is not None
            transitions = await refresh_job(session, job, now=now, cfg=cfg)
            if transitions["closed"]:
                outcome.closed_job_ids.append(job.id)
                await record_job_event(
                    session,
                    job.id,
                    EventType.job_closed,
                    "Posting no longer listed by the employer",
                    actor=Actor.system,
                    occurred_at=now,
                    payload={"posting_ids": [p.id for p in affected_jobs[job_id]]},
                )
        if affected_jobs:
            await update_company_open_count(session, company_id)
        return outcome


ingest_service = Ingest()
