"""Repository helper tests (``TASK_PLAN.md`` §M0-02.17/§M0-02.18)."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Setting, UserStatus
from app.db.repositories import settings as settings_repo
from app.db.repositories import users as users_repo


async def test_users_get_returns_none_for_unknown(session: AsyncSession) -> None:
    assert await users_repo.get(session, 42) is None


async def test_users_upsert_from_telegram_inserts(session: AsyncSession) -> None:
    user = await users_repo.upsert_from_telegram(
        session, 7, username="neo", first_name="Neo"
    )
    await session.commit()
    assert user.tg_id == 7
    assert user.status == UserStatus.NEW
    assert user.username == "neo"


async def test_users_upsert_refreshes_and_resets_bot_blocked(
    session: AsyncSession,
) -> None:
    await users_repo.upsert_from_telegram(session, 7, username="old")
    await session.commit()
    await session.execute(text("UPDATE users SET bot_blocked = 1 WHERE tg_id = 7"))
    await session.commit()

    user = await users_repo.upsert_from_telegram(session, 7, username="new")
    await session.commit()
    assert user.username == "new"
    assert user.bot_blocked is False


async def test_users_set_status(session: AsyncSession) -> None:
    await users_repo.upsert_from_telegram(session, 7)
    await session.commit()
    user = await users_repo.set_status(
        session, 7, UserStatus.APPROVED, changed_by=1, note="ok"
    )
    await session.commit()
    assert user is not None
    assert user.status == UserStatus.APPROVED
    assert user.status_changed_by == 1
    assert user.status_note == "ok"
    assert user.status_changed_at is not None


async def test_users_set_status_unknown_returns_none(session: AsyncSession) -> None:
    assert await users_repo.set_status(session, 123, UserStatus.APPROVED) is None


async def test_users_list_broadcast_ids_filters_and_returns_ints(
    session: AsyncSession,
) -> None:
    """``list_broadcast_ids`` returns bare ints: approved and not bot-blocked."""
    for tg_id, config in [
        (1, (UserStatus.APPROVED, False)),
        (2, (UserStatus.NEW, False)),
        (3, (UserStatus.APPROVED, True)),
        (4, (UserStatus.APPROVED, False)),
    ]:
        status, bot_blocked = config
        await users_repo.upsert_from_telegram(session, tg_id)
        await users_repo.set_status(session, tg_id, status)
        if bot_blocked:
            await users_repo.set_bot_blocked(session, tg_id, True)
    await session.commit()

    ids = await users_repo.list_broadcast_ids(session)
    assert ids == [1, 4]
    assert all(type(tg_id) is int for tg_id in ids)


async def test_users_list_approved(session: AsyncSession) -> None:
    for tg_id, status in [
        (1, UserStatus.APPROVED),
        (2, UserStatus.NEW),
        (3, UserStatus.APPROVED),
    ]:
        await users_repo.upsert_from_telegram(session, tg_id)
        await users_repo.set_status(session, tg_id, status)
    await session.commit()

    approved = await users_repo.list_approved(session)
    assert [u.tg_id for u in approved] == [1, 3]


async def test_settings_get_set(session: AsyncSession) -> None:
    session.add(Setting(key="k", value=None))
    await session.commit()
    assert await settings_repo.get(session, "k") is None

    await settings_repo.set_value(session, "k", {"a": 1}, updated_by=99)
    await session.commit()
    assert await settings_repo.get(session, "k") == {"a": 1}

    row = await session.get(Setting, "k")
    assert row is not None and row.updated_by == 99


async def test_settings_get_all(session: AsyncSession) -> None:
    await settings_repo.set_value(session, "a", 1)
    await settings_repo.set_value(session, "b", "x")
    await session.commit()
    assert await settings_repo.get_all(session) == {"a": 1, "b": "x"}
