"""Text helpers for messages rendered with ``parse_mode="HTML"``.

Telegram rejects any message whose text contains a stray ``<``, ``>`` or ``&``
under HTML parse mode (``TASK_PLAN.md`` defect B9). Every user-supplied string
that is embedded into an HTML message must go through :func:`esc` first.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.settings import Settings


def esc(text: object) -> str:
    """Escape ``text`` for safe embedding in an HTML-parsed message.

    ``&`` must be replaced first (``html.escape`` does this), otherwise the
    ``&`` introduced by escaping ``<``/``>`` would itself be double-escaped.
    """
    return html.escape(str(text), quote=False)


def sub_url(settings: Settings, sub_id: str) -> str:
    """Return the subscription URL for ``sub_id`` (``TASK_PLAN.md`` §S1-1.6).

    The **single** source of every link the bot shows (profile, QR,
    instructions, the link screen and regeneration), so the base URL lives in
    exactly one place: ``settings.sub_url_base``.
    """
    return f"{settings.sub_url_base}{sub_id}"
