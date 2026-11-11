"""Settings, saved views, resume versions, tags and job tag assignment."""

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound, ValidationFailed
from app.jobs.scope import reevaluate_all
from app.models import AppSetting, Job, JobTag, ResumeVersion, SavedView, Tag
from app.settings.registry import SETTINGS_MODELS, LocationScopeSettings, put_setting
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

# ---------------------------------------------------------------- typed settings


async def _stored(session: AsyncSession) -> dict[str, dict[str, Any]]:
    rows = await session.execute(select(AppSetting.key, AppSetting.value))
    return {k: v for k, v in rows.all()}


def _effective(key: str, stored: dict[str, dict[str, Any]]) -> SettingOut:
    value = SETTINGS_MODELS[key].model_validate(stored.get(key) or {}).model_dump(mode="json")
    return SettingOut(key=key, value=value)


async def list_settings(session: AsyncSession) -> list[SettingOut]:
    stored = await _stored(session)
    return [_effective(key, stored) for key in SETTINGS_MODELS]


async def get_setting_value(session: AsyncSession, key: str) -> SettingOut:
    if key not in SETTINGS_MODELS:
        raise NotFound(f"Unknown setting '{key}'")
    return _effective(key, await _stored(session))


async def put_setting_value(session: AsyncSession, key: str, value: dict[str, Any]) -> SettingOut:
    if key not in SETTINGS_MODELS:
        raise NotFound(f"Unknown setting '{key}'")
    try:
        validated = await put_setting(session, key, value)
    except ValidationError as e:
        raise ValidationFailed(f"Invalid value for setting '{key}'", details=json.loads(e.json())) from e
    if isinstance(validated, LocationScopeSettings):
        # Same transaction: scope setting and every job's location_scope change together.
        await reevaluate_all(session, validated)
    return SettingOut(key=key, value=validated.model_dump(mode="json"))


# ---------------------------------------------------------------- saved views


async def _unset_defaults(session: AsyncSession, page: str, *, except_id: int | None = None) -> None:
    stmt = update(SavedView).where(SavedView.page == page, SavedView.is_default.is_(True))
    if except_id is not None:
        stmt = stmt.where(SavedView.id != except_id)
    await session.execute(stmt.values(is_default=False))


async def _view_name_taken(session: AsyncSession, page: str, name: str, *, except_id: int | None = None) -> bool:
    q = select(SavedView.id).where(SavedView.page == page, SavedView.name == name)
    if except_id is not None:
        q = q.where(SavedView.id != except_id)
    return await session.scalar(q.limit(1)) is not None


async def list_saved_views(session: AsyncSession, page: str | None = None) -> list[SavedViewOut]:
    q = select(SavedView).order_by(SavedView.page, SavedView.position, SavedView.id)
    if page:
        q = q.where(SavedView.page == page)
    return [SavedViewOut.model_validate(v) for v in (await session.scalars(q)).all()]


async def create_saved_view(session: AsyncSession, body: SavedViewCreate) -> SavedViewOut:
    if await _view_name_taken(session, body.page, body.name):
        raise Conflict(f"A saved view named '{body.name}' already exists on page '{body.page}'")
    last = await session.scalar(select(func.max(SavedView.position)).where(SavedView.page == body.page))
    if body.is_default:
        await _unset_defaults(session, body.page)
    view = SavedView(
        page=body.page,
        name=body.name,
        filters=body.filters,
        sort=body.sort,
        columns=body.columns,
        is_default=body.is_default,
        position=0 if last is None else last + 1,
    )
    session.add(view)
    await session.flush()
    await session.refresh(view)
    return SavedViewOut.model_validate(view)


async def update_saved_view(session: AsyncSession, view_id: int, body: SavedViewUpdate) -> SavedViewOut:
    view = await session.get(SavedView, view_id)
    if view is None:
        raise NotFound("Saved view not found")
    provided = body.model_fields_set
    if body.name is not None and body.name != view.name:
        if await _view_name_taken(session, view.page, body.name, except_id=view.id):
            raise Conflict(f"A saved view named '{body.name}' already exists on page '{view.page}'")
        view.name = body.name
    if body.filters is not None:
        view.filters = body.filters
    if body.sort is not None:
        view.sort = body.sort
    if "columns" in provided:
        view.columns = body.columns
    if body.position is not None:
        view.position = body.position
    if body.is_default is not None:
        if body.is_default:
            await _unset_defaults(session, view.page, except_id=view.id)
        view.is_default = body.is_default
    await session.flush()
    await session.refresh(view)
    return SavedViewOut.model_validate(view)


async def delete_saved_view(session: AsyncSession, view_id: int) -> None:
    view = await session.get(SavedView, view_id)
    if view is None:
        raise NotFound("Saved view not found")
    await session.delete(view)
    await session.flush()


# ---------------------------------------------------------------- resume versions


