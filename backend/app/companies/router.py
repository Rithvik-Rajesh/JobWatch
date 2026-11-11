"""Companies API. Owner: catalog agent. Paths/models are the frozen contract.
Scraping endpoints (detect/test/scrape) are wired in Phase 3; in Phase 2 `scrape` only records a
queued scrape_run and `detect` handles Workday URL recognition without network calls."""

from typing import Annotated

from fastapi import APIRouter, File, Query, UploadFile

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
from app.core.errors import todo
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
    raise todo()


@router.post("", response_model=CompanyOut, status_code=201)
async def create_company(body: CompanyCreate, session: SessionDep, user: CurrentUser):
    raise todo()


@router.post("/import", response_model=CsvImportPreview)
async def import_preview(session: SessionDep, user: CurrentUser, file: UploadFile = File(...)):
    """Columns: name, careers_url, website_url, monitoring_enabled, aliases (`;`-separated). Header row
    required; unknown columns ignored. Matches existing companies by slug/name/alias -> update."""
    raise todo()


@router.post("/import/{token}/commit", response_model=CsvImportResult)
async def import_commit(token: str, session: SessionDep, user: CurrentUser):
    raise todo()


@router.get("/{company_id}", response_model=CompanyOut)
async def get_company(company_id: int, session: SessionDep, user: CurrentUser):
    raise todo()


@router.patch("/{company_id}", response_model=CompanyOut)
async def update_company(company_id: int, body: CompanyUpdate, session: SessionDep, user: CurrentUser):
    raise todo()


@router.post("/{company_id}/archive", response_model=CompanyOut)
async def archive_company(company_id: int, session: SessionDep, user: CurrentUser):
    """Soft delete: disables monitoring, hides from lists. Jobs and history are kept."""
    raise todo()


@router.post("/{company_id}/unarchive", response_model=CompanyOut)
async def unarchive_company(company_id: int, session: SessionDep, user: CurrentUser):
    raise todo()


@router.post("/{company_id}/scrape", response_model=ScrapeRunOut, status_code=202)
async def trigger_scrape(company_id: int, session: SessionDep, user: CurrentUser):
    """Manual run. Idempotent while a run is queued/running for this company (returns that run)."""
    raise todo()


@router.get("/{company_id}/runs", response_model=Page[ScrapeRunOut])
async def company_runs(company_id: int, session: SessionDep, user: CurrentUser, paging: PageDep):
    raise todo()


@router.get("/{company_id}/history", response_model=list[DiscoveryHistoryPoint])
async def company_history(
    company_id: int, session: SessionDep, user: CurrentUser, days: int = Query(default=90, ge=1, le=730)
):
    raise todo()
