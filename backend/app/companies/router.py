"""Companies API. Owner: catalog agent. Paths/models are the frozen contract.
Scraping endpoints (detect/test/scrape) are wired in Phase 3; in Phase 2 `scrape` only records a
queued scrape_run and `detect` handles Workday URL recognition without network calls."""

from typing import Annotated

from fastapi import APIRouter, File, Query, UploadFile

from app.companies import csv_import, service
from app.companies.schemas import (
    CompanyCreate,
    CompanyListItem,
    CompanyOut,
    CompanyUpdate,
    CsvImportPreview,
    CsvImportResult,
    DiscoveryHistoryPoint,
    ScrapeRunOut,
)
from app.core.auth import CurrentUser
from app.core.db import SessionDep
from app.core.pagination import Page, PageDep
from app.models.enums import ExtractorKind, HealthStatus

router = APIRouter(prefix="/companies", tags=["companies"])


@router.get("", response_model=Page[CompanyListItem])
async def list_companies(
    session: SessionDep,
    user: CurrentUser,
    paging: PageDep,
    q: str | None = None,
    monitoring: bool | None = None,
    health: Annotated[list[HealthStatus], Query()] = [],
    extractor: Annotated[list[ExtractorKind], Query()] = [],
    include_archived: bool = False,
    sort: str = "name_asc",
):
    return await service.list_companies(
        session,
        paging,
        q=q,
        monitoring=monitoring,
        health=health,
        extractor=extractor,
        include_archived=include_archived,
        sort=sort,
    )


@router.post("", response_model=CompanyOut, status_code=201)
async def create_company(body: CompanyCreate, session: SessionDep, user: CurrentUser):
    return await service.create_company(session, body)


@router.post("/import", response_model=CsvImportPreview)
async def import_preview(session: SessionDep, user: CurrentUser, file: UploadFile = File(...)):
    """Columns: name, careers_url, website_url, monitoring_enabled, aliases (`;`-separated). Header row
    required; unknown columns ignored. Matches existing companies by slug/name/alias -> update."""
    return await csv_import.preview_csv(session, await file.read())


@router.post("/import/{token}/commit", response_model=CsvImportResult)
async def import_commit(token: str, session: SessionDep, user: CurrentUser):
    return await csv_import.commit_csv(session, token)


@router.get("/{company_id}", response_model=CompanyOut)
async def get_company(company_id: int, session: SessionDep, user: CurrentUser):
    return await service.get_company(session, company_id)


@router.patch("/{company_id}", response_model=CompanyOut)
async def update_company(company_id: int, body: CompanyUpdate, session: SessionDep, user: CurrentUser):
    return await service.update_company(session, company_id, body)


@router.post("/{company_id}/archive", response_model=CompanyOut)
async def archive_company(company_id: int, session: SessionDep, user: CurrentUser):
    """Soft delete: disables monitoring, hides from lists. Jobs and history are kept."""
    return await service.archive_company(session, company_id)


@router.post("/{company_id}/unarchive", response_model=CompanyOut)
async def unarchive_company(company_id: int, session: SessionDep, user: CurrentUser):
    return await service.unarchive_company(session, company_id)


@router.post("/{company_id}/scrape", response_model=ScrapeRunOut, status_code=202)
async def trigger_scrape(company_id: int, session: SessionDep, user: CurrentUser):
    """Manual run. Idempotent while a run is queued/running for this company (returns that run)."""
    return await service.trigger_scrape(session, company_id)


@router.get("/{company_id}/runs", response_model=Page[ScrapeRunOut])
async def company_runs(company_id: int, session: SessionDep, user: CurrentUser, paging: PageDep):
    return await service.company_runs(session, company_id, paging)


@router.get("/{company_id}/history", response_model=list[DiscoveryHistoryPoint])
async def company_history(
    company_id: int, session: SessionDep, user: CurrentUser, days: int = Query(default=90, ge=1, le=730)
):
    return await service.company_history(session, company_id, days)
