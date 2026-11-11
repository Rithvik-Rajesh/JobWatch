"""Statuses API. Owner: catalog agent."""

from fastapi import APIRouter

from app.core.auth import CurrentUser
from app.core.db import SessionDep
from app.statuses import service
from app.statuses.schemas import StatusCreate, StatusOut, StatusReorder, StatusUpdate

router = APIRouter(prefix="/statuses", tags=["statuses"])


@router.get("", response_model=list[StatusOut])
async def list_statuses(session: SessionDep, user: CurrentUser, include_archived: bool = False):
    return await service.list_statuses(session, include_archived)


@router.post("", response_model=StatusOut, status_code=201)
async def create_status(body: StatusCreate, session: SessionDep, user: CurrentUser):
    return await service.create_status(session, body)


@router.patch("/{status_id}", response_model=StatusOut)
async def update_status(status_id: int, body: StatusUpdate, session: SessionDep, user: CurrentUser):
    """System statuses: label/color/show_on_board editable; category is immutable (409)."""
    return await service.update_status(session, status_id, body)


@router.post("/reorder", response_model=list[StatusOut])
async def reorder_statuses(body: StatusReorder, session: SessionDep, user: CurrentUser):
    return await service.reorder_statuses(session, body)


@router.post("/{status_id}/archive", response_model=StatusOut)
async def archive_status(status_id: int, session: SessionDep, user: CurrentUser):
    """Custom statuses only, and only when no application currently uses it (409 otherwise)."""
    return await service.archive_status(session, status_id)
