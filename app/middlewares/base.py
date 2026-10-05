"""Small shared helpers for the middleware layer (``TASK_PLAN.md`` §M0-05).

Kept separate so the individual middleware modules never import each other (and
so ``app/middlewares/__init__.py`` can import all of them without a cycle).
"""

from __future__ import annotations

from typing import Any


def user_id(update: Any) -> int | None:
    """Return the acting Telegram ID for a message/callback update."""
    return getattr(getattr(update, "from_user", None), "id", None)


def chat_id(update: Any) -> int | None:
    """Return the chat ID of a message, or of a callback's message."""
    chat = getattr(update, "chat", None)
    if chat is None:
        message = getattr(update, "message", None)
        chat = getattr(message, "chat", None)
    cid = getattr(chat, "id", None)
    return None if cid is None else int(cid)


def is_callback(update: Any) -> bool:
    """Return ``True`` for a ``CallbackQuery`` (it carries ``data`` + ``id``)."""
    return getattr(update, "data", None) is not None and (
        getattr(update, "id", None) is not None
    )
