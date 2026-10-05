"""Access middleware placeholder (§M0-05.7).

The real access state machine (``new``/``pending``/``rejected`` + invites)
arrives with M1-01. For now this only silently drops updates from users whose
``status`` is ``blocked`` so banned users reach no handler.
"""

from __future__ import annotations

import logging
from typing import Any

from telebot.asyncio_handler_backends import BaseMiddleware, CancelUpdate

from app.db.models import UserStatus

logger = logging.getLogger(__name__)


class AccessMiddleware(BaseMiddleware):
    """Silently drop ``blocked`` users; everything else passes through."""

    def __init__(self) -> None:
        self.update_types = ["message", "callback_query"]

    async def pre_process(self, update: Any, data: dict[str, Any]) -> Any:
        user = data.get("user")
        # ``data["user"]`` is ``None`` for a stranger (``users.touch`` never
        # inserts), so guard before dereferencing ``status``.
        if user is not None and getattr(user, "status", None) == UserStatus.BLOCKED:
            return CancelUpdate()
        return None

    async def post_process(
        self, update: Any, data: dict[str, Any], exception: Exception | None
    ) -> None:
        return None
