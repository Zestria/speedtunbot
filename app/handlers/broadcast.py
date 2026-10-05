"""``/broadcast`` — announce to every active user (``TASK_PLAN.md`` §M0-09.4).

Fixes B3: the legacy handler sent the summary **inside** the loop, so every
recipient produced another copy. Here the loop only sends, and exactly one
summary is sent afterwards.

Recipients come from the database (``approved`` and not ``bot_blocked``) instead
of the panel client list, and every send goes through
:meth:`Notifier.safe_send` (§2.9) with 0.1 s pacing. The text is escaped (B9).
(Replaced by the job-based version in M2-06.)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app import texts
from app.container import Container
from app.handlers.common import reply
from app.permissions import Permission, require
from app.utils.text import esc

logger = logging.getLogger(__name__)

#: Delay between two sends, to stay well inside Telegram's rate limits (§M0-09.4).
PACING = 0.1


@require(Permission.BROADCAST_SEND)
async def broadcast_command(message: Any, bot: Any, container: Container) -> None:
    """Send ``/broadcast <text>`` to all active users, then one summary."""
    chat_id = int(message.chat.id)
    text = (getattr(message, "text", None) or "").partition(" ")[2].strip()
    if not text:
        await reply(bot, chat_id, texts.BROADCAST_USAGE, parse_mode="HTML")
        return

    users = container.users
    notifier = container.notifier
    if users is None or notifier is None:
        await reply(bot, chat_id, texts.ERROR_GENERIC)
        return

    targets = await users.broadcast_targets()
    body = f"{texts.BROADCAST_HEADER}\n\n{esc(text)}"

    sent = 0
    for target in targets:
        if await notifier.safe_send(target, body, parse_mode="HTML"):
            sent += 1
        await asyncio.sleep(PACING)

    # B3: the summary is sent exactly once, after the loop.
    await reply(
        bot,
        chat_id,
        texts.BROADCAST_SUMMARY.format(sent=sent, failed=len(targets) - sent),
        parse_mode="HTML",
    )


def register_broadcast_handler(bot: Any, container: Container) -> None:
    """Register ``/broadcast`` on ``bot``."""

    @bot.message_handler(commands=["broadcast"])
    async def _broadcast(message: Any) -> None:  # pragma: no cover - thin adapter
        await broadcast_command(message, bot, container)
