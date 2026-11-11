"""Jobs API (discovery list, inbox, detail, manual entry, capture, versions, duplicates).
Owner: jobs & ingest agent. Paths, parameters and response models are the frozen contract."""

from typing import Annotated

from fastapi import APIRouter, Query

from app.core.auth import CaptureClient, CurrentUser
from app.core.db import SessionDep
from app.core.pagination import Page, PageDep
from app.jobs import capture, merge, queries, service
from app.jobs.schemas import (
    CaptureRequest,
    CaptureResult,
    DuplicateSuggestionOut,
    InboxSummary,
    JobDetail,
    JobFacets,
    JobFilters,
    JobListItem,
    JobUpdate,
    ManualJobCreate,
    MergeRequest,
    ReviewRequest,
    VersionDiff,
    VersionOut,
)
from app.salary import service as salary_service
from app.salary.schemas import JobSalaryView, UserSalaryCreate

router = APIRouter(tags=["jobs"])
FiltersDep = Annotated[JobFilters, Query()]


@router.get("/jobs", response_model=Page[JobListItem])
async def list_jobs(session: SessionDep, user: CurrentUser, filters: FiltersDep, paging: PageDep):
    return await queries.list_jobs(session, filters, paging)


@router.get("/jobs/facets", response_model=JobFacets)
async def job_facets(session: SessionDep, user: CurrentUser, filters: FiltersDep):
    """Counts per facet value under the current filters (excluding the facet's own filter)."""
    return await queries.job_facets(session, filters)


@router.get("/inbox", response_model=Page[JobListItem])
async def inbox(session: SessionDep, user: CurrentUser, filters: FiltersDep, paging: PageDep):
    """Unreviewed jobs (applications.reviewed_at IS NULL), visible scopes only, newest first."""
    return await queries.list_jobs(session, filters.model_copy(update={"inbox": True}), paging)


@router.get("/inbox/summary", response_model=InboxSummary)
async def inbox_summary(session: SessionDep, user: CurrentUser):
    return await queries.inbox_summary(session)


@router.post("/jobs/review", status_code=204)
async def mark_reviewed(body: ReviewRequest, session: SessionDep, user: CurrentUser):
    """Remove jobs from the inbox without changing their status (adds a 'reviewed' event)."""
    await service.mark_reviewed(session, body.job_ids)


@router.post("/jobs/unreview", status_code=204)
async def mark_unreviewed(body: ReviewRequest, session: SessionDep, user: CurrentUser):
    """Undo for mark_reviewed (returns jobs to the inbox)."""
    await service.mark_unreviewed(session, body.job_ids)


@router.post("/jobs", response_model=JobDetail, status_code=201)
async def create_manual_job(body: ManualJobCreate, session: SessionDep, user: CurrentUser):
    return await service.create_manual_job(session, body)


@router.post("/jobs/capture", response_model=CaptureResult)
async def capture_job(body: CaptureRequest, session: SessionDep, client: CaptureClient):
    """Bookmarklet endpoint. Parses JobPosting JSON-LD from the payload — never fetches the URL."""
    return await capture.capture_job(session, body)


@router.get("/jobs/{job_id}", response_model=JobDetail)
async def get_job(job_id: int, session: SessionDep, user: CurrentUser):
    return await service.get_job_detail(session, job_id)


@router.patch("/jobs/{job_id}", response_model=JobDetail)
async def update_job(job_id: int, body: JobUpdate, session: SessionDep, user: CurrentUser):
    return await service.update_job(session, job_id, body)


@router.get("/jobs/{job_id}/versions", response_model=list[VersionOut])
async def list_versions(job_id: int, session: SessionDep, user: CurrentUser):
    return await service.list_versions(session, job_id)


@router.get("/jobs/{job_id}/versions/{version_id}/diff", response_model=VersionDiff)
async def version_diff(job_id: int, version_id: int, session: SessionDep, user: CurrentUser):
    return await service.version_diff(session, job_id, version_id)


@router.get("/jobs/{job_id}/duplicates", response_model=list[DuplicateSuggestionOut])
async def job_duplicates(job_id: int, session: SessionDep, user: CurrentUser):
    return await service.job_duplicates(session, job_id)


@router.post("/jobs/{job_id}/merge", response_model=JobDetail)
async def merge_jobs(job_id: int, body: MergeRequest, session: SessionDep, user: CurrentUser):
    await merge.merge_jobs(session, job_id, body.other_job_id, take_status_from_other=body.take_status_from_other)
    return await service.get_job_detail(session, job_id)


@router.get("/duplicates", response_model=Page[DuplicateSuggestionOut])
async def list_duplicate_suggestions(session: SessionDep, user: CurrentUser, paging: PageDep):
    """Pending suggestions, highest score first."""
    return await service.list_pending_duplicates(session, paging)


@router.post("/duplicates/{suggestion_id}/confirm", response_model=JobDetail)
async def confirm_duplicate(suggestion_id: int, session: SessionDep, user: CurrentUser):
    """Merge job_b into job_a (older job survives)."""
    survivor = await merge.confirm_suggestion(session, suggestion_id)
    return await service.get_job_detail(session, survivor.id)


@router.post("/duplicates/{suggestion_id}/dismiss", status_code=204)
async def dismiss_duplicate(suggestion_id: int, session: SessionDep, user: CurrentUser):
    await merge.dismiss_suggestion(session, suggestion_id)


@router.get("/jobs/{job_id}/salary", response_model=JobSalaryView)
async def job_salary(job_id: int, session: SessionDep, user: CurrentUser):
    return await salary_service.get_job_salary_view(session, job_id)


@router.post("/jobs/{job_id}/salary", response_model=JobSalaryView, status_code=201)
async def add_user_salary(job_id: int, body: UserSalaryCreate, session: SessionDep, user: CurrentUser):
    return await salary_service.add_user_salary(session, job_id, body)


@router.delete("/jobs/{job_id}/salary/{record_id}", status_code=204)
async def delete_user_salary(job_id: int, record_id: int, session: SessionDep, user: CurrentUser):
    """Only user_entered records can be deleted (marks is_current=false; history kept)."""
    await salary_service.delete_user_salary(session, job_id, record_id)
