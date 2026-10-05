"""Per-role Telegram command menus (``TASK_PLAN.md`` §S4-1).

The AC is "the Telegram ``/`` menu differs per role", so the tests pin the three
tables and then drive the real startup publisher against a recording bot: the
default scope carries the user menu, every staff member (owners from settings,
everyone else from the ``admins`` table) gets a per-chat scope of their role, and
a chat Telegram refuses does not stop the remaining menus.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.commands import (
    ADMIN_COMMANDS,
    STAFF_COMMANDS,
    USER_COMMANDS,
    commands_for,
    setup_bot_commands,
)
from app.container import Container
from app.db.models import Admin
from app.permissions import Role
from tests.fakes import FakeBot

OWNER = 1
ADMIN = 7
SUPPORT = 8
USER = 500


def names(role: Role | None) -> list[str]:
    """Return the command names ``role`` sees, in menu order."""
    return [command.command for command in commands_for(role)]


def pairs(role: Role | None) -> list[tuple[str, str]]:
    """Return ``(command, description)`` pairs as :class:`FakeBot` records them."""
    return [(command.command, command.description) for command in commands_for(role)]


async def seed_admin(
    factory: async_sessionmaker[AsyncSession], tg_id: int, role: Role
) -> None:
    """Grant ``tg_id`` a non-owner role from the ``admins`` table."""
    async with factory() as session:
        session.add(Admin(tg_id=tg_id, role=str(role), added_by=OWNER))
        await session.commit()


# --- the three tables (§S4-1) -----------------------------------------------


def test_default_scope_is_the_user_menu() -> None:
    """A plain user gets ``/start /profile /pay /support /help`` and nothing more."""
    assert names(None) == list(USER_COMMANDS)
    assert names(None)[0] == "start"
    for command in commands_for(None):
        assert command.description
        assert 1 <= len(command.command) <= 32
        assert 3 <= len(command.description) <= 256


def test_every_role_extends_the_previous_one() -> None:
    """Each table is exactly the extra commands that role unlocks (§2.5)."""
    user, support, admin = names(None), names(Role.SUPPORT), names(Role.ADMIN)

    assert set(support) - set(user) == set(STAFF_COMMANDS)
    assert set(admin) - set(support) == set(ADMIN_COMMANDS)
    assert set(user) <= set(support) <= set(admin)
    assert user != support != admin


def test_owner_menu_equals_the_admin_menu() -> None:
    """§S4-1: "owner same as admin" — owners hold no command of their own."""
    assert names(Role.OWNER) == names(Role.ADMIN)
    assert "admin" in names(Role.SUPPORT)


# --- startup publishing (§S4-1) ---------------------------------------------


async def test_setup_publishes_default_then_one_scope_per_staff(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The default scope plus a per-chat menu for every owner and admin row."""
    await seed_admin(session_factory, ADMIN, Role.ADMIN)
    await seed_admin(session_factory, SUPPORT, Role.SUPPORT)

    await setup_bot_commands(fake_bot, handler_container)

    assert fake_bot.command_menus[0] == (None, pairs(None))
    assert fake_bot.commands_to(OWNER) == pairs(Role.OWNER)
    assert fake_bot.commands_to(ADMIN) == pairs(Role.ADMIN)
    assert fake_bot.commands_to(SUPPORT) == pairs(Role.SUPPORT)
    # A non-staff chat keeps the default menu: no scope was published for it.
    assert fake_bot.commands_to(USER) is None
    assert len(fake_bot.command_menus) == 4


async def test_setup_skips_a_chat_telegram_refuses(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A staff chat the bot never talked to (`400`) is skipped, not fatal."""
    await seed_admin(session_factory, ADMIN, Role.ADMIN)
    await seed_admin(session_factory, SUPPORT, Role.SUPPORT)
    fake_bot.fail_commands_for = {ADMIN}

    await setup_bot_commands(fake_bot, handler_container)

    assert fake_bot.commands_to(ADMIN) is None
    assert fake_bot.commands_to(OWNER) == pairs(Role.OWNER)
    assert fake_bot.commands_to(SUPPORT) == pairs(Role.SUPPORT)
