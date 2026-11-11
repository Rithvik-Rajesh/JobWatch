"""Settings, saved views, resume versions, tags. Owner: catalog agent."""

from typing import Any

from fastapi import APIRouter

from app.core.auth import CurrentUser
from app.core.db import SessionDep
from app.settings import service
from app.settings.schemas import (
    ResumeVersionCreate,
    ResumeVersionOut,
    ResumeVersionUpdate,
    SavedViewCreate,
    SavedViewOut,
    SavedViewUpdate,
    SettingOut,
    TagCreate,
    TagOut,
    TagUpdate,
)

router = APIRouter(tags=["settings"])


@router.get("/settings", response_model=list[SettingOut])
async def list_settings(session: SessionDep, user: CurrentUser):
    return await service.list_settings(session)


@router.get("/settings/{key}", response_model=SettingOut)
async def get_setting_value(key: str, session: SessionDep, user: CurrentUser):
    return await service.get_setting_value(session, key)


@router.put("/settings/{key}", response_model=SettingOut)
async def put_setting_value(key: str, body: dict[str, Any], session: SessionDep, user: CurrentUser):
    """Validated against app.settings.registry. Changing 'location_scope' re-evaluates all jobs'
    location_scope in the same transaction."""
    return await service.put_setting_value(session, key, body)


@router.get("/saved-views", response_model=list[SavedViewOut])
async def list_saved_views(session: SessionDep, user: CurrentUser, page: str | None = None):
    return await service.list_saved_views(session, page)


@router.post("/saved-views", response_model=SavedViewOut, status_code=201)
async def create_saved_view(body: SavedViewCreate, session: SessionDep, user: CurrentUser):
    return await service.create_saved_view(session, body)


@router.patch("/saved-views/{view_id}", response_model=SavedViewOut)
async def update_saved_view(view_id: int, body: SavedViewUpdate, session: SessionDep, user: CurrentUser):
    return await service.update_saved_view(session, view_id, body)


@router.delete("/saved-views/{view_id}", status_code=204)
async def delete_saved_view(view_id: int, session: SessionDep, user: CurrentUser):
    await service.delete_saved_view(session, view_id)


@router.get("/resume-versions", response_model=list[ResumeVersionOut])
async def list_resume_versions(session: SessionDep, user: CurrentUser, include_archived: bool = False):
    return await service.list_resume_versions(session, include_archived)


@router.post("/resume-versions", response_model=ResumeVersionOut, status_code=201)
async def create_resume_version(body: ResumeVersionCreate, session: SessionDep, user: CurrentUser):
    return await service.create_resume_version(session, body)


@router.patch("/resume-versions/{resume_id}", response_model=ResumeVersionOut)
async def update_resume_version(resume_id: int, body: ResumeVersionUpdate, session: SessionDep, user: CurrentUser):
    return await service.update_resume_version(session, resume_id, body)


@router.get("/tags", response_model=list[TagOut])
async def list_tags(session: SessionDep, user: CurrentUser):
    return await service.list_tags(session)


@router.post("/tags", response_model=TagOut, status_code=201)
async def create_tag(body: TagCreate, session: SessionDep, user: CurrentUser):
    return await service.create_tag(session, body)


@router.patch("/tags/{tag_id}", response_model=TagOut)
async def update_tag(tag_id: int, body: TagUpdate, session: SessionDep, user: CurrentUser):
    return await service.update_tag(session, tag_id, body)


@router.delete("/tags/{tag_id}", status_code=204)
async def delete_tag(tag_id: int, session: SessionDep, user: CurrentUser):
    await service.delete_tag(session, tag_id)


@router.put("/jobs/{job_id}/tags", response_model=list[TagOut])
async def set_job_tags(job_id: int, tag_names: list[str], session: SessionDep, user: CurrentUser):
    """Replace the job's tags; unknown tag names are created."""
    return await service.set_job_tags(session, job_id, tag_names)
