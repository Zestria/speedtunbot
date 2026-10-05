"""Admin dashboard handlers (``TASK_PLAN.md`` §7 / Stage 2).

One package for every admin screen. Each screen is a module-level ``async def``
with the same ``(call|message, bot, container)`` shape the rest of the handlers
use, so they are testable without a live poller. Callbacks all share the ``adm:``
namespace (:class:`~app.callbacks.AdminNav`), so this package registers exactly
one message handler and one callback handler for the panel itself; screens hook
themselves into the :data:`~app.handlers.admin.nav.SCREENS` registry as their task
lands. A review queue that owns a *second* callback namespace (``acc:``) registers
that one on top, next to the shared dispatcher.
"""

from __future__ import annotations

from typing import Any

from app.container import Container

# Importing the screen registers ``adm:payments`` in ``nav.SCREENS`` (§S2-4.2).
from app.handlers.admin import access as access_screen  # noqa: F401
from app.handlers.admin import payments as payments_screen  # noqa: F401

# Same for ``adm:server`` plus the ``cf:`` restart action (§S2-5.4/.5).
from app.handlers.admin import server as server_screen  # noqa: F401

# Importing the settings screen registers ``adm:settings`` (§S2-7.1) and the card
# registers its ``adm:users:…`` actions (§S2-3.4).
from app.handlers.admin import (
    settings_screen,  # noqa: F401
    user_card,
)
from app.handlers.admin import users as users_screen
from app.handlers.admin.home import admin_command, show_dashboard
from app.handlers.admin.nav import NAMESPACE, admin_callback

__all__ = [
    "NAMESPACE",
    "admin_callback",
    "admin_command",
    "register_admin_handler",
    "show_dashboard",
]


def register_admin_handler(bot: Any, container: Container) -> None:
    """Register ``/admin`` and the single ``adm:`` callback dispatcher."""

    @bot.message_handler(commands=["admin"])
    async def _admin(message: Any) -> None:  # pragma: no cover - thin adapter
        await admin_command(message, bot, container)

    @bot.callback_query_handler(
        func=lambda call: (getattr(call, "data", "") or "").startswith(NAMESPACE)
    )
    async def _callback(call: Any) -> None:  # pragma: no cover - thin adapter
        await admin_callback(call, bot, container)

    # Importing the module registered the ``adm:users`` screen; this adds the
    # ``admin_search`` message handler it owns (§S2-2.8).
    users_screen.register_users_handler(bot, container)

    # Same for the card: its ``admin_grant`` prompt owns one message handler,
    # while the buttons ride the shared ``adm:`` callback above (§S2-3.6).
    user_card.register_user_card_handler(bot, container)

    # The settings screen owns the ``admin_bank`` free-text prompt (§S2-7.3);
    # its ``adm:settings`` buttons ride the shared callback above.
    settings_screen.register_settings_handler(bot, container)

    # The access screen owns the ``acc:`` review namespace (§S3-2.6); its
    # ``adm:access`` buttons ride the shared callback above.
    access_screen.register_access_handler(bot, container)
