"""Seed idempotency tests (``TASK_PLAN.md`` §M0-02.20)."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Setting, Tariff
from app.db.repositories import settings as settings_repo
from app.db.repositories import tariffs as tariffs_repo
from app.db.seeds import seed_all


async def _count(session: AsyncSession, model: type) -> int:
    return await session.scalar(select(func.count()).select_from(model)) or 0


async def test_seed_all_then_again_is_idempotent(session: AsyncSession) -> None:
    await seed_all(session)
    await session.commit()
    tariffs_after_first = await _count(session, Tariff)
    settings_after_first = await _count(session, Setting)

    # Second run inserts nothing.
    await seed_all(session)
    await session.commit()

    assert await _count(session, Tariff) == tariffs_after_first
    assert await _count(session, Setting) == settings_after_first
    assert tariffs_after_first == len(tariffs_repo.DEFAULT_TARIFFS)
    assert settings_after_first == len(settings_repo.DEFAULT_SETTINGS)


async def test_seed_defaults_sets_expected_values(session: AsyncSession) -> None:
    await settings_repo.seed_defaults(session)
    await session.commit()
    assert await settings_repo.get(session, "access_mode") == "approval"
    assert await settings_repo.get(session, "maintenance_mode") is False
    assert await settings_repo.get(session, "reminders_enabled") is True


async def test_seed_does_not_overwrite_existing_value(session: AsyncSession) -> None:
    await settings_repo.set_value(session, "access_mode", "open")
    await session.commit()

    await settings_repo.seed_defaults(session)
    await session.commit()

    assert await settings_repo.get(session, "access_mode") == "open"


async def test_tariff_seed_prices(session: AsyncSession) -> None:
    await tariffs_repo.seed_defaults(session)
    await session.commit()
    tariffs = await tariffs_repo.list_all(session)
    assert [(t.days, t.price) for t in tariffs] == [(30, 150), (60, 250), (90, 350)]
