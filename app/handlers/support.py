"""``/support`` + ``/support_user`` (``TASK_PLAN.md`` §M0-09.3, fixes B9).

Behaviour is preserved from the legacy handlers (two FSM states), with two
changes:

* every user-controlled fragment goes through :func:`~app.utils.text.esc`, so a
  message containing ``<``/``&`` can no longer make Telegram reject the whole
  fan-out (B9);
* the fan-out uses :mod:`app.permissions` (``staff_with``) plus
  :meth:`Notifier.send_to`, i.e. the safe-send path (B2) instead of raw
  ``bot.send_message`` loops.
"""

from __future__ import annotations

import logging
from typing import Any

from app import texts
from app.container import Container
from app.handlers.common import parse_target, reply
from app.permissions import Permission, require, staff_with
from app.states import UserStates
from app.utils.text import esc

logger = logging.getLogger(__name__)


def _who(message: Any) -> str:
    """Return the HTML-safe ``@username`` / ``ID …`` header of a support card."""
    from_user = message.from_user
    username = getattr(from_user, "username", None)
    if username:
        return f"@{esc(username)}\nID: {int(from_user.id)}"
    return f"ID: {int(from_user.id)}"


async def support_command(message: Any, bot: Any, container: Container) -> None:
    """Toggle the "waiting for help" state for a user."""
    tg_id = int(message.from_user.id)
    chat_id = int(message.chat.id)

    if await bot.get_state(tg_id, chat_id) == UserStates.waiting_for_help.name:
        await bot.delete_state(tg_id, chat_id)
        await reply(bot, chat_id, texts.SUPPORT_EXITED)
        return

    await bot.set_state(tg_id, UserStates.waiting_for_help, chat_id)
    await reply(bot, chat_id, texts.SUPPORT_ENTERED, parse_mode="HTML")


async def support_message(message: Any, bot: Any, container: Container) -> None:
    """Fan the user's support text out to staff holding ``support.reply``."""
    text = getattr(message, "text", None) or ""
    card = texts.SUPPORT_CARD.format(who=_who(message), text=esc(text))
    recipients = await staff_with(Permission.SUPPORT_REPLY, "notify_support")
    notifier = container.notifier
    if notifier is None or not recipients:
        logger.info("support message from %s delivered to nobody", message.from_user.id)
        return
    await notifier.send_to(recipients, card, parse_mode="HTML")


@require(Permission.SUPPORT_REPLY)
async def support_user_command(message: Any, bot: Any, container: Container) -> None:
    """Put staff into the "writing to a user" state (``/support_user <tg_id>``)."""
    tg_id = int(message.from_user.id)
    chat_id = int(message.chat.id)
    parts = (getattr(message, "text", None) or "").split()

    if (
        await bot.get_state(tg_id, chat_id) == UserStates.writing_to_user.name
        and len(parts) < 2
    ):
        await bot.delete_state(tg_id, chat_id)
        await reply(bot, chat_id, texts.SUPPORT_USER_EXITED)
        return

    target = parse_target(parts)
    if target is None:
        await reply(bot, chat_id, texts.SUPPORT_USER_USAGE, parse_mode="HTML")
        return

    await bot.set_state(tg_id, UserStates.writing_to_user, chat_id)
    async with bot.retrieve_data(tg_id, chat_id) as data:
        data["target_user_id"] = target
    await reply(bot, chat_id, texts.SUPPORT_USER_ENTERED.format(tg_id=target))


async def support_relay(message: Any, bot: Any, container: Container) -> None:
    """Deliver a staff reply to the target user stored in the FSM data."""
    tg_id = int(message.from_user.id)
    chat_id = int(message.chat.id)

    async with bot.retrieve_data(tg_id, chat_id) as data:
        target = data.get("target_user_id")

    if not target:
        await reply(bot, chat_id, texts.ERROR_UNKNOWN_USER)
        await bot.delete_state(tg_id, chat_id)
        return

    card = texts.SUPPORT_REPLY_CARD.format(
        text=esc(getattr(message, "text", None) or "")
    )
    notifier = container.notifier
    delivered = False
    if notifier is not None:
        delivered = await notifier.safe_send(int(target), card, parse_mode="HTML")
    if not delivered:
        await reply(bot, chat_id, texts.SUPPORT_REPLY_FAILED)


def register_support_handler(bot: Any, container: Container) -> None:
    """Register the four support handlers on ``bot``."""

    @bot.message_handler(commands=["support"])
    async def _support(message: Any) -> None:  # pragma: no cover - thin adapter
        await support_command(message, bot, container)

    @bot.message_handler(state=UserStates.waiting_for_help, content_types=["text"])
    async def _process(  # pragma: no cover - thin adapter
        message: Any,
    ) -> None:
        await support_message(message, bot, container)

    @bot.message_handler(commands=["support_user"])
    async def _support_user(message: Any) -> None:  # pragma: no cover - adapter
        await support_user_command(message, bot, container)

    @bot.message_handler(state=UserStates.writing_to_user, content_types=["text"])
    async def _relay(message: Any) -> None:  # pragma: no cover - thin adapter
        await support_relay(message, bot, container)


__all__ = [
    "register_support_handler",
    "support_command",
    "support_message",
    "support_relay",
    "support_user_command",
]
