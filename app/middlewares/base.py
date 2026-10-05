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


def is_start_command(update: Any) -> bool:
    """Return ``True`` when the update is a ``/start`` message (§S3-1).

    The access gate lets *any* ``/start`` through — the command is also the
    redemption entry point for an ``inv_…`` deep-link payload (§S3-3), so a
    ``pending``/``rejected``/unknown user must reach ``start_command``. A
    ``/start@botname`` form and an attached payload are both accepted.
    """
    text = getattr(update, "text", None) or getattr(update, "caption", None)
    if not isinstance(text, str) or not text.strip():
        return False
    first = text.strip().split()[0]
    return first.split("@", 1)[0] == "/start"
