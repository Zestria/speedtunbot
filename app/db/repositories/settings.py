"""Key/value settings repository (``TASK_PLAN.md`` §M0-02.18)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import Setting

# Default values seeded on first run (``TASK_PLAN.md`` §2.4 `settings` keys).
DEFAULT_SETTINGS: dict[str, Any] = {
    "access_mode": "approval",
    "maintenance_mode": False,
    "bank_details": None,
    "reminders_enabled": True,
}


async def get(session: AsyncSession, key: str) -> Any:
    """Return the value for ``key`` or ``None`` when the key is absent."""
    row = await session.get(Setting, key)
    return row.value if row is not None else None


async def get_all(session: AsyncSession) -> dict[str, Any]:
    """Return every setting as a ``{key: value}`` mapping."""
    result = await session.execute(select(Setting))
    return {row.key: row.value for row in result.scalars().all()}


async def set_value(
    session: AsyncSession,
    key: str,
    value: Any,
    *,
    updated_by: int | None = None,
) -> Setting:
    """Create or update the setting ``key`` and return the row."""
    row = await session.get(Setting, key)
    if row is None:
        row = Setting(key=key, value=value, updated_at=utcnow(), updated_by=updated_by)
        session.add(row)
    else:
        row.value = value
        row.updated_at = utcnow()
        row.updated_by = updated_by
    await session.flush()
    return row


async def seed_defaults(
    session: AsyncSession,
    defaults: dict[str, Any] | None = None,
) -> int:
    """Insert missing default settings; existing keys are left untouched.

    Idempotent: running twice inserts nothing the second time. Returns the
    number of rows inserted.
    """
    effective = defaults if defaults is not None else DEFAULT_SETTINGS
    result = await session.execute(select(Setting.key))
    existing = set(result.scalars().all())
    inserted = 0
    for key, value in effective.items():
        if key in existing:
            continue
        session.add(Setting(key=key, value=value, updated_at=utcnow(), updated_by=None))
        inserted += 1
    await session.flush()
    return inserted
