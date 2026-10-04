"""Model / constraint tests (``TASK_PLAN.md`` §M0-02.20)."""

from __future__ import annotations

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db.models import (
    AdminGrantRequest,
    Payment,
    ReminderSent,
    Setting,
    Tariff,
    User,
)

EXPECTED_TABLES = {
    "admin_cards",
    "admin_grant_requests",
    "admins",
    "audit_log",
    "broadcast_jobs",
    "invites",
    "payments",
    "reminders_sent",
    "settings",
    "tariffs",
    "users",
}


async def test_create_all_creates_every_table(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        names = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
    assert names >= EXPECTED_TABLES


async def test_partial_unique_blocks_two_active_payments(session: AsyncSession) -> None:
    session.add(User(tg_id=1001))
    await session.flush()
    session.add(
        Payment(user_tg_id=1001, tariff_name="t", days=30, price=150, status="created")
    )
    await session.commit()

    session.add(
        Payment(
            user_tg_id=1001, tariff_name="t", days=60, price=250, status="submitted"
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_partial_unique_allows_two_decided_payments(
    session: AsyncSession,
) -> None:
    session.add(User(tg_id=1002))
    await session.flush()
    session.add(
        Payment(user_tg_id=1002, tariff_name="t", days=30, price=150, status="declined")
    )
    session.add(
        Payment(user_tg_id=1002, tariff_name="t", days=60, price=250, status="declined")
    )
    await session.commit()  # no error: neither row is "active"


async def test_reminder_unique_key(session: AsyncSession) -> None:
    session.add(ReminderSent(user_tg_id=1, kind="3d", expiry_ms=111))
    await session.commit()
    session.add(ReminderSent(user_tg_id=1, kind="3d", expiry_ms=111))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_foreign_key_is_enforced(session: AsyncSession) -> None:
    # invite_id references a non-existent invite -> FK violation (pragma ON).
    session.add(
        AdminGrantRequest(invite_id=999, tg_id=5, role="support", status="pending")
    )
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_setting_json_round_trip(session: AsyncSession) -> None:
    payload = {"nested": [1, 2, 3], "flag": True}
    session.add(Setting(key="access_mode", value=payload))
    await session.commit()

    session.expire_all()
    row = await session.get(Setting, "access_mode")
    assert row is not None
    assert row.value == payload


async def test_tariff_check_constraints(session: AsyncSession) -> None:
    session.add(Tariff(name="bad", days=0, price=10))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()

    session.add(Tariff(name="bad", days=30, price=-1))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()
