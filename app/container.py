"""Explicit dependency container.

Replaces the ``loads.py`` import-time singletons with an object that is built
once during startup and passed to the handler factories as ``c``
(``TASK_PLAN.md`` §2.1 / §2.8).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.settings import Settings


@dataclass
class Container:
    """Shared, mutable application state assembled at startup."""

    settings: Settings
    # Cached bot username used to build deep links, e.g.
    # ``https://t.me/<bot_username>?start=...`` (§2.8 step 3). Populated from
    # ``bot.get_me()`` exactly once during startup.
    bot_username: str | None = None
    engine: AsyncEngine | None = field(default=None)
    sessionmaker: async_sessionmaker[AsyncSession] | None = field(default=None)

    @asynccontextmanager
    async def db(self) -> AsyncIterator[AsyncSession]:
        """Yield a session bound to one transaction (unit of work).

        Commits on clean exit, rolls back on any exception so each use case is
        atomic (``TASK_PLAN.md`` §M0-02.14).
        """
        if self.sessionmaker is None:
            raise RuntimeError("Container.sessionmaker is not configured")
        session = self.sessionmaker()
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def dispose(self) -> None:
        """Dispose the database engine (called on shutdown)."""
        if self.engine is not None:
            await self.engine.dispose()
