"""Audit-log repository (``audit_log`` table, ``TASK_PLAN.md`` §2.4).

Append-only writer plus a couple of read helpers. ``details`` is stored through
:class:`~app.db.base.JSONText`, so callers pass plain dicts/lists and get them
back decoded.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog


async def add(
    session: AsyncSession,
    *,
    actor_tg_id: int,
    actor_role: str,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    details: Any = None,
) -> AuditLog:
    """Append one audit row and return it."""
    row = AuditLog(
        actor_tg_id=int(actor_tg_id),
        actor_role=str(actor_role),
        action=str(action),
        target_type=None if target_type is None else str(target_type),
        target_id=None if target_id is None else str(target_id),
        details=details,
    )
    session.add(row)
    await session.flush()
    return row


async def list_recent(session: AsyncSession, limit: int = 50) -> list[AuditLog]:
    """Return the newest audit rows, newest first."""
    result = await session.execute(
        select(AuditLog).order_by(AuditLog.id.desc()).limit(int(limit))
    )
    return list(result.scalars().all())
