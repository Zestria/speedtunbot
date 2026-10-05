"""User repository: thin query helpers (``TASK_PLAN.md`` §M0-02.17)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select, update
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


async def claim_pending(
    session: AsyncSession,
    tg_id: int,
    status: UserStatus,
    *,
    changed_by: int | None = None,
    note: str | None = None,
) -> bool:
    """Atomically move a ``pending`` row to ``status`` (§S3-2.2).

    Returns ``True`` only for the caller whose ``UPDATE`` matched a row. Two
    admins clicking «Принять» at the same moment both issue this statement, but
    the database serialises the writes, so exactly one sees ``rowcount == 1``:
    the claim *is* the race guard, no read-then-write window exists.

    A row that is already ``approved``/``rejected``/``blocked`` matches nothing
    and the caller is told it lost (``False``).
    """
    result = await session.execute(
        update(User)
        .where(User.tg_id == int(tg_id), User.status == UserStatus.PENDING)
        .values(
            status=str(status),
            status_note=note,
            status_changed_at=utcnow(),
            status_changed_by=changed_by,
        )
    )
    await session.flush()
    return int(getattr(result, "rowcount", 0)) == 1


async def list_pending(session: AsyncSession) -> list[User]:
    """Return every ``pending`` access request, oldest first (§S3-2.7)."""
    result = await session.execute(
        select(User).where(User.status == UserStatus.PENDING).order_by(User.tg_id)
    )
    return list(result.scalars().all())


async def list_approved(session: AsyncSession) -> list[User]:
    """Return every approved (active) user, oldest first."""
    result = await session.execute(
        select(User).where(User.status == UserStatus.APPROVED).order_by(User.tg_id)
    )
    return list(result.scalars().all())


async def list_blocked(session: AsyncSession) -> list[User]:
    """Return every blocked (banned) user, oldest first."""
    result = await session.execute(
        select(User).where(User.status == UserStatus.BLOCKED).order_by(User.tg_id)
    )
    return list(result.scalars().all())


async def list_all(session: AsyncSession) -> list[User]:
    """Return **every** user regardless of status, oldest first (§S2-2.1).

    The admin users list shows all statuses, so this is the unfiltered variant
    of :func:`list_approved`/:func:`list_blocked`; ordering by ``tg_id`` keeps
    the oldest (lowest id) accounts on the first page.
    """
    result = await session.execute(select(User).order_by(User.tg_id))
    return list(result.scalars().all())


async def search(session: AsyncSession, query: str) -> list[User]:
    """Find users by Telegram id or username (§S2-2.1).

    A digits-only ``query`` is matched against ``tg_id`` exactly; anything else
    is matched against ``username`` case-insensitively (a leading ``@`` is
    stripped). No match yields ``[]``. Ordering matches :func:`list_all`.
    """
    needle = (query or "").strip()
    if needle.startswith("@"):
        needle = needle[1:]
    if not needle:
        return []
    if needle.isdigit():
        statement = select(User).where(User.tg_id == int(needle))
    else:
        statement = select(User).where(func.lower(User.username) == needle.lower())
    result = await session.execute(statement.order_by(User.tg_id))
    return list(result.scalars().all())


async def list_broadcast_ids(session: AsyncSession) -> list[int]:
    """Return broadcast recipients: ``approved`` and not blocking the bot (§M0-09.4).

    Only ``tg_id`` is selected and materialised as a plain ``int`` list, so the
    caller can close the session before iterating: a detached ORM instance would
    drag a lazy load (and therefore a connection) into the send loop.
    """
    result = await session.execute(
        select(User.tg_id)
        .where(User.status == UserStatus.APPROVED, User.bot_blocked.is_(False))
        .order_by(User.tg_id)
    )
    return [int(tg_id) for tg_id in result.scalars().all()]


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


async def counts_by_status(session: AsyncSession) -> dict[str, int]:
    """Return ``{status: row_count}`` for the ``users`` table (§S2-1.4).

    One grouped ``COUNT`` instead of loading every row, so the dashboard can
    size the audience cheaply. Statuses with no rows are absent from the map.
    """
    result = await session.execute(
        select(User.status, func.count()).group_by(User.status)
    )
    return {str(status): int(count) for status, count in result.all()}


def to_dict(user: User) -> dict[str, Any]:
    """Small helper for logging/tests: serialise the public fields."""
    return {
        "tg_id": user.tg_id,
        "username": user.username,
        "first_name": user.first_name,
        "status": user.status,
        "bot_blocked": user.bot_blocked,
    }