async def list_resume_versions(session: AsyncSession, include_archived: bool = False) -> list[ResumeVersionOut]:
    q = select(ResumeVersion).order_by(ResumeVersion.created_at.desc(), ResumeVersion.id.desc())
    if not include_archived:
        q = q.where(ResumeVersion.archived_at.is_(None))
    return [ResumeVersionOut.model_validate(r) for r in (await session.scalars(q)).all()]


async def _resume_label_taken(session: AsyncSession, label: str, *, except_id: int | None = None) -> bool:
    q = select(ResumeVersion.id).where(ResumeVersion.label == label)
    if except_id is not None:
        q = q.where(ResumeVersion.id != except_id)
    return await session.scalar(q.limit(1)) is not None


async def create_resume_version(session: AsyncSession, body: ResumeVersionCreate) -> ResumeVersionOut:
    if await _resume_label_taken(session, body.label):
        raise Conflict(f"A resume version labelled '{body.label}' already exists")
    resume = ResumeVersion(label=body.label, file_url=str(body.file_url) if body.file_url else None, notes=body.notes)
    session.add(resume)
    await session.flush()
    await session.refresh(resume)
    return ResumeVersionOut.model_validate(resume)


async def update_resume_version(
    session: AsyncSession, resume_id: int, body: ResumeVersionUpdate, *, now: datetime | None = None
) -> ResumeVersionOut:
    resume = await session.get(ResumeVersion, resume_id)
    if resume is None:
        raise NotFound("Resume version not found")
    provided = body.model_fields_set
    if body.label is not None and body.label != resume.label:
        if await _resume_label_taken(session, body.label, except_id=resume.id):
            raise Conflict(f"A resume version labelled '{body.label}' already exists")
        resume.label = body.label
    if "file_url" in provided:
        resume.file_url = str(body.file_url) if body.file_url else None
    if "notes" in provided:
        resume.notes = body.notes
    if body.archived is True and resume.archived_at is None:
        resume.archived_at = now or datetime.now(UTC)
    elif body.archived is False:
        resume.archived_at = None
    await session.flush()
    await session.refresh(resume)
    return ResumeVersionOut.model_validate(resume)


# ---------------------------------------------------------------- tags


async def _tag_taken(session: AsyncSession, name: str, *, except_id: int | None = None) -> bool:
    q = select(Tag.id).where(func.lower(Tag.name) == name.lower())
    if except_id is not None:
        q = q.where(Tag.id != except_id)
    return await session.scalar(q.limit(1)) is not None


async def list_tags(session: AsyncSession) -> list[TagOut]:
    rows = await session.scalars(select(Tag).order_by(func.lower(Tag.name), Tag.id))
    return [TagOut.model_validate(t) for t in rows.all()]


async def create_tag(session: AsyncSession, body: TagCreate) -> TagOut:
    name = body.name.strip()
    if await _tag_taken(session, name):
        raise Conflict(f"Tag '{name}' already exists")
    tag = Tag(name=name, color=body.color)
    session.add(tag)
    await session.flush()
    return TagOut.model_validate(tag)


async def update_tag(session: AsyncSession, tag_id: int, body: TagUpdate) -> TagOut:
    tag = await session.get(Tag, tag_id)
    if tag is None:
        raise NotFound("Tag not found")
    if body.name is not None:
        name = body.name.strip()
        if name.lower() != tag.name.lower() and await _tag_taken(session, name, except_id=tag.id):
            raise Conflict(f"Tag '{name}' already exists")
        tag.name = name
    if body.color is not None:
        tag.color = body.color
    await session.flush()
    return TagOut.model_validate(tag)


async def delete_tag(session: AsyncSession, tag_id: int) -> None:
    tag = await session.get(Tag, tag_id)
    if tag is None:
        raise NotFound("Tag not found")
    await session.delete(tag)  # job_tags rows cascade
    await session.flush()


async def set_job_tags(session: AsyncSession, job_id: int, tag_names: list[str]) -> list[TagOut]:
    if await session.get(Job, job_id) is None:
        raise NotFound("Job not found")
    wanted: dict[str, str] = {}
    for raw in tag_names:
        name = raw.strip()
        if name and name.lower() not in wanted:
            wanted[name.lower()] = name
    tags: list[Tag] = []
    if wanted:
        existing = {
            t.name.lower(): t
            for t in (await session.scalars(select(Tag).where(func.lower(Tag.name).in_(list(wanted))))).all()
        }
        for low, name in wanted.items():
            tag = existing.get(low)
            if tag is None:
                tag = Tag(name=name)
                session.add(tag)
                await session.flush()
            tags.append(tag)
    await session.execute(delete(JobTag).where(JobTag.job_id == job_id))
    for tag in tags:
        session.add(JobTag(job_id=job_id, tag_id=tag.id))
    await session.flush()
    return [TagOut.model_validate(t) for t in sorted(tags, key=lambda t: t.name.lower())]
