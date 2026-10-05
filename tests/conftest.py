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

from app.container import Container
from app.db import models  # noqa: F401  (register tables)
from app.db.base import Base
from app.db.session import attach_sqlite_pragmas
from app.permissions import configure
from app.services.subscriptions import SubscriptionService
from app.settings import Settings
from tests.fakes import FakeBot, FakePanel


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


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """A sessionmaker over the shared in-memory engine (services, handlers)."""
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
def fake_bot() -> FakeBot:
    """Recording bot double shared by the handler and RBAC tests."""
    return FakeBot()


@pytest_asyncio.fixture
async def handler_container(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    fake_bot: FakeBot,
) -> Container:
    """Container wired like startup, but with ``FakePanel`` + ``FakeBot`` (M0-09).

    ``permissions.configure`` is called with the container's ``AdminService`` so
    the ``@require`` guard inside the ported handlers can resolve roles.
    """
    container = Container(settings=settings, sessionmaker=session_factory)
    container.init_rbac()
    container.init_settings()
    container.init_audit()
    container.init_confirmations()
    container.panel = FakePanel()
    container.subscriptions = SubscriptionService(container.panel)
    container.init_users()
    container.init_payments()
    container.notifier.attach_bot(fake_bot)  # type: ignore[union-attr]
    configure(bot=fake_bot, admins=container.admins)
    return container
