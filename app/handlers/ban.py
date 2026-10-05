"""``/ban``, ``/unban``, ``/banned_list`` (``TASK_PLAN.md`` §M0-09.5).

The legacy handlers kept bans in a JSON file next to the process, so a ban did
not survive a redeploy and left the panel client enabled (the proxy kept
working). Here the status lives in ``users.status`` and the matching panel client
is disabled/enabled through the gateway:

* ``/ban`` → ``blocked`` + ``set_enabled(False)``;
* ``/unban`` → ``approved`` + ``set_enabled(True)`` **only** while the
  subscription has not expired (§0.2);
* staff can never be banned (``permissions.get_role``), and every change writes
  an audit row.
"""

from __future__ import annotations

import logging
from typing import Any

from app import texts
from app.container import Container
from app.db.models import UserStatus
from app.handlers.common import alert_staff, parse_target, reply
from app.permissions import Permission, get_role, require
from app.utils.text import esc

logger = logging.getLogger(__name__)


@require(Permission.USERS_BAN)
async def ban_command(message: Any, bot: Any, container: Container) -> None:
    """Ban ``/ban <tg_id>``: block the user and disable their panel client."""
    tg_id = int(message.from_user.id)
    chat_id = int(message.chat.id)
    parts = (getattr(message, "text", None) or "").split()

    target = parse_target(parts)
    if target is None:
        await reply(bot, chat_id, texts.BAN_USAGE, parse_mode="HTML")
        return

    # Preserve the legacy rule (main.py:88-93): staff are never bannable.
    if await get_role(target) is not None:
        await reply(bot, chat_id, texts.BAN_STAFF_REFUSED)
        return

    users = container.users
    if users is None:
        await reply(bot, chat_id, texts.ERROR_GENERIC)
        return

    role = await get_role(tg_id)
    change = await users.ban(
        target, actor=tg_id, role=None if role is None else str(role)
    )
    if not change.found:
        await reply(bot, chat_id, texts.ERROR_UNKNOWN_USER)
        return

    await _notice(container, target, texts.BAN_USER_NOTICE)
    confirmation = texts.BAN_DONE.format(tg_id=target)
    if change.panel_error:
        confirmation += "\n\n" + texts.PANEL_WARNING.format(
            error=esc(change.panel_error)
        )
        await alert_staff(container, "ban", change.panel_error)
    await reply(bot, chat_id, confirmation, parse_mode="HTML")


@require(Permission.USERS_BAN)
async def unban_command(message: Any, bot: Any, container: Container) -> None:
    """Unban ``/unban <tg_id>``: restore access, re-enable only if not expired."""
    tg_id = int(message.from_user.id)
    chat_id = int(message.chat.id)
    parts = (getattr(message, "text", None) or "").split()

    target = parse_target(parts)
    if target is None:
        await reply(bot, chat_id, texts.UNBAN_USAGE, parse_mode="HTML")
        return

    users = container.users
    if users is None:
        await reply(bot, chat_id, texts.ERROR_GENERIC)
        return

    current = await users.status(target)
    if current is None:
        await reply(bot, chat_id, texts.ERROR_UNKNOWN_USER)
        return

    # An ``approved`` user whose panel client is still disabled is the leftover of
    # a ``/unban`` whose panel call failed: retry instead of "not banned".
    retry_panel = current != UserStatus.BLOCKED and await users.needs_panel_retry(
        target
    )
    if current != UserStatus.BLOCKED and not retry_panel:
        await reply(bot, chat_id, texts.UNBAN_NOT_BANNED)
        return

    role = await get_role(tg_id)
    change = await users.unban(
        target, actor=tg_id, role=None if role is None else str(role)
    )
    await _notice(container, target, texts.UNBAN_USER_NOTICE)

    template = (
        texts.UNBAN_EXPIRED if change.client_action == "skipped" else texts.UNBAN_DONE
    )
    confirmation = template.format(tg_id=target)
    if change.panel_error:
        confirmation += "\n\n" + texts.PANEL_WARNING.format(
            error=esc(change.panel_error)
        )
        await alert_staff(container, "unban", change.panel_error)
    await reply(bot, chat_id, confirmation, parse_mode="HTML")


@require(Permission.USERS_BAN)
async def banned_list_command(message: Any, bot: Any, container: Container) -> None:
    """List every blocked user id (from the DB, not from a JSON file)."""
    chat_id = int(message.chat.id)
    users = container.users
    if users is None:
        await reply(bot, chat_id, texts.ERROR_GENERIC)
        return

    banned = await users.banned()
    if not banned:
        await reply(bot, chat_id, texts.BANNED_LIST_EMPTY)
        return

    body = "\n".join(f"<code>{tg_id}</code>" for tg_id in banned)
    await reply(
        bot, chat_id, f"{texts.BANNED_LIST_HEADER}\n\n{body}", parse_mode="HTML"
    )


async def _notice(container: Container, target: int, text: str) -> None:
    """Best-effort direct message to the affected user (never raises)."""
    notifier = container.notifier
    if notifier is not None:
        await notifier.safe_send(int(target), text, parse_mode="HTML")


def register_ban_handler(bot: Any, container: Container) -> None:
    """Register the ban commands on ``bot``."""

    @bot.message_handler(commands=["ban"])
    async def _ban(message: Any) -> None:  # pragma: no cover - thin adapter
        await ban_command(message, bot, container)

    @bot.message_handler(commands=["unban"])
    async def _unban(message: Any) -> None:  # pragma: no cover - thin adapter
        await unban_command(message, bot, container)

    @bot.message_handler(commands=["banned_list"])
    async def _banned_list(message: Any) -> None:  # pragma: no cover - adapter
        await banned_list_command(message, bot, container)
