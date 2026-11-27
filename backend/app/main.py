from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.applications.router import router as applications_router
from app.companies.router import router as companies_router
from app.core import auth
from app.core.config import get_settings
from app.core.db import dispose_engine
from app.core.errors import install_error_handlers
from app.core.logging import configure_logging
from app.dashboard.router import router as dashboard_router
from app.health.router import probe_router
from app.health.router import router as health_router
from app.jobs.router import router as jobs_router
from app.reminders.router import router as reminders_router
from app.settings.router import router as settings_router
from app.sites.fetch_all import router as fetch_router
from app.sites.router import router as sites_router
from app.sources.router import router as sources_router
from app.statuses.router import router as statuses_router

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging()
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="JobWatch API", version="0.1.0", lifespan=lifespan)
    install_error_handlers(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    api = APIRouter(prefix=API_PREFIX)
    for r in (
        auth.router,
        jobs_router,
        applications_router,
        reminders_router,
        companies_router,
        sites_router,
        fetch_router,
        statuses_router,
        settings_router,
        sources_router,
        dashboard_router,
        health_router,
    ):
        api.include_router(r)
    app.include_router(api)
    app.include_router(probe_router)

    from pathlib import Path

    from fastapi.responses import FileResponse

    ui = Path(__file__).parent / "ui" / "index.html"

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(ui)

    return app


app = create_app()
