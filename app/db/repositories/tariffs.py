"""Tariff repository and seed helper (``TASK_PLAN.md`` §M0-02.18)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Tariff


@dataclass(frozen=True)
class TariffSeed:
    """Seed definition for one tariff."""

    name: str
    days: int
    price: int
    sort_order: int


# Seeded from the legacy ``RATES`` hardcode (``TASK_PLAN.md`` §2.3).
DEFAULT_TARIFFS: tuple[TariffSeed, ...] = (
    TariffSeed("30 дней - 150 рублей", 30, 150, 1),
    TariffSeed("60 дней - 250 рублей", 60, 250, 2),
    TariffSeed("90 дней - 350 рублей", 90, 350, 3),
)


async def list_all(session: AsyncSession, *, active_only: bool = False) -> list[Tariff]:
    """List tariffs ordered by ``sort_order``."""
    stmt = select(Tariff).order_by(Tariff.sort_order, Tariff.id)
    if active_only:
        stmt = stmt.where(Tariff.is_active.is_(True))
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get(session: AsyncSession, tariff_id: int) -> Tariff | None:
    """Return a tariff by primary key."""
    return await session.get(Tariff, tariff_id)


async def seed_defaults(
    session: AsyncSession,
    tariffs: tuple[TariffSeed, ...] | None = None,
) -> int:
    """Insert the default tariffs if the table is empty. Idempotent.

    Returns the number of rows inserted (0 if the table already had rows).
    """
    total = await session.scalar(select(func.count()).select_from(Tariff))
    if total:
        return 0
    effective = tariffs if tariffs is not None else DEFAULT_TARIFFS
    for seed in effective:
        session.add(
            Tariff(
                name=seed.name,
                days=seed.days,
                price=seed.price,
                is_active=True,
                sort_order=seed.sort_order,
            )
        )
    await session.flush()
    return len(effective)
