"""Admin-card repository (``admin_cards`` table, ``TASK_PLAN.md`` §2.4).

One row per delivered copy of a staff card, so every copy can later be edited in
place (e.g. a payment switching to "✅ Одобрено") instead of only the copy the
acting admin happens to be looking at.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AdminCard


async def add(
    session: AsyncSession,
    *,
    kind: str,
    ref_id: int,
    chat_id: int,
    message_id: int,
) -> AdminCard:
    """Record one delivered card copy."""
    row = AdminCard(
        kind=str(kind),
        ref_id=int(ref_id),
        chat_id=int(chat_id),
        message_id=int(message_id),
    )
    session.add(row)
    await session.flush()
    return row


async def list_for(session: AsyncSession, kind: str, ref_id: int) -> list[AdminCard]:
    """Return every stored copy of the ``kind``/``ref_id`` card, oldest first."""
    result = await session.execute(
        select(AdminCard)
        .where(AdminCard.kind == str(kind), AdminCard.ref_id == int(ref_id))
        .order_by(AdminCard.id)
    )
    return list(result.scalars().all())


async def set_message_id(session: AsyncSession, card_id: int, message_id: int) -> None:
    """Point a stored copy at a new message (fallback re-send)."""
    row = await session.get(AdminCard, card_id)
    if row is not None:
        row.message_id = int(message_id)
        await session.flush()
