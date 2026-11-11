"""Statuses API. Owner: catalog agent."""

from fastapi import APIRouter

from app.core.auth import CurrentUser
from app.core.db import SessionDep
from app.core.errors import todo
from app.statuses.schemas import StatusCreate, StatusOut, StatusReorder, StatusUpdate

router = APIRouter(prefix="/statuses", tags=["statuses"])


@router.get("", response_model=list[StatusOut])
async def list_statuses(session: SessionDep, user: CurrentUser, include_archived: bool = False):
    raise todo()


@router.post("", response_model=StatusOut, status_code=201)
async def create_status(body: StatusCreate, session: SessionDep, user: CurrentUser):
    raise todo()


@router.patch("/{status_id}", response_model=StatusOut)
async def update_status(status_id: int, body: StatusUpdate, session: SessionDep, user: CurrentUser):
    """System statuses: label/color/show_on_board editable; category is immutable (409)."""
    raise todo()


@router.post("/reorder", response_model=list[StatusOut])
async def reorder_statuses(body: StatusReorder, session: SessionDep, user: CurrentUser):
    raise todo()


@router.post("/{status_id}/archive", response_model=StatusOut)
async def archive_status(status_id: int, session: SessionDep, user: CurrentUser):
    """Custom statuses only, and only when no application currently uses it (409 otherwise)."""
    raise todo()
