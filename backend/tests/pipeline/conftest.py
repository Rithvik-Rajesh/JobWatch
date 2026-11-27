import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.discovery import adapters
from tests.pipeline.fakes import FakeAdapter


@pytest.fixture
def session_factory(connection):
    """Sessions for run_site: same rolled-back transaction as the test (commits are savepoint releases)."""
    return async_sessionmaker(bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint")


@pytest.fixture
def fake_adapter(monkeypatch):
    monkeypatch.setitem(adapters._REGISTRY, "custom:fake", "tests.pipeline.fakes:FakeAdapter")
    FakeAdapter.reset()
    yield FakeAdapter
    FakeAdapter.reset()
