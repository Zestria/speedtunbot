"""``adm:`` callback dispatcher (§S2-1).

One handler serves the whole admin namespace: ``unpack`` a typed
:class:`~app.callbacks.AdminNav`, route its ``section`` to a registered screen,
and always answer the callback query (B5). Screens register themselves with
:func:`register_screen` as their task lands, and :data:`SCREENS` doubles as the
"is this button live yet?" check used by the dashboard keyboard.

The gate here is only a **view** gate (``USERS_VIEW`` etc.). Mutation actions
live *inside* their screen handler and re-check their own, stronger permission
(``USERS_EDIT``/``USERS_BAN``/``USERS_DELETE`` …) — hiding a button is cosmetic,
so a forged mutation callback must be refused by the handler that performs the
write, never by this dispatcher.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app import texts
from app.callbacks import AdminNav, unpack
from app.container import Container
from app.errors import InvalidCallback
from app.permissions import Permission, check_callback, get_role

logger = logging.getLogger(__name__)

#: Callback-data namespace owned by this package.
NAMESPACE = f"{AdminNav.ns}:"

#: Signature every registered screen shares: ``(call, bot, container, payload)``.
Screen = Callable[..., Awaitable[None]]

#: ``section`` → screen handler. Populated by :func:`register_screen`.
SCREENS: dict[str, Screen] = {}

#: Permission required to *view* each section (actions re-check their own).
SECTION_PERMISSIONS: dict[str, Permission] = {
    "users": Permission.USERS_VIEW,
    "payments": Permission.PAYMENTS_VIEW,
    "server": Permission.SERVER_VIEW,
    "broadcast": Permission.BROADCAST_SEND,
    "settings": Permission.SETTINGS_EDIT,
}


def register_screen(section: str) -> Callable[[Screen], Screen]:
    """Register ``func`` as the handler of the ``adm:<section>`` screen."""

    def decorate(func: Screen) -> Screen:
        SCREENS[section] = func
        return func

    return decorate


async def admin_callback(call: Any, bot: Any, container: Container) -> None:
    """Dispatch an ``adm:`` callback to its screen, else answer a stale toast."""
    try:
        payload = unpack(getattr(call, "data", None))
    except InvalidCallback:
        await _answer(bot, call, texts.ERROR_STALE_BUTTON)
        return
    if not isinstance(payload, AdminNav):
        await _answer(bot, call, texts.ERROR_STALE_BUTTON)
        return

    section = payload.section
    if section in ("menu", "back"):
        await _show_menu(call, bot, container)
        await _answer(bot, call, None)
        return

    # Authorise first, then route: a forged section must be refused with the
    # denial alert, never reveal whether a screen happens to be built yet.
    required = SECTION_PERMISSIONS.get(section)
    if required is None:
        await _answer(bot, call, texts.ERROR_STALE_BUTTON)
        return
    if not await check_callback(call, required):
        return
    screen = SCREENS.get(section)
    if screen is None:
        await _answer(bot, call, texts.ERROR_STALE_BUTTON)
        return
    await screen(call, bot, container, payload)
    await _answer(bot, call, None)


async def _show_menu(call: Any, bot: Any, container: Container) -> None:
    """Re-render the dashboard in place (lazy import avoids a home↔nav cycle)."""
    from app.handlers.admin.home import show_dashboard

    tg_id = int(getattr(call.from_user, "id", 0))
    chat_id, message_id = _target(call)
    role = await get_role(tg_id)
    await show_dashboard(
        bot, chat_id, container, tg_id, role=role, message_id=message_id
    )


def _target(call: Any) -> tuple[int, int]:
    """Return ``(chat_id, message_id)`` of the message the callback came from."""
    message = call.message
    return int(message.chat.id), int(message.message_id)


async def _answer(bot: Any, call: Any, text: str | None) -> None:
    """Answer the callback query without ever raising."""
    try:
        await bot.answer_callback_query(getattr(call, "id", None), text)
    except Exception:  # pragma: no cover - cosmetic
        logger.debug("failed to answer callback", exc_info=True)


__all__ = [
    "NAMESPACE",
    "SCREENS",
    "SECTION_PERMISSIONS",
    "admin_callback",
    "register_screen",
]
