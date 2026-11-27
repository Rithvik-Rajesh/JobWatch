"""Procrastinate application (jobs queue lives in the same PostgreSQL database)."""

from procrastinate import App, PsycopgConnector

from app.core.config import get_settings


def conninfo_from_url(url: str) -> str:
    """SQLAlchemy URL (postgresql+asyncpg://...) -> libpq URL understood by psycopg."""
    scheme, sep, rest = url.partition("://")
    return f"{scheme.split('+', 1)[0]}{sep}{rest}"


def build_app() -> App:
    return App(
        connector=PsycopgConnector(conninfo=conninfo_from_url(get_settings().database_url)),
        import_paths=["app.worker.tasks"],
    )


app = build_app()
