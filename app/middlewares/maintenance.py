"""Maintenance middleware: read the flag at call time (§M0-05.6).

Fixes B1: instead of the legacy import-time ``IS_MAINTENANCE_MODE`` constant,
the flag is read from :class:`~app.services.settings_service.SettingsService` on
**every** update, so a toggle applies to the very next update without a restart.

Staff bypass the notice. Non-staff get one notice per 10 minutes and the update
is cancelled.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from telebot.asyncio_handler_backends import BaseMiddleware, CancelUpdate

from app.middlewares.base import user_id
from app.services.settings_service import SettingsService

logger = logging.getLogger(__name__)

#: One maintenance notice per user per 10 minutes (§M0-05.6).
NOTICE_INTERVAL = 600.0
MAINTENANCE_TEXT = "🛠 Бот на техническом обслуживании. Пожалуйста, зайдите позже."


class MaintenanceMiddleware(BaseMiddleware):
    """Cancel updates from non-staff while maintenance mode is on."""

    def __init__(
        self,
        settings: SettingsService | None,
        *,
        bot: Any = None,
        notice_interval: float = NOTICE_INTERVAL,
    ) -> None:
        self._settings = settings
        self._bot = bot
        self._notice_interval = notice_interval
        self._last_notice: dict[int, float] = {}
        self.update_types = ["message", "callback_query"]

    async def pre_process(self, update: Any, data: dict[str, Any]) -> Any:
        if data.get("role") is not None:
            return None
        if self._settings is None or not await self._settings.maintenance_mode():
            return None

        tg_id = user_id(update)
        if tg_id is not None and self._should_notice(int(tg_id)):
            await self._notify(int(tg_id))
        return CancelUpdate()

    async def post_process(
        self, update: Any, data: dict[str, Any], exception: Exception | None
    ) -> None:
        return None

    # --- internals ---------------------------------------------------------

    def _should_notice(self, tg_id: int) -> bool:
        """Rate-limit the maintenance notice to once per interval per user."""
        now = time.monotonic()
        last = self._last_notice.get(tg_id)
        if last is not None and now - last < self._notice_interval:
            return False
        self._last_notice[tg_id] = now
        return True

    async def _notify(self, tg_id: int) -> None:
        if self._bot is None:
            return
        try:
            await self._bot.send_message(tg_id, MAINTENANCE_TEXT)
        except Exception:  # pragma: no cover - best-effort notice
            logger.debug("failed to send maintenance notice", exc_info=True)
