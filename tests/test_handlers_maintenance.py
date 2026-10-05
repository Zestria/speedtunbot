"""``/maintenance`` + runtime-cleanliness tests (``TASK_PLAN.md`` §M0-09.6).

``/maintenance`` moved out of ``handlers/legacy_commands.py`` into
:mod:`app.handlers.maintenance` so the runtime no longer imports the legacy
``config``/``loads`` singletons and the toggle writes the setting the middleware
actually reads (B1).

The second test pins §M0-09.6's acceptance criterion: importing the ported
handler package must not pull in ``config`` or ``loads``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import texts
from app.container import Container
from app.db.models import AuditLog
from app.handlers.maintenance import maintenance_command
from tests.fakes import FakeBot

OWNER = 1
STRANGER = 7
CHAT = 99


def message(tg_id: int, *, chat_id: int = CHAT) -> SimpleNamespace:
    return SimpleNamespace(
        from_user=SimpleNamespace(id=tg_id, username="neo", first_name="Neo"),
        chat=SimpleNamespace(id=chat_id),
        text="/maintenance",
    )


async def test_maintenance_toggle_writes_the_setting_and_audits(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The toggle persists ``maintenance_mode`` (B1) instead of a dead constant."""
    settings = handler_container.settings_service
    assert settings is not None

    await maintenance_command(message(OWNER), fake_bot, handler_container)
    assert await settings.maintenance_mode() is True
    assert fake_bot.texts_to(CHAT) == [texts.MAINTENANCE_ON]

    await maintenance_command(message(OWNER), fake_bot, handler_container)
    assert await settings.maintenance_mode() is False
    assert fake_bot.texts_to(CHAT)[-1] == texts.MAINTENANCE_OFF

    async with session_factory() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
    assert [row.action for row in rows] == ["setting.set", "setting.set"]
    assert {row.target_id for row in rows} == {"maintenance_mode"}


async def test_maintenance_is_refused_for_non_staff(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """``maintenance.toggle`` is admin+; a stranger is dropped silently."""
    await maintenance_command(message(STRANGER), fake_bot, handler_container)

    settings = handler_container.settings_service
    assert settings is not None
    assert await settings.maintenance_mode() is False
    assert fake_bot.sent == []


def test_handler_package_does_not_import_the_legacy_modules() -> None:
    """AC (M0-09.6): no ``config``/``loads`` import on the ported runtime path."""
    root = Path(__file__).resolve().parents[1]
    code = (
        "import sys, app.handlers; "
        "assert 'config' not in sys.modules, 'config imported'; "
        "assert 'loads' not in sys.modules, 'loads imported'"
    )
    subprocess.run([sys.executable, "-c", code], check=True, cwd=root)


async def test_users_service_is_wired_to_the_panel(
    handler_container: Container,
) -> None:
    """``Container.init_users`` passes the panel into the user service."""
    users = handler_container.users
    assert users is not None

    await users.approve(4242, username="neo")

    assert await handler_container.panel.get_client(4242) is not None  # type: ignore[union-attr]
