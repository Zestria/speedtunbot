"""Presentation helpers for the Stage-1 user screens (``TASK_PLAN.md`` §S1-1).

Pure formatters (:func:`bar`, :func:`fmt_bytes`, :func:`fmt_left`) plus two
best-effort Telegram helpers (:func:`edit_or_send`, :func:`delete_if_photo`).
Keeping them out of the handlers means every renderer is unit-testable without
a bot, and the "never raise into a handler" rule (§0.1) is enforced in one spot.
"""

from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger(__name__)

#: Filled / empty cells of the traffic bar.
BAR_FILLED = "▰"
BAR_EMPTY = "▱"
DEFAULT_BAR_WIDTH = 10

_BINARY_STEP = 1024
#: Binary units, smallest first (``TASK_PLAN.md`` §S1-1.2).
_BINARY_UNITS = ("Б", "КБ", "МБ", "ГБ", "ТБ")

_HOUR_MS = 3_600_000
_DAY_MS = 24 * _HOUR_MS


def bar(fraction: float, width: int = DEFAULT_BAR_WIDTH) -> str:
    """Return a ``width``-cell progress bar for ``fraction`` ∈ ``[0, 1]``.

    ``fraction`` is clamped, so a used/quota ratio above ``1`` still renders a
    full bar; a non-finite or non-numeric value collapses to ``0`` (§S1-1.1).
    """
    if width <= 0:
        return ""
    try:
        value = float(fraction)
    except (TypeError, ValueError):
        value = 0.0
    if not math.isfinite(value):
        value = 0.0
    value = min(1.0, max(0.0, value))
    filled = round(value * width)
    return BAR_FILLED * filled + BAR_EMPTY * (width - filled)


def fmt_bytes(n: int | float | None) -> str:
    """Format a byte count with binary units (§S1-1.2).

    Bytes are shown without a decimal (``1023 Б``); larger units keep one
    (``1.0 КБ``). ``None``, a negative or a non-finite value yields ``"0 Б"``.
    """
    if n is None:
        return f"0 {_BINARY_UNITS[0]}"
    try:
        value = float(n)
    except (TypeError, ValueError):
        return f"0 {_BINARY_UNITS[0]}"
    if not math.isfinite(value) or value < 0:
        return f"0 {_BINARY_UNITS[0]}"
    index = 0
    while value >= _BINARY_STEP and index < len(_BINARY_UNITS) - 1:
        value /= _BINARY_STEP
        index += 1
    if index == 0:
        return f"{int(value)} {_BINARY_UNITS[0]}"
    return f"{value:.1f} {_BINARY_UNITS[index]}"


def fmt_left(expiry_ms: int | None, now_ms: int) -> str:
    """Human "time left" label for an expiry timestamp (§S1-1.3).

    ``0`` means "no expiry at all", so it yields an empty string — the caller
    picks ∞ vs «не активирован» from ``enable`` (S0-1.6). An already-passed
    timestamp yields «истёк»; under 24 h the unit switches to hours.
    """
    if not expiry_ms:
        return ""
    left = int(expiry_ms) - int(now_ms)
    if left <= 0:
        return "истёк"
    if left < _DAY_MS:
        return f"ещё {max(1, left // _HOUR_MS)} ч."
    return f"ещё {left // _DAY_MS} дн."


async def edit_or_send(
    bot: Any,
    chat_id: int,
    message_id: int | None,
    text: str,
    *,
    markup: Any | None = None,
    parse_mode: str = "HTML",
) -> bool:
    """Edit ``message_id`` to ``text``, falling back to a fresh send (§S1-1.4).

    Telegram answers ``400`` both when the edit target is gone and when the text
    is simply unchanged; the latter is treated as success so a re-render never
    produces a duplicate message. Any other failure falls back to
    ``send_message``. Returns ``True`` when the user ends up with the text, and
    never raises.
    """
    if message_id:
        try:
            await bot.edit_message_text(
                text,
                chat_id,
                message_id,
                reply_markup=markup,
                parse_mode=parse_mode,
            )
            return True
        except Exception as exc:
            if _is_not_modified(exc):
                return True
            logger.debug("edit failed for %s/%s: %s", chat_id, message_id, exc)
    try:
        await bot.send_message(
            chat_id, text, reply_markup=markup, parse_mode=parse_mode
        )
    except Exception:  # user unreachable — never break the handler
        logger.warning("failed to send to %s", chat_id, exc_info=True)
        return False
    return True


async def delete_if_photo(bot: Any, call: Any) -> None:
    """Delete the callback's message when it carries a photo (§S1-1.5).

    A QR screen is a photo, so editing it as text fails; removing it first lets
    the caller send a clean text card instead — an edit-based reply would
    otherwise leave the stale image behind. Errors are ignored.
    """
    message = getattr(call, "message", None)
    if getattr(message, "photo", None) is None:
        return
    chat_id = int(getattr(getattr(message, "chat", None), "id", 0))
    message_id = int(getattr(message, "message_id", 0))
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:  # cosmetic — the reply must still go out
        logger.debug("failed to delete photo message", exc_info=True)


def _is_not_modified(exc: BaseException) -> bool:
    """True when Telegram rejected the edit only because nothing changed."""
    return "not modified" in str(exc).lower()
