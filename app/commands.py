"""Per-role Telegram command menus (``TASK_PLAN.md`` §S4-1).

Telegram builds the "/" menu from the bot's *command list*, and that list is
scoped: :class:`~telebot.types.BotCommandScopeDefault` covers every chat, while a
:class:`~telebot.types.BotCommandScopeChat` published for one chat overrides it
for that chat alone. §2.5 hands each role a different set of sections, so each
staff member gets a scope of their own:

* everyone — ``/start /profile /pay /support /help``;
* any staff role — plus ``/admin`` (``users.view``) and ``/support_user``
  (``support.reply``), which the ``support`` role holds too;
* ``admin`` and ``owner`` — plus ``/invite /broadcast /ban /unban /banned_list
  /maintenance``.

Only commands that are **actually registered** appear here: a menu entry that
opens nothing is the command-menu twin of a dead button (§S2-1).
"""

from __future__ import annotations

import logging
from typing import Any

from telebot.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from app.container import Container
from app.permissions import Permission, Role

logger = logging.getLogger(__name__)

#: ``command -> description``; the copy mirrors the ``/help`` reference so the
#: menu and the message can never describe a command differently.
DESCRIPTIONS: dict[str, str] = {
    "start": "🚀 Главное меню",
    "profile": "👤 Подписка, трафик и ссылка",
    "pay": "💳 Оплата тарифов",
    "support": "💬 Поддержка",
    "help": "ℹ️ Помощь",
    "admin": "🛠 Админ-панель",
    "support_user": "✉️ Написать пользователю",
    "invite": "📨 Приглашения",
    "broadcast": "📢 Рассылка",
    "ban": "🚫 Забанить",
    "unban": "✅ Разбанить",
    "banned_list": "📋 Список забаненных",
    "maintenance": "🔧 Режим обслуживания",
}

#: The default scope: what every user sees (§S4-1).
USER_COMMANDS: tuple[str, ...] = ("start", "profile", "pay", "support", "help")
#: Added for every staff role — ``users.view`` / ``support.reply`` (§2.5).
STAFF_COMMANDS: tuple[str, ...] = ("admin", "support_user")
#: Added for ``admin`` and ``owner`` (both hold the whole staff permission set).
ADMIN_COMMANDS: tuple[str, ...] = (
    "broadcast",
    "ban",
    "unban",
    "banned_list",
    "maintenance",
    "invite",
)


def commands_for(role: Role | None) -> list[BotCommand]:
    """Return the command menu ``role`` should see (§S4-1).

    ``None`` is a plain user: any other caller is staff by definition
    (:class:`Role` has no non-staff member), so the staff entries come first and
    ``admin``/``owner`` add the moderation ones on top.
    """
    names = list(USER_COMMANDS)
    if role is not None:
        names += list(STAFF_COMMANDS)
    if role in (Role.ADMIN, Role.OWNER):
        names += list(ADMIN_COMMANDS)
    return [BotCommand(name, DESCRIPTIONS[name]) for name in names]


async def setup_bot_commands(bot: Any, container: Container) -> None:
    """Publish the default menu and one scope per staff chat (§S4-1).

    Called once at startup. Owners come from ``OWNER_IDS``, everyone else from
    the ``admins`` table; ``users.view`` is held by **every** staff role (§2.5),
    so enumerating it yields exactly the set that may open ``/admin`` and
    therefore needs its own menu.
    """
    await bot.set_my_commands(commands_for(None), scope=BotCommandScopeDefault())
    admins = container.admins
    if admins is None:  # pragma: no cover - container is wired at startup
        return
    for tg_id in await admins.staff_with_all(Permission.USERS_VIEW):
        await _publish_chat(bot, tg_id, await admins.get_role(tg_id))


async def _publish_chat(bot: Any, tg_id: int, role: Role | None) -> None:
    """Publish ``tg_id``'s menu, skipping chats Telegram refuses.

    A per-chat scope is only accepted for a chat the user already started, so a
    staff member who never wrote to the bot raises ``400 chat not found`` here;
    that must not abort the rest of the menus, let alone startup.
    """
    try:
        await bot.set_my_commands(
            commands_for(role), scope=BotCommandScopeChat(chat_id=int(tg_id))
        )
    except Exception:
        logger.warning("command menu for %s not published", tg_id, exc_info=True)
    else:
        logger.debug("command menu for %s published (%s)", tg_id, role or "user")


__all__ = [
    "ADMIN_COMMANDS",
    "DESCRIPTIONS",
    "STAFF_COMMANDS",
    "USER_COMMANDS",
    "commands_for",
    "setup_bot_commands",
]
