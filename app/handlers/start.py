"""``/start`` + ``/help`` — registration and the main menu (§M0-09.1, §S1-4).

``/start`` is the only place a ``users`` row is created (the context middleware
just refreshes an existing one) and the only place a panel client is created for
a new user. A panel outage must still produce an answer: the handler replies with
a friendly message **and** alerts staff instead of dying silently (B7).

Since S1-4 both commands end with the same inline main menu: ``/start`` keeps its
welcome/returning copy and appends :data:`~app.texts.START_MENU`, ``/help`` shows
the command reference — one :func:`menu_keyboard` for both, so the two can never
drift apart. The menu buttons are plain ``prf:`` sections, so the menu needs no
namespace of its own and cannot shadow the dashboard's callback handler.
"""

from __future__ import annotations

import logging
from typing import Any

from telebot.util import quick_markup

from app import texts
from app.callbacks import ProfileNav
from app.container import Container
from app.errors import PanelError
from app.handlers.common import alert_staff, reply

logger = logging.getLogger(__name__)


def menu_keyboard() -> Any:
    """Build the main inline menu (§S1-4.5).

    Every button is a ``prf:`` section that already exists on the dashboard
    card (``ProfileNav`` declares all four), so the menu adds no callback
    namespace and no button is dead while its flow is still to come.
    """
    return quick_markup(
        {
            texts.BUTTON_MENU_PROFILE: {"callback_data": ProfileNav("profile").pack()},
            texts.BUTTON_MENU_PAY: {"callback_data": ProfileNav("pay").pack()},
            texts.BUTTON_MENU_INSTR: {"callback_data": ProfileNav("instr").pack()},
            texts.BUTTON_MENU_SUPPORT: {"callback_data": ProfileNav("support").pack()},
        },
        row_width=2,
    )


async def start_command(message: Any, bot: Any, container: Container) -> None:
    """Create/refresh the user row, ensure the panel client, always reply."""
    from_user = message.from_user
    tg_id = int(from_user.id)
    chat_id = int(message.chat.id)
    username = getattr(from_user, "username", None)
    first_name = getattr(from_user, "first_name", None)

    users = container.users
    if users is None:
        await reply(bot, chat_id, texts.ERROR_GENERIC)
        return

    try:
        result = await users.register(tg_id, username=username, first_name=first_name)
    except PanelError as exc:
        # B7: reply to the user *and* tell the owners — never a bare traceback.
        logger.warning("panel failure in /start for %s: %s", tg_id, exc)
        await reply(bot, chat_id, texts.ERROR_PANEL)
        await alert_staff(container, "start", exc)
        return

    greeting = texts.START_RETURNING if result.returning else texts.START_WELCOME
    await reply(
        bot,
        chat_id,
        f"{greeting}\n\n{texts.START_MENU}",
        reply_markup=menu_keyboard(),
        parse_mode="HTML",
    )


async def help_command(message: Any, bot: Any, container: Container) -> None:
    """Show the command reference and the same menu ``/start`` shows (§S1-4.6)."""
    await reply(
        bot,
        int(message.chat.id),
        texts.HELP_TEXT,
        reply_markup=menu_keyboard(),
        parse_mode="HTML",
    )


def register_start_handler(bot: Any, container: Container) -> None:
    """Register ``/start`` on ``bot``."""

    @bot.message_handler(commands=["start"])
    async def _start(message: Any) -> None:  # pragma: no cover - thin adapter
        await start_command(message, bot, container)


def register_help_handler(bot: Any, container: Container) -> None:
    """Register ``/help`` on ``bot``."""

    @bot.message_handler(commands=["help"])
    async def _help(message: Any) -> None:  # pragma: no cover - thin adapter
        await help_command(message, bot, container)


__all__ = [
    "help_command",
    "menu_keyboard",
    "register_help_handler",
    "register_start_handler",
    "start_command",
]
