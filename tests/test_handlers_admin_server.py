"""Server screen and Xray restart tests (``TASK_PLAN.md`` §S2-5).

Covers the S2-5 acceptance criteria: ``render_server`` draws every bar/summary
line for a full payload and silently drops the lines whose field is missing, the
``adm:server`` screen renders on ``FakePanel`` and degrades to a warning card
when the panel is down, the restart button is owner-only, and the restart itself
is confirm-gated (a forged ``rs`` from a non-owner is denied, and the confirmed
action restarts, audits and reports).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import texts
from app.callbacks import Confirm, unpack
from app.container import Container
from app.db.models import Admin
from app.db.repositories import audit as audit_repo
from app.handlers.admin import server as server_screen
from app.handlers.admin.nav import admin_callback
from app.handlers.confirm import confirm_callback
from app.permissions import Role
from tests.fakes import FakeBot, FakePanel

OWNER = 1
ADMIN = 7
CHAT = 99

#: A complete ``/server/status`` payload (§S2-5.3).
FULL_STATUS: dict[str, Any] = {
    "cpu": 18.0,
    "mem": {"current": 52, "total": 100},
    "disk": {"current": 31, "total": 100},
    "uptime": 1_051_200,  # 12 д. 4 ч
    "xray": {"state": "running", "version": "1.8"},
    "online": 14,
}


def callback(
    data: str, *, tg_id: int = OWNER, message_id: int = 500
) -> SimpleNamespace:
    """Minimal Telegram ``CallbackQuery``: only the fields the handlers read."""
    return SimpleNamespace(
        id=f"cb-{message_id}",
        data=data,
        from_user=SimpleNamespace(id=tg_id, username="neo", first_name="Neo"),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=CHAT), message_id=message_id, photo=None
        ),
    )


def payloads_of(markup: Any) -> list[str]:
    """Return the callback payloads of an inline keyboard, row by row."""
    return [button.callback_data for row in markup.keyboard for button in row]


def labels_of(markup: Any) -> list[str]:
    """Return the button labels of an inline keyboard, row by row."""
    return [button.text for row in markup.keyboard for button in row]


async def seed_staff(
    factory: async_sessionmaker[AsyncSession], tg_id: int, role: Role
) -> None:
    """Grant ``tg_id`` a non-owner role from the ``admins`` table."""
    async with factory() as session:
        session.add(Admin(tg_id=tg_id, role=str(role), added_by=OWNER))
        await session.commit()


# --- rendering (§S2-5.3) ----------------------------------------------------


def test_render_server_full_payload() -> None:
    text = server_screen.render_server(FULL_STATUS)

    assert "CPU" in text and "18%" in text
    assert "RAM" in text and "52%" in text
    assert "Диск" in text and "31%" in text
    assert "Аптайм: 12 д. 4 ч" in text
    assert "Xray: ✅ v1.8" in text
    assert "онлайн: 14" in text


def test_render_server_drops_absent_fields() -> None:
    """AC: a partial payload omits the lines whose field is missing."""
    text = server_screen.render_server({"cpu": 18.0, "online": 3})

    assert "CPU" in text and "18%" in text
    assert "RAM" not in text
    assert "Диск" not in text
    assert "Аптайм" not in text
    assert "Xray" not in text
    assert "онлайн: 3" in text


def test_render_server_empty_payload_is_just_the_header() -> None:
    assert server_screen.render_server({}) == "🖥 <b>Сервер</b>\n"


# --- screen (§S2-5.4) -------------------------------------------------------


async def test_server_screen_renders_on_fake_panel(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    await admin_callback(callback("adm:server"), fake_bot, handler_container)

    text = fake_bot.edits[-1][2]
    markup = fake_bot.edits[-1][3]["reply_markup"]
    assert "🖥 <b>Сервер" in text
    assert "онлайн: 0" in text
    assert "♻️ Перезапустить Xray" in labels_of(markup)


async def test_server_screen_warns_when_the_panel_is_down(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    panel = handler_container.panel
    assert isinstance(panel, FakePanel)
    panel.unavailable = True

    await admin_callback(callback("adm:server"), fake_bot, handler_container)

    assert server_screen.PANEL_DOWN in fake_bot.edits[-1][2]


async def test_restart_button_is_owner_only(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await seed_staff(session_factory, ADMIN, Role.ADMIN)

    await admin_callback(
        callback("adm:server", tg_id=ADMIN), fake_bot, handler_container
    )

    assert "🖥 <b>Сервер" in fake_bot.edits[-1][2]
    assert "♻️ Перезапустить Xray" not in labels_of(
        fake_bot.edits[-1][3]["reply_markup"]
    )


async def test_forged_restart_is_denied_for_non_owner(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``admin`` holds ``server.view`` but not ``server.restart`` (§S2-5.5)."""
    await seed_staff(session_factory, ADMIN, Role.ADMIN)

    await admin_callback(
        callback("adm:server:rs", tg_id=ADMIN), fake_bot, handler_container
    )

    assert fake_bot.edits == []
    # The screen re-checks ``server.restart`` and answers with the denial alert;
    # the ``adm:`` dispatcher then answers ``None`` on the way out.
    assert ("cb-500", "Недостаточно прав", True) in fake_bot.callback_answers


# --- restart flow (§S2-5.5) -------------------------------------------------


async def test_restart_is_confirmed_then_applied(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server_screen, "RESTART_SETTLE_S", 0)
    panel = handler_container.panel
    assert isinstance(panel, FakePanel)
    assert panel.xray_running is True

    # The button only mints a token and shows the confirm card; no write yet.
    await admin_callback(callback("adm:server:rs"), fake_bot, handler_container)

    assert "Перезапустить Xray?" in fake_bot.edits[-1][2]
    assert "restart_xray" not in panel.calls
    confirm_payload = payloads_of(fake_bot.edits[-1][3]["reply_markup"])[0]
    assert isinstance(unpack(confirm_payload), Confirm)

    # Pressing ✅ Подтвердить runs the registered ``cf:`` action.
    await confirm_callback(
        callback(confirm_payload, message_id=501), fake_bot, handler_container
    )

    assert "restart_xray" in panel.calls
    assert panel.xray_running is False
    assert server_screen.RESTART_DONE in fake_bot.edits[-1][2]
    assert fake_bot.callback_answers[-1] == (
        "cb-501",
        server_screen.RESTART_DONE,
        False,
    )
    async with session_factory() as session:
        actions = [row.action for row in await audit_repo.list_recent(session)]
    assert actions == [server_screen.AUDIT_RESTART]


async def test_restart_failure_reports_the_panel_error(
    handler_container: Container,
    fake_bot: FakeBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server_screen, "RESTART_SETTLE_S", 0)
    panel = handler_container.panel
    assert isinstance(panel, FakePanel)

    await admin_callback(callback("adm:server:rs"), fake_bot, handler_container)
    confirm_payload = payloads_of(fake_bot.edits[-1][3]["reply_markup"])[0]
    panel.unavailable = True  # the restart route now fails

    await confirm_callback(
        callback(confirm_payload, message_id=502), fake_bot, handler_container
    )

    assert fake_bot.edits[-1][2] == texts.ERROR_PANEL
    assert fake_bot.callback_answers[-1] == ("cb-502", texts.ERROR_PANEL, True)
