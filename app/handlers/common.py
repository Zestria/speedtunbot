"""Shared helpers for the handlers ported in M0-09.

The ported handlers are module-level ``async def`` functions with an explicit
``(message, bot, container)`` signature; ``register_*`` only wraps them into
telebot handlers. That keeps them callable from tests without a live telebot
poller (``TASK_PLAN.md`` §M0-09.7).

Everything here is best-effort by design: a reply or a staff alert must never
raise into a handler (``TASK_PLAN.md`` §0.1 rules 6-7).
"""

from __future__ import annotations

import logging
from typing import Any

from app import texts
from app.container import Container
from app.utils.text import esc

logger = logging.getLogger(__name__)


async def reply(bot: Any, chat_id: int, text: str, **kwargs: Any) -> bool:
    """Send ``text`` to ``chat_id``; return ``False`` instead of raising."""
    try:
        await bot.send_message(chat_id, text, **kwargs)
    except Exception:  # user unreachable — never break the handler
        logger.warning("failed to send reply to %s", chat_id, exc_info=True)
        return False
    return True


async def alert_staff(container: Container, context: str, error: object) -> None:
    """Alert owners that ``context`` failed with ``error`` (HTML, escaped)."""
    notifier = container.notifier
    if notifier is None:
        return
    message = texts.PANEL_ALERT.format(context=esc(context), error=esc(str(error)))
    try:
        await notifier.alert_staff(message, kind="alert", parse_mode="HTML")
    except Exception:  # pragma: no cover - alerting must never raise
        logger.warning("failed to alert staff about %s", context, exc_info=True)


def parse_target(parts: list[str]) -> int | None:
    """Return ``parts[1]`` as a positive Telegram ID, or ``None``.

    Shared by ``/ban``, ``/unban`` and ``/support_user`` so all three reject a
    missing or non-numeric argument the same way.
    """
    if len(parts) < 2 or not parts[1].isdigit():
        return None
    return int(parts[1])
