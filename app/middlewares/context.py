"""Context middleware: resolve user + role for every update (§M0-05.3).

Runs first (``Context → Maintenance → Access → Throttle``). It **only updates**
an existing ``users`` row (``username``/``first_name``/``last_seen_at``, reset
``bot_blocked``) and never inserts: a stranger produces no DB row but still gets
``data["role"] = None`` and ``data["user"] = None``. Row creation is the job of
``/start`` (M0-09.1).
"""

from __future__ import annotations

import logging
from typing import Any

from telebot.asyncio_handler_backends import BaseMiddleware

from app.container import Container
from app.db.repositories import users as users_repo
from app.middlewares.base import user_id
from app.permissions import Role

logger = logging.getLogger(__name__)


class ContextMiddleware(BaseMiddleware):
    """Attach ``data["user"]`` (may be ``None``) and ``data["role"]``."""

    def __init__(self, container: Container) -> None:
        self._container = container
        self.update_types = ["message", "callback_query"]

    async def pre_process(self, update: Any, data: dict[str, Any]) -> None:
        tg_id = user_id(update)
        if tg_id is None:
            data["user"] = None
            data["role"] = None
            return None

        from_user = getattr(update, "from_user", None)
        username = getattr(from_user, "username", None)
        first_name = getattr(from_user, "first_name", None)

        data["user"] = await self._refresh(int(tg_id), username, first_name)
        data["role"] = await self._role_for(int(tg_id))
        return None

    async def _refresh(
        self, tg_id: int, username: str | None, first_name: str | None
    ) -> Any:
        """Update the existing row (never insert) and return it (or ``None``)."""
        if self._container.sessionmaker is None:
            return None
        try:
            async with self._container.db() as session:
                return await users_repo.touch(
                    session, tg_id, username=username, first_name=first_name
                )
        except Exception:  # pragma: no cover - never block an update on this
            logger.exception("failed to refresh context for user %s", tg_id)
            return None

    async def _role_for(self, tg_id: int) -> Role | None:
        """Resolve the caller's staff role (``None`` for non-staff)."""
        admins = self._container.admins
        if admins is None:
            return None
        return await admins.get_role(tg_id)

    async def post_process(
        self, update: Any, data: dict[str, Any], exception: Exception | None
    ) -> None:
        return None
