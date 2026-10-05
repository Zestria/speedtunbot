"""Text helpers for messages rendered with ``parse_mode="HTML"``.

Telegram rejects any message whose text contains a stray ``<``, ``>`` or ``&``
under HTML parse mode (``TASK_PLAN.md`` defect B9). Every user-supplied string
that is embedded into an HTML message must go through :func:`esc` first.
"""

from __future__ import annotations

import html


def esc(text: object) -> str:
    """Escape ``text`` for safe embedding in an HTML-parsed message.

    ``&`` must be replaced first (``html.escape`` does this), otherwise the
    ``&`` introduced by escaping ``<``/``>`` would itself be double-escaped.
    """
    return html.escape(str(text), quote=False)
