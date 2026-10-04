"""Session, pragma, container and migration tests (``TASK_PLAN.md`` §M0-02)."""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.container import Container
from app.db.base import Base
from app.db.migrate import upgrade_head
from app.db.models import User
from app.db.session import (
    PRAGMAS,
    create_engine_and_sessionmaker,
    ensure_sqlite_dir,
)


async def test_pragmas_applied_on_temp_file_db() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "pragma.db"
        url = f"sqlite+aiosqlite:///{db_path}"
        engine, _ = create_engine_and_sessionmaker(url)
        try:
            async with engine.connect() as conn:
                journal = await conn.scalar(text("PRAGMA journal_mode"))
                fk = await conn.scalar(text("PRAGMA foreign_keys"))
                busy = await conn.scalar(text("PRAGMA busy_timeout"))
        finally:
            await engine.dispose()
    assert str(journal).lower() == "wal"
    assert fk == 1
    assert busy == 5000


def test_pragmas_tuple_is_documented() -> None:
    assert PRAGMAS == (
        ("journal_mode", "WAL"),
        ("foreign_keys", "ON"),
        ("busy_timeout", "5000"),
    )


def test_ensure_sqlite_dir_creates_parent(tmp_path: Path) -> None:
    target = tmp_path / "data" / "bot.db"
    ensure_sqlite_dir(f"sqlite+aiosqlite:///{target}")
    assert target.parent.is_dir()


def test_ensure_sqlite_dir_ignores_memory() -> None:
    ensure_sqlite_dir("sqlite+aiosqlite:///:memory:")  # must not raise


async def test_container_db_commits_and_rolls_back() -> None:
    engine, sessionmaker = create_engine_and_sessionmaker("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    settings = _dummy_settings()
    container = Container(settings=settings, engine=engine, sessionmaker=sessionmaker)

    async with container.db() as session:
        session.add(User(tg_id=1))
    async with container.db() as session:
        assert await session.get(User, 1) is not None

    with pytest.raises(RuntimeError):
        async with container.db() as session:
            session.add(User(tg_id=2))
            raise RuntimeError("boom")

    async with container.db() as session:
        assert await session.get(User, 2) is None

    await container.dispose()


async def test_container_db_requires_sessionmaker() -> None:
    container = Container(settings=_dummy_settings())
    with pytest.raises(RuntimeError):
        async with container.db() as session:  # noqa: F841
            pass


def test_upgrade_head_on_empty_file_creates_all_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "mig.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    upgrade_head(url)

    con = sqlite3.connect(db_path)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
    finally:
        con.close()
    assert {"users", "payments", "tariffs", "settings", "alembic_version"} <= tables


def test_upgrade_head_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "mig2.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    upgrade_head(url)
    upgrade_head(url)  # second call is a no-op, not an error


async def test_migrated_schema_matches_metadata(tmp_path: Path) -> None:
    db_path = tmp_path / "mig3.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    # upgrade_head() uses asyncio.run internally, so run it off the event loop.
    await asyncio.to_thread(upgrade_head, url)

    con = sqlite3.connect(db_path)
    try:
        migrated = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
    finally:
        con.close()
    expected = set(Base.metadata.tables)
    assert expected <= migrated
    assert "alembic_version" in migrated


def _dummy_settings():  # local import keeps the fixture light
    from app.settings import Settings

    payload = {
        "bot_token": "123456:AA-test",
        "vpn_token": "vpn-token-value",
        "domain": "https://panel.example.com/",
        "sub_url_base": "https://panel.example.com/sub/",
        "inbound_id": 1,
        "owner_ids": [1],
    }
    return Settings(_env_file=None, **payload)


def test_make_sessionmaker_is_callable() -> None:
    # Guard against a regression where the factory returns something unusable.
    _, factory = create_engine_and_sessionmaker("sqlite+aiosqlite://")
    assert isinstance(factory, async_sessionmaker)
    assert issubclass(factory.class_, AsyncSession)


def test_env_database_url_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "env.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    upgrade_head()
    assert db_path.exists()
