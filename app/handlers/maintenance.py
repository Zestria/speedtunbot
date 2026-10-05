"""``/maintenance`` — toggle maintenance mode (``TASK_PLAN.md`` §M0-05.6).

Lifted out of ``handlers/legacy_commands.py`` so the runtime no longer imports
the legacy ``config``/``loads`` singletons. The legacy version flipped an
import-time module constant, which the new maintenance middleware never reads
(B1), so the toggle had no effect; here it writes the ``maintenance_mode``
setting that :class:`~app.middlewares.maintenance.MaintenanceMiddleware` reads on
every update.
"""

from __future__ import annotations

import logging
from typing import Any

from app import texts
from app.container import Container
from app.handlers.common import reply
from app.permissions import Permission, get_role, require

logger = logging.getLogger(__name__)

ACTION_SETTING = "setting.set"
SETTING_KEY = "maintenance_mode"


@require(Permission.MAINTENANCE_TOGGLE)
async def maintenance_command(message: Any, bot: Any, container: Container) -> None:
    """Flip ``maintenance_mode`` and report the new state."""
    tg_id = int(message.from_user.id)
    chat_id = int(message.chat.id)
    settings = container.settings_service
    if settings is None:
        await reply(bot, chat_id, texts.ERROR_GENERIC)
        return

    enabled = not await settings.maintenance_mode()
    await settings.set_maintenance_mode(enabled, updated_by=tg_id)

    audit = container.audit
    if audit is not None:
        role = await get_role(tg_id)
        await audit.log(
            tg_id,
            ACTION_SETTING,
            "setting",
            SETTING_KEY,
            role=None if role is None else str(role),
            value=enabled,
        )

    await reply(
        bot,
        chat_id,
        texts.MAINTENANCE_ON if enabled else texts.MAINTENANCE_OFF,
        parse_mode="HTML",
    )


def register_maintenance_handler(bot: Any, container: Container) -> None:
    """Register ``/maintenance`` on ``bot``."""

    @bot.message_handler(commands=["maintenance"])
    async def _maintenance(message: Any) -> None:  # pragma: no cover - adapter
        await maintenance_command(message, bot, container)
