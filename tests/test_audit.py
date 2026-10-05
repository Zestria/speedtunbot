"""Audit service tests (``TASK_PLAN.md`` §M0-08.1 / §M0-08.4).

Covers the acceptance criteria: rows land with **JSON** ``details``, and an
audit failure never propagates (``log`` returns ``False`` instead).
"""

from __future__ import annotations

import json

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.repositories import audit as audit_repo
from app.services.audit import SYSTEM_ACTOR, UNKNOWN_ROLE, AuditService

OWNER, TARGET = 1, 55


@pytest_asyncio.fixture
async def factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Sessionmaker over the shared in-memory engine."""
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def test_log_writes_row_with_json_details(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    audit = AuditService(factory)

    assert (
        await audit.log(
            OWNER,
            "payment.approve",
            "payment",
            TARGET,
            role="owner",
            amount=150,
            days=30,
        )
        is True
    )

    async with factory() as session:
        rows = await audit_repo.list_recent(session)
    assert len(rows) == 1
    row = rows[0]
    assert (row.actor_tg_id, row.actor_role, row.action) == (
        OWNER,
        "owner",
        "payment.approve",
    )
    assert (row.target_type, row.target_id) == ("payment", str(TARGET))
    assert row.details == {"amount": 150, "days": 30}

    # The column really holds JSON text (not a Python repr / pickle).
    async with factory() as session:
        result = await session.execute(text("SELECT details FROM audit_log"))
        raw = result.scalar_one()
    assert json.loads(raw) == {"amount": 150, "days": 30}


async def test_log_defaults_for_system_actions(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    audit = AuditService(factory)

    assert await audit.log(None, "job.reminders") is True

    async with factory() as session:
        row = (await audit_repo.list_recent(session))[0]
    assert row.actor_tg_id == SYSTEM_ACTOR
    assert row.actor_role == UNKNOWN_ROLE
    assert row.target_type is None
    assert row.target_id is None
    assert row.details is None


async def test_list_recent_returns_newest_first(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    audit = AuditService(factory)
    for index in range(3):
        assert await audit.log(OWNER, f"action.{index}") is True

    async with factory() as session:
        rows = await audit_repo.list_recent(session, limit=2)
    assert [row.action for row in rows] == ["action.2", "action.1"]


async def test_log_never_raises_on_db_failure() -> None:
    class BrokenSessionmaker:
        """A sessionmaker whose every call raises (simulated DB outage)."""

        def __call__(self) -> object:
            raise RuntimeError("database is down")

    audit = AuditService(BrokenSessionmaker())  # type: ignore[arg-type]

    assert await audit.log(OWNER, "payment.approve") is False


async def test_log_without_sessionmaker_is_a_noop() -> None:
    assert await AuditService(None).log(OWNER, "payment.approve") is False
