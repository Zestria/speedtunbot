"""User repository: thin query helpers (``TASK_PLAN.md`` §M0-02.17)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import User, UserStatus


async def get(session: AsyncSession, tg_id: int) -> User | None:
    """Return the user with ``tg_id`` or ``None``."""
    return await session.get(User, tg_id)


async def upsert_from_telegram(
    session: AsyncSession,
    tg_id: int,
    *,
    username: str | None = None,
    first_name: str | None = None,
) -> User:
    """Create or refresh a user row from Telegram profile data.

    An existing row is refreshed (``username``/``first_name``/``last_seen_at``)
    and its ``bot_blocked`` flag reset, because the user is clearly reachable
    again.
    """
    user = await session.get(User, tg_id)
    now = utcnow()
    if user is None:
        user = User(
            tg_id=tg_id,
            username=username,
            first_name=first_name,
            status=UserStatus.NEW,
            created_at=now,
            last_seen_at=now,
        )
        session.add(user)
    else:
        if username is not None:
            user.username = username
        if first_name is not None:
            user.first_name = first_name
        user.bot_blocked = False
        user.last_seen_at = now
    await session.flush()
    return user


async def touch(
    session: AsyncSession,
    tg_id: int,
    *,
    username: str | None = None,
    first_name: str | None = None,
) -> User | None:
    """Refresh an **existing** user row from Telegram profile data.

    Unlike :func:`upsert_from_telegram` this **never inserts**: a stranger gets
    ``None`` and no row is created (row creation happens in ``/start`` — M0-09.1,
    see ``TASK_PLAN.md`` §M0-05.3). A reachable user is clearly no longer
    blocking the bot, so ``bot_blocked`` is reset.
    """
    user = await session.get(User, tg_id)
    if user is None:
        return None
    if username is not None:
        user.username = username
    if first_name is not None:
        user.first_name = first_name
    user.bot_blocked = False
    user.last_seen_at = utcnow()
    await session.flush()
    return user


async def set_status(
    session: AsyncSession,
    tg_id: int,
    status: UserStatus,
    *,
    changed_by: int | None = None,
    note: str | None = None,
) -> User | None:
    """Set a user's access status and record who/when. Returns the user."""
    user = await session.get(User, tg_id)
    if user is None:
        return None
    user.status = str(status)
    user.status_note = note
    user.status_changed_at = utcnow()
    user.status_changed_by = changed_by
    await session.flush()
    return user


async def list_approved(session: AsyncSession) -> list[User]:
    """Return every approved (active) user, oldest first."""
    result = await session.execute(
        select(User).where(User.status == UserStatus.APPROVED).order_by(User.tg_id)
    )
    return list(result.scalars().all())


async def set_panel_client_uuid(
    session: AsyncSession,
    tg_id: int,
    client_uuid: str | None,
) -> None:
    """Attach the panel client uuid to a user row."""
    user = await session.get(User, tg_id)
    if user is not None:
        user.panel_client_uuid = client_uuid
        await session.flush()


async def set_bot_blocked(
    session: AsyncSession, tg_id: int, blocked: bool = True
) -> None:
    """Mark a user as (not) blocking the bot (Telegram 403 / "chat not found").

    Used by :meth:`app.services.notifier.Notifier.safe_send`; a missing user row
    is ignored (the chat id may belong to a stranger or a group).
    """
    user = await session.get(User, tg_id)
    if user is not None:
        user.bot_blocked = blocked
        await session.flush()


def to_dict(user: User) -> dict[str, Any]:
    """Small helper for logging/tests: serialise the public fields."""
    return {
        "tg_id": user.tg_id,
        "username": user.username,
        "first_name": user.first_name,
        "status": user.status,
        "bot_blocked": user.bot_blocked,
    }
