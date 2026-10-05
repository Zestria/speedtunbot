"""Legacy handler package — **``/pay`` only** (``TASK_PLAN.md`` §M0-09.6).

``/start``, ``/profile``, ``/support``, ``/support_user``, ``/broadcast`` and the
ban commands were ported to :mod:`app.handlers` in M0-09, and the empty
``handlers/ban.py`` plus the JSON banned-file logic were deleted with them. What
is left is the legacy payment flow, which M0-10 replaces with the DB-backed
payments service; ``app/app.py`` registers it explicitly until then.
"""

from __future__ import annotations

from telebot.async_telebot import AsyncTeleBot

from handlers.payment import register_payment_handler

__all__ = ["register_payment_handler"]


def register_legacy_handlers(bot: AsyncTeleBot) -> None:
    """Register the legacy handlers that are not ported yet (``/pay``)."""
    register_payment_handler(bot)

