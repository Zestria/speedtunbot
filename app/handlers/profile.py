"""``/profile`` — subscription dashboard (``TASK_PLAN.md`` §S1-1, fixes B4).

Reads the client through :class:`~app.services.panel.PanelGateway` (never through
``py3xui`` directly) and renders one dashboard card with the traffic bar,
expiry, activity and the subscription link. The read never raises: a failed
panel read returns a friendly message the caller owns, and a user without an
account is told to run ``/start``.

B4 was an ``UnboundLocalError``: the legacy handler caught the panel failure but
then used the never-assigned ``inbound`` variable. Here a failed read is turned
into ``(None, <friendly>)`` and the handler stops.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from telebot.util import quick_markup

from app import texts
from app.callbacks import ProfileNav, unpack
from app.container import Container
from app.errors import InvalidCallback, PanelError
from app.handlers.common import alert_staff, reply
from app.services.panel import ClientTraffic
from app.ui import bar, delete_if_photo, edit_or_send, fmt_bytes, fmt_left
from app.utils.text import esc, sub_url

logger = logging.getLogger(__name__)

#: Callback-data namespace owned by this handler.
NAMESPACE = f"{ProfileNav.ns}:"

#: Dashboard states (§S1-1.9).
STATE_ACTIVE = "active"
STATE_EXPIRING = "expiring"
STATE_EXPIRED = "expired"
STATE_UNLIMITED = "unlimited"
STATE_SUSPENDED = "suspended"
STATE_NOT_ACTIVATED = "not_activated"

#: An enabled client entering its last three days is "expiring" (🟡).
EXPIRING_WINDOW_MS = 3 * 24 * 60 * 60 * 1000

_STATE_LINES = {
    STATE_ACTIVE: texts.PROFILE_STATE_ACTIVE,
    STATE_EXPIRING: texts.PROFILE_STATE_EXPIRING,
    STATE_EXPIRED: texts.PROFILE_STATE_EXPIRED,
    STATE_SUSPENDED: texts.PROFILE_STATE_SUSPENDED,
    STATE_NOT_ACTIVATED: texts.PROFILE_STATE_NOT_ACTIVATED,
}
#: States that get the "pay a tariff" hint appended.
_DEAD_STATES = frozenset({STATE_EXPIRED, STATE_SUSPENDED, STATE_NOT_ACTIVATED})


def _now_ms() -> int:
    """Wall-clock epoch milliseconds."""
    return int(time.time() * 1000)


async def load_profile_info(
    container: Container, tg_id: int
) -> tuple[ClientTraffic | None, str | None]:
    """Fetch the traffic snapshot for ``tg_id`` (§S1-1.8).

    Returns ``(None, None)`` when there is no panel client (the caller tells the
    user to run ``/start``) and ``(None, <friendly>)`` on a panel error, so "no
    client" and "outage" stay distinguishable. Never raises and never replies —
    the caller owns the reply.
    """
    panel = container.panel
    if panel is None:
        return None, texts.ERROR_GENERIC
    try:
        info = await panel.get_traffic(tg_id)
    except PanelError as exc:
        logger.warning("panel failure in /profile for %s: %s", tg_id, exc)
        return None, texts.ERROR_PANEL
    return info, None


def profile_state(info: ClientTraffic, now_ms: int) -> str:
    """Classify a snapshot into one of the dashboard states (§S1-1.9).

    Expiry is evaluated **first**: the panel disables a client the moment its
    expiry passes, so a disabled client with a future expiry is a suspension
    (paid, then frozen), while a disabled ``0`` was never activated.
    """
    if not info.enable:
        if info.expiry_ms == 0:
            return STATE_NOT_ACTIVATED
        return STATE_SUSPENDED if info.expiry_ms > now_ms else STATE_EXPIRED
    if info.expiry_ms == 0:
        return STATE_UNLIMITED
    left = info.expiry_ms - now_ms
    if left <= 0:
        return STATE_EXPIRED
    if left <= EXPIRING_WINDOW_MS:
        return STATE_EXPIRING
    return STATE_ACTIVE


def render_profile(
    info: ClientTraffic, now_ms: int, *, link: str, timezone: str
) -> tuple[str, Any]:
    """Render the dashboard card + keyboard for a snapshot (§S1-1.9).

    ``now_ms`` is passed in (rather than read from the clock) so every state is
    testable and the expiry and activity lines share one notion of "now".
    """
    state = profile_state(info, now_ms)
    line = (
        texts.PROFILE_STATE_ACTIVE if state == STATE_UNLIMITED else _STATE_LINES[state]
    )

    lines = [texts.PROFILE_TITLE, "", line]
    if state in _DEAD_STATES:
        lines.append(texts.PROFILE_STATE_HINT)
    expires = _expiry_line(info, now_ms, timezone)
    if expires:
        lines.append(expires)
    lines.append(_traffic_line(info))
    activity = _activity_line(info, now_ms, timezone)
    if activity:
        lines.append(activity)
    lines += ["", f"{texts.PROFILE_LINK_LABEL} <code>{esc(link)}</code>"]
    return "\n".join(lines), profile_keyboard()


def profile_keyboard() -> Any:
    """Build the 3-row dashboard keyboard (§S1-1.10).

    ``quick_markup`` with ``row_width=2`` lays the five buttons out as
    ``[QR][Инструкция]`` / ``[Продлить][Новая ссылка]`` / ``[Поддержка]``.
    """
    return quick_markup(
        {
            texts.BUTTON_PROFILE_QR: {"callback_data": ProfileNav("qr").pack()},
            texts.BUTTON_PROFILE_INSTR: {"callback_data": ProfileNav("instr").pack()},
            texts.BUTTON_PROFILE_EXTEND: {"callback_data": ProfileNav("pay").pack()},
            texts.BUTTON_PROFILE_NEWLINK: {
                "callback_data": ProfileNav("newlink").pack()
            },
            texts.BUTTON_PROFILE_SUPPORT: {
                "callback_data": ProfileNav("support").pack()
            },
        },
        row_width=2,
    )


def _expiry_line(info: ClientTraffic, now_ms: int, timezone: str) -> str | None:
    """Return the ``📅 До:`` line, or ``None`` when there is no expiry at all."""
    if info.expiry_ms == 0:
        # Only an *enabled* ``0`` is ∞; a disabled one is reported by the state
        # line alone (S0-1.6).
        return (
            f"{texts.PROFILE_EXPIRES_LABEL} {texts.PROFILE_UNLIMITED}"
            if info.enable
            else None
        )
    tz = ZoneInfo(timezone)
    stamp = datetime.fromtimestamp(info.expiry_ms / 1000, tz=tz).strftime("%d.%m.%Y")
    left = fmt_left(info.expiry_ms, now_ms)
    return f"{texts.PROFILE_EXPIRES_LABEL} {stamp} · {left}"


def _traffic_line(info: ClientTraffic) -> str:
    """Return the ``📊`` line; the bar is omitted when there is no quota."""
    used = fmt_bytes(info.used)
    if info.total <= 0:
        return f"{texts.PROFILE_TRAFFIC_LABEL} {used} / {texts.PROFILE_UNLIMITED}"
    filled = bar(info.used / info.total)
    return f"{texts.PROFILE_TRAFFIC_LABEL} {filled} {used} / {fmt_bytes(info.total)}"


def _activity_line(info: ClientTraffic, now_ms: int, timezone: str) -> str | None:
    """Return the ``🕒`` line, or ``None`` when the panel reports no activity."""
    if not info.last_online_ms:
        return None
    tz = ZoneInfo(timezone)
    seen = datetime.fromtimestamp(info.last_online_ms / 1000, tz=tz)
    today = datetime.fromtimestamp(now_ms / 1000, tz=tz).date()
    clock = seen.strftime("%H:%M")
    if seen.date() == today:
        when = texts.PROFILE_ACTIVITY_TODAY.format(time=clock)
    elif (today - seen.date()).days == 1:
        when = texts.PROFILE_ACTIVITY_YESTERDAY.format(time=clock)
    else:
        when = texts.PROFILE_ACTIVITY_OTHER.format(
            date=seen.strftime("%d.%m"), time=clock
        )
    return f"{texts.PROFILE_ACTIVITY_LABEL} {when}"


async def profile_command(message: Any, bot: Any, container: Container) -> None:
    """Show the caller's dashboard; always reply, never raise on a panel error."""
    tg_id = int(message.from_user.id)
    chat_id = int(message.chat.id)

    info, error = await load_profile_info(container, tg_id)
    if info is None:
        await reply(bot, chat_id, error or texts.PROFILE_NO_ACCOUNT)
        if error is not None:
            await alert_staff(container, "profile", error)
        return

    settings = container.settings
    text, markup = render_profile(
        info,
        _now_ms(),
        link=sub_url(settings, info.sub_id),
        timezone=settings.timezone,
    )
    await reply(bot, chat_id, text, reply_markup=markup, parse_mode="HTML")


