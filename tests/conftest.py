"""Shared pytest fixtures for the DB layer (``TASK_PLAN.md`` §M0-02.20)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.db import models  # noqa: F401  (register tables)
from app.db.base import Base
from app.db.session import attach_sqlite_pragmas
from app.settings import Settings


@pytest.fixture
def settings() -> Settings:
    """Minimal valid :class:`Settings` for panel/service tests."""
    return Settings(
        _env_file=None,
        bot_token="123456:AA-test",
        vpn_token="vpn-token-value",
        domain="https://panel.example.com/",
        sub_url_base="https://panel.example.com/sub/",
        inbound_id=1,
        owner_ids=[1],
    )


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """In-memory SQLite engine.

    ``StaticPool`` keeps a single connection alive so every session sees the
    same schema/data (otherwise each connection gets its own empty DB).
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    attach_sqlite_pragmas(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """An ``AsyncSession`` bound to the in-memory engine."""
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
