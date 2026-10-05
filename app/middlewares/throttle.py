"""Throttle middleware: per-user in-flight guard + rate cap (§M0-05.4/§M0-05.5).

Registered **last** so that a `CancelUpdate` from Maintenance/Access cannot leave
a lock behind: telebot skips `post_process` for *every* middleware once one
cancels, so acquiring the lock after those gates means it is only ever taken on
updates that reach a handler.

Fixes B10: the in-flight guard is released in ``post_process`` (which telebot
calls even when the handler raised) and a 30 s safety TTL additionally recovers
from any leaked lock (cancelled task, shutdown mid-flight, future middleware).

Staff (``data["role"]`` is set) are exempt from the rate cap but **not** from the
in-flight guard.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any

from telebot.asyncio_handler_backends import BaseMiddleware, CancelUpdate

from app.middlewares.base import is_callback, user_id

logger = logging.getLogger(__name__)

#: A lock older than this is treated as leaked and ignored (B10 safety net).
IN_FLIGHT_TTL = 30.0
#: Sliding window / limit for the per-user rate cap.
RATE_WINDOW = 10.0
RATE_LIMIT = 20
#: Answer shown when a duplicate callback arrives while one is in flight.
WAIT_TEXT = "Подождите…"


class ThrottleMiddleware(BaseMiddleware):
    """Drop duplicate in-flight updates and cap the per-user update rate."""

    def __init__(self, *, bot: Any = None) -> None:
        self._bot = bot
        self._in_flight: dict[int, float] = {}
        self._recent: dict[int, deque[float]] = {}
        self.update_types = ["message", "callback_query"]

    async def pre_process(self, update: Any, data: dict[str, Any]) -> Any:
        tg_id = user_id(update)
        if tg_id is None:
            return None
        tg_id = int(tg_id)
        now = time.monotonic()

        if self._is_in_flight(tg_id, now):
            await self._reject_duplicate(update)
            return CancelUpdate()

        staff = data.get("role") is not None
        if not staff and self._over_cap(tg_id, now):
            return CancelUpdate()

        self._in_flight[tg_id] = now
        if not staff:
            self._recent.setdefault(tg_id, deque()).append(now)
        return None

    async def post_process(
        self, update: Any, data: dict[str, Any], exception: Exception | None
    ) -> None:
        tg_id = user_id(update)
        if tg_id is not None:
            self._in_flight.pop(int(tg_id), None)

    # --- internals ---------------------------------------------------------

    def _is_in_flight(self, tg_id: int, now: float) -> bool:
        """``True`` while a fresh lock is held (stale locks are cleared)."""
        started = self._in_flight.get(tg_id)
        if started is None:
            return False
        if now - started >= IN_FLIGHT_TTL:
            del self._in_flight[tg_id]
            return False
        return True

    def _over_cap(self, tg_id: int, now: float) -> bool:
        """``True`` when the user exceeded :data:`RATE_LIMIT` in the window."""
        window = self._recent.get(tg_id)
        if window is None:
            return False
        while window and now - window[0] > RATE_WINDOW:
            window.popleft()
        if not window:
            del self._recent[tg_id]
            return False
        return len(window) >= RATE_LIMIT

    async def _reject_duplicate(self, update: Any) -> None:
        """Answer a duplicate callback; drop a duplicate message silently."""
        if not is_callback(update):
            return
        callback_id = getattr(update, "id", None)
        if callback_id is None or self._bot is None:
            return
        try:
            await self._bot.answer_callback_query(callback_id, WAIT_TEXT)
        except Exception:  # pragma: no cover - best-effort answer
            logger.debug("failed to answer duplicate callback", exc_info=True)