async def profile_callback(call: Any, bot: Any, container: Container) -> None:
    """Handle ``prf:`` callbacks — ``prf:profile`` only for now (§S1-1.12).

    Every path answers the callback query, so a crafted or stale payload never
    leaves the button spinner running and never raises into the poller (B5).
    """
    try:
        payload = unpack(getattr(call, "data", None))
    except InvalidCallback:
        await _answer(bot, call, texts.ERROR_STALE_BUTTON)
        return
    if not isinstance(payload, ProfileNav) or payload.section != "profile":
        await _answer(bot, call, texts.ERROR_STALE_BUTTON)
        return

    tg_id = int(call.from_user.id)
    chat_id = int(call.message.chat.id)
    message_id = int(call.message.message_id)

    info, error = await load_profile_info(container, tg_id)
    await delete_if_photo(bot, call)
    if info is None:
        await edit_or_send(bot, chat_id, message_id, error or texts.PROFILE_NO_ACCOUNT)
        await _answer(bot, call, None)
        if error is not None:
            await alert_staff(container, "profile", error)
        return

    settings = container.settings
    text, markup = render_profile(
        info,
        _now_ms(),
        link=sub_url(settings, info.sub_id),
        timezone=settings.timezone,
    )
    await edit_or_send(bot, chat_id, message_id, text, markup=markup)
    await _answer(bot, call, None)


async def _answer(bot: Any, call: Any, text: str | None) -> None:
    """Answer the callback query without ever raising."""
    try:
        await bot.answer_callback_query(getattr(call, "id", None), text)
    except Exception:  # pragma: no cover - cosmetic
        logger.debug("failed to answer callback", exc_info=True)


def register_profile_handler(bot: Any, container: Container) -> None:
    """Register ``/profile`` and the ``prf:`` callbacks on ``bot``."""

    @bot.message_handler(commands=["profile"])
    async def _profile(message: Any) -> None:  # pragma: no cover - thin adapter
        await profile_command(message, bot, container)

    @bot.callback_query_handler(
        func=lambda call: (getattr(call, "data", "") or "").startswith(NAMESPACE)
    )
    async def _callback(call: Any) -> None:  # pragma: no cover - thin adapter
        await profile_callback(call, bot, container)


__all__ = [
    "NAMESPACE",
    "load_profile_info",
    "profile_callback",
    "profile_command",
    "profile_keyboard",
    "profile_state",
    "register_profile_handler",
    "render_profile",
]
