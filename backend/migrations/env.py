import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

import app.models  # noqa: F401  (registers all tables on Base.metadata)
from app.core.config import get_settings
from app.core.db import Base

target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    """Procrastinate (migration 0004) owns the procrastinate_* tables; keep them out of autogenerate."""
    if type_ == "table":
        return not (name or "").startswith("procrastinate_")
    table = getattr(obj, "table", None)
    return not (table is not None and table.name.startswith("procrastinate_"))


def _url() -> str:
    return context.config.get_main_option("sqlalchemy.url") or get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection) -> None:
    context.configure(
        connection=connection, target_metadata=target_metadata, compare_type=True, include_object=include_object
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_url())
    async with engine.connect() as connection:
        await connection.run_sync(_do_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
