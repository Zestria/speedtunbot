"""Handler registration (``TASK_PLAN.md`` §M0-09.6).

Every handler ported in M0-09 lives here as a ``register_<name>_handler(bot,
container)`` factory plus a module-level ``async def`` command function that
tests can call directly (§M0-09.7).

``/pay`` was the last legacy handler; M0-10 ported it onto the payments service
(:mod:`app.handlers.payment`), so the top-level ``handlers`` package and its
``config``/``loads`` imports are gone entirely.
"""

from __future__ import annotations

from typing import Any

from app.container import Container
from app.handlers.admin import register_admin_handler
from app.handlers.ban import register_ban_handler
from app.handlers.broadcast import register_broadcast_handler
from app.handlers.confirm import register_confirm_handler
from app.handlers.maintenance import register_maintenance_handler
from app.handlers.payment import register_payment_handler
from app.handlers.profile import register_profile_handler
from app.handlers.start import register_help_handler, register_start_handler
from app.handlers.support import register_support_handler

__all__ = [
    "register_admin_handler",
    "register_all_handlers",
    "register_ban_handler",
    "register_broadcast_handler",
    "register_confirm_handler",
    "register_help_handler",
    "register_maintenance_handler",
    "register_payment_handler",
    "register_profile_handler",
    "register_start_handler",
    "register_support_handler",
]


def register_all_handlers(bot: Any, container: Container) -> None:
    """Register every ported handler on ``bot`` (order does not matter)."""
    register_start_handler(bot, container)
    register_help_handler(bot, container)
    register_profile_handler(bot, container)
    register_confirm_handler(bot, container)
    register_payment_handler(bot, container)
    register_support_handler(bot, container)
    register_broadcast_handler(bot, container)
    register_ban_handler(bot, container)
    register_maintenance_handler(bot, container)
    register_admin_handler(bot, container)
