"""``/start`` — registration (``TASK_PLAN.md`` §M0-09.1, fixes B7).

``/start`` is the only place a ``users`` row is created (the context middleware
just refreshes an existing one) and the only place a panel client is created for
a new user. A panel outage must still produce an answer: the handler replies with
a friendly message **and** alerts staff instead of dying silently (B7).
"""

from __future__ import annotations

import logging
from typing import Any

from app import texts
from app.container import Container
from app.errors import PanelError
from app.handlers.common import alert_staff, reply

logger = logging.getLogger(__name__)


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

    await reply(
        bot,
        chat_id,
        texts.START_RETURNING if result.returning else texts.START_WELCOME,
    )


def register_start_handler(bot: Any, container: Container) -> None:
    """Register ``/start`` on ``bot``."""

    @bot.message_handler(commands=["start"])
    async def _start(message: Any) -> None:  # pragma: no cover - thin adapter
        await start_command(message, bot, container)
