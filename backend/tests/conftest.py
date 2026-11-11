"""Integration-test harness — real PostgreSQL, real migrations, real HTTP stack (no mocks).

* A dedicated database `jobwatch_test` is (re)created once per session and migrated with Alembic.
* Every test runs inside an outer transaction that is rolled back afterwards; the app's sessions
  join it via SAVEPOINTs (`join_transaction_mode="create_savepoint"`), so services can commit/flush
  normally and tests stay isolated and fast.
* `client` is an httpx AsyncClient against the ASGI app, already logged in, sending the CSRF header.
"""

import asyncio
import os
from collections.abc import AsyncIterator

import pytest
from argon2 import PasswordHasher
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker, create_async_engine

ADMIN_BASE_URL = os.environ.get(
    "JOBWATCH_TEST_ADMIN_URL", "postgresql+asyncpg://jobwatch:jobwatch@127.0.0.1:5433/jobwatch"
)
TEST_DB = "jobwatch_test"
TEST_URL = ADMIN_BASE_URL.rsplit("/", 1)[0] + f"/{TEST_DB}"
TEST_PASSWORD = "test-password"

os.environ["JOBWATCH_ENV"] = "test"
os.environ["JOBWATCH_DATABASE_URL"] = TEST_URL
os.environ["JOBWATCH_ADMIN_USERNAME"] = "admin"
os.environ["JOBWATCH_ADMIN_PASSWORD_HASH"] = PasswordHasher().hash(TEST_PASSWORD)
os.environ["JOBWATCH_SESSION_SECRET"] = "test-session-secret"
os.environ["JOBWATCH_CAPTURE_TOKEN"] = "test-capture-token"


def _run_migrations() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "migrations"))
    cfg.set_main_option("sqlalchemy.url", TEST_URL)
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session")
async def database() -> AsyncIterator[str]:
    admin = create_async_engine(ADMIN_BASE_URL, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f"drop database if exists {TEST_DB} with (force)"))
        await conn.execute(text(f"create database {TEST_DB}"))
    await admin.dispose()
    await asyncio.to_thread(_run_migrations)
    yield TEST_URL


@pytest.fixture(scope="session")
async def engine(database: str):
    eng = create_async_engine(database)
    yield eng
    await eng.dispose()


@pytest.fixture
async def connection(engine) -> AsyncIterator[AsyncConnection]:
    async with engine.connect() as conn:
        trans = await conn.begin()
        yield conn
        await trans.rollback()


@pytest.fixture
async def session(connection: AsyncConnection) -> AsyncIterator[AsyncSession]:
    """Session for arranging/asserting DB state directly in tests (shares the rolled-back txn)."""
    maker = async_sessionmaker(bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint")
    async with maker() as s:
        yield s


@pytest.fixture
async def app(connection: AsyncConnection):
    from app.core import db as core_db
    from app.main import create_app

    maker = async_sessionmaker(bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint")
    application = create_app()

    async def _session():
        async with maker() as s:
            try:
                yield s
                await s.commit()
            except BaseException:
                await s.rollback()
                raise

    application.dependency_overrides[core_db.get_session] = _session
    return application


@pytest.fixture
async def anon_client(app) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


@pytest.fixture
async def client(app) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"X-JobWatch-Client": "web"},
    ) as c:
        r = await c.post("/api/v1/auth/login", json={"username": "admin", "password": TEST_PASSWORD})
        assert r.status_code == 200, r.text
        yield c
