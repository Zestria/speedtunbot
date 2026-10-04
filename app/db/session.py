"""Async engine / sessionmaker factory and SQLite pragma handling.

Every connection applies ``journal_mode=WAL``, ``foreign_keys=ON`` and
``busy_timeout=5000`` (``TASK_PLAN.md`` §2.4).
"""

from __future__ import annotations

import logging
import os

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

logger = logging.getLogger(__name__)

_SQLITE_PREFIX = "sqlite+aiosqlite:///"

# (pragma, value) applied on every new DBAPI connection.
PRAGMAS: tuple[tuple[str, str], ...] = (
    ("journal_mode", "WAL"),
    ("foreign_keys", "ON"),
    ("busy_timeout", "5000"),
)


def ensure_sqlite_dir(database_url: str) -> None:
    """Create the parent directory for a file-based SQLite database.

    SQLite never creates directories, so ``data/bot.db`` fails on a fresh
    checkout until this runs (``TASK_PLAN.md`` §M0-02.21).
    """
    if not database_url.startswith(_SQLITE_PREFIX):
        return
    path = database_url[len(_SQLITE_PREFIX) :]
    if not path or path == ":memory:" or path.startswith("file:"):
        return
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def attach_sqlite_pragmas(engine: AsyncEngine) -> None:
    """Apply the project PRAGMAs whenever a new connection is established."""

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_connection: object, _record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        try:
            for pragma, value in PRAGMAS:
                cursor.execute(f"PRAGMA {pragma}={value}")
        finally:
            cursor.close()


def create_engine_and_sessionmaker(
    database_url: str,
    *,
    echo: bool = False,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """Create the async engine and a session factory for ``database_url``."""
    engine = create_async_engine(database_url, echo=echo, future=True)
    attach_sqlite_pragmas(engine)
    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    return engine, session_factory
