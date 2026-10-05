"""Access gate middleware (§S3-1).

Runs between Maintenance and Throttle (``Context → Maintenance → Access →
Throttle``) and enforces the ``users.status`` state machine:

* staff (``data["role"]`` set) and ``approved`` users pass untouched;
* ``blocked`` users are dropped **silently** — a ban must be invisible;
* **any** ``/start`` passes: the command is also the ``inv_…`` redemption entry
  point (§S3-3), so a ``pending``/``rejected``/unknown user must reach
  ``start_command`` and may redeem a valid invite;
* a ``pending``/``rejected`` user on any other update gets one rate-limited
  notice («Заявка на рассмотрении» / «Заявка отклонена») and the update is
  cancelled;
* an unknown user (no ``users`` row) on any update other than ``/start`` is
  dropped.

Notices are rate-limited in-process so a stuck user cannot be spammed.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from telebot.asyncio_handler_backends import BaseMiddleware, CancelUpdate

from app import texts
from app.db.models import UserStatus
from app.middlewares.base import is_start_command, user_id

logger = logging.getLogger(__name__)

#: One «заявка на рассмотрении» notice per user per 10 minutes (§S3-1).
PENDING_INTERVAL = 600.0
#: One «заявка отклонена» notice per user per hour (§S3-1).
REJECTED_INTERVAL = 3600.0


class AccessMiddleware(BaseMiddleware):
    """Cancel updates that must not reach a handler, with rate-limited notices."""

    def __init__(
        self,
        *,
        bot: Any = None,
        pending_interval: float = PENDING_INTERVAL,
        rejected_interval: float = REJECTED_INTERVAL,
    ) -> None:
        self._bot = bot
        self._pending_interval = pending_interval
        self._rejected_interval = rejected_interval
        #: ``(tg_id, status)`` → monotonic time of the last notice sent.
        self._last_notice: dict[tuple[int, str], float] = {}
        self.update_types = ["message", "callback_query"]

    async def pre_process(self, update: Any, data: dict[str, Any]) -> Any:
        if data.get("role") is not None:
            return None
        user = data.get("user")
        status = None if user is None else getattr(user, "status", None)
        if status == UserStatus.BLOCKED:
            return CancelUpdate()
        # Any /start passes: a stranger/pending/rejected user may redeem an invite.
        if is_start_command(update):
            return None
        if user is None:
            # Unknown user: only /start may create a row (M0-05.3 / S3-1).
            return CancelUpdate()
        if status == UserStatus.APPROVED:
            return None

        tg_id = user_id(update)
        if status == UserStatus.PENDING:
            await self._notify(
                tg_id, "pending", texts.ACCESS_PENDING, self._pending_interval
            )
        elif status == UserStatus.REJECTED:
            await self._notify(
                tg_id, "rejected", texts.ACCESS_REJECTED, self._rejected_interval
            )
        return CancelUpdate()

    async def post_process(
        self, update: Any, data: dict[str, Any], exception: Exception | None
    ) -> None:
        return None

    # --- internals ---------------------------------------------------------

    async def _notify(
        self, tg_id: int | None, key: str, message: str, interval: float
    ) -> None:
        """Send ``message`` at most once per ``interval`` per ``(tg_id, key)``."""
        if tg_id is None or self._bot is None:
            return
        if not self._should_notice(int(tg_id), key, interval):
            return
        try:
            await self._bot.send_message(int(tg_id), message)
        except Exception:  # pragma: no cover - best-effort notice
            logger.debug("failed to send access notice", exc_info=True)

    def _should_notice(self, tg_id: int, key: str, interval: float) -> bool:
        """Return ``True`` while the notice for ``(tg_id, key)`` is due."""
        now = time.monotonic()
        last = self._last_notice.get((tg_id, key))
        if last is not None and now - last < interval:
            return False
        self._last_notice[(tg_id, key)] = now
        return True
