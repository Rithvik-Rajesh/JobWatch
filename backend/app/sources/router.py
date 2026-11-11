"""Job sources API. Owner: catalog agent."""

from fastapi import APIRouter

from app.core.auth import CurrentUser
from app.core.db import SessionDep
from app.sources import service
from app.sources.schemas import SourceOut, SourceUpdate

router = APIRouter(prefix="/sources", tags=["sources"])


@router.get("", response_model=list[SourceOut])
async def list_sources(session: SessionDep, user: CurrentUser):
    return await service.list_sources(session)


@router.patch("/{source_id}", response_model=SourceOut)
async def update_source(source_id: int, body: SourceUpdate, session: SessionDep, user: CurrentUser):
    return await service.update_source(session, source_id, body)
