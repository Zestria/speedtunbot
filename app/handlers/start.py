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
from app.callbacks import AdminNav, ProfileNav
from app.container import Container
from app.errors import PanelError
from app.handlers.common import alert_staff, reply
from app.permissions import Permission, has

logger = logging.getLogger(__name__)


async def _is_staff(tg_id: int) -> bool:
    """Return ``True`` when ``tg_id`` holds ``users.view`` (§S2-1.11).

    Defensive: an RBAC context that was never configured (some unit tests) must
    not break ``/start`` — the menu simply drops the staff button.
    """
    try:
        return await has(tg_id, Permission.USERS_VIEW)
    except Exception:  # pragma: no cover - RBAC not configured
        logger.debug("staff check failed for %s", tg_id, exc_info=True)
        return False


def menu_keyboard(*, is_staff: bool = False) -> Any:
    """Build the main inline menu (§S1-4.5, §S2-1.11).

    Everyone gets the four ``prf:`` sections; staff additionally get
    ``🛠 Админ-панель`` (``adm:menu``), gated by ``users.view`` so a
    plain user never sees a button they cannot use.
    """
    buttons: dict[str, dict[str, str]] = {
        texts.BUTTON_MENU_PROFILE: {"callback_data": ProfileNav("profile").pack()},
        texts.BUTTON_MENU_PAY: {"callback_data": ProfileNav("pay").pack()},
        texts.BUTTON_MENU_INSTR: {"callback_data": ProfileNav("instr").pack()},
        texts.BUTTON_MENU_SUPPORT: {"callback_data": ProfileNav("support").pack()},
    }
    if is_staff:
        buttons[texts.BUTTON_MENU_ADMIN] = {"callback_data": AdminNav("menu").pack()}
    return quick_markup(buttons, row_width=2)


async def start_command(message: Any, bot: Any, container: Container) -> None:
    """Create/refresh the user row, ensure the panel client, always reply."""
    from_user = message.from_user
    tg_id = int(from_user.id)
    chat_id = int(message.chat.id)
    is_staff = await _is_staff(tg_id)
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
        reply_markup=menu_keyboard(is_staff=is_staff),
        parse_mode="HTML",
    )


async def help_command(message: Any, bot: Any, container: Container) -> None:
    """Show the command reference and the same menu ``/start`` shows (§S1-4.6)."""
    is_staff = await _is_staff(int(message.from_user.id))
    await reply(
        bot,
        int(message.chat.id),
        texts.HELP_TEXT,
        reply_markup=menu_keyboard(is_staff=is_staff),
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
