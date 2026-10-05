"""``/profile`` — subscription dashboard and its screens (``TASK_PLAN.md`` §S1-1…§S1-3).

Reads the client through :class:`~app.services.panel.PanelGateway` (never through
``py3xui`` directly) and renders one dashboard card with the traffic bar,
expiry, activity and the subscription link. The read never raises: a failed
panel read returns a friendly message the caller owns, and a user without an
account is told to run ``/start``.

B4 was an ``UnboundLocalError``: the legacy handler caught the panel failure but
then used the never-assigned ``inbound`` variable. Here a failed read is turned
into ``(None, <friendly>)`` and the handler stops.

Every screen below the dashboard (QR, instructions, link) is a ``prf:`` section
served by :func:`profile_callback`, and every one of them shares
:func:`_resolve_target` — one panel read, one failure branch, one callback
answer — so no navigation path can leave the spinner running (B5).

The subscription link is always ``sub_url(settings, snapshot.sub_id)`` (§S1-1.6),
i.e. the **caller's own** URL: nothing about a particular panel is hard-coded.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from telebot.util import quick_markup

from app import texts
from app.callbacks import Confirm, ProfileNav, unpack
from app.container import Container
from app.errors import InvalidCallback, PanelError
from app.handlers.common import alert_staff, reply
from app.handlers.confirm import register_action
from app.handlers.payment import show_tariffs
from app.handlers.support import enter_support
from app.services.panel import ClientTraffic
from app.ui import bar, delete_if_photo, edit_or_send, fmt_bytes, fmt_left
from app.utils.qr import make_qr_png
from app.utils.text import esc, sub_url

logger = logging.getLogger(__name__)

#: Callback-data namespace owned by this handler.
NAMESPACE = f"{ProfileNav.ns}:"

#: Section prefix of a per-platform instruction screen (§S1-3.3).
INSTR_PREFIX = "instr_"

#: Dashboard states (§S1-1.9).
STATE_ACTIVE = "active"
STATE_EXPIRING = "expiring"
STATE_EXPIRED = "expired"
STATE_UNLIMITED = "unlimited"
STATE_SUSPENDED = "suspended"
STATE_NOT_ACTIVATED = "not_activated"

#: An enabled client entering its last three days is "expiring" (🟡).
EXPIRING_WINDOW_MS = 3 * 24 * 60 * 60 * 1000

#: ``cf:`` action name and cooldown for «🔄 Новая ссылка» (§S1-5.2–.4).
ACTION_NEWLINK = "newlink"
REGEN_COOLDOWN = 600.0

#: ``tg_id`` → monotonic time of the last **successful** regeneration. A failed
#: attempt never stamps it, so the user is not locked out by a panel hiccup.
_last_regen: dict[int, float] = {}


def _regen_allowed(tg_id: int, now: float) -> bool:
    """Return ``True`` when ``tg_id`` may regenerate a link right now (§S1-5.4).

    ``now`` is a monotonic timestamp so the check is independent of the wall
    clock; the cooldown is only started **after** a regeneration succeeds.
    """
    last = _last_regen.get(int(tg_id))
    return last is None or (now - last) >= REGEN_COOLDOWN


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


def qr_keyboard() -> Any:
    """Keyboard under the QR photo (§S1-2.2): one step further or back."""
    return quick_markup(
        {
            texts.BUTTON_PROFILE_INSTR: {"callback_data": ProfileNav("instr").pack()},
            texts.BUTTON_BACK: {"callback_data": ProfileNav("profile").pack()},
        },
        row_width=2,
    )


def instr_picker_keyboard() -> Any:
    """Platform picker (§S1-3.2): one button per platform, then ``⬅️ Назад``."""
    buttons = {
        texts.INSTR_PLATFORM_LABELS[platform]: {
            "callback_data": ProfileNav(f"{INSTR_PREFIX}{platform}").pack()
        }
        for platform in texts.INSTRUCTION_PLATFORMS
    }
    buttons[texts.BUTTON_BACK] = {"callback_data": ProfileNav("profile").pack()}
    return quick_markup(buttons, row_width=2)


def instructions_keyboard() -> Any:
    """Keyboard under one platform's steps (§S1-3.3).

    ``⬅️ Назад`` returns to the **picker** (``prf:instr``), not the dashboard:
    the picker is the parent screen of every platform.
    """
    return quick_markup(
        {
            texts.BUTTON_PROFILE_QR: {"callback_data": ProfileNav("qr").pack()},
            texts.BUTTON_PROFILE_LINK: {"callback_data": ProfileNav("link").pack()},
            texts.BUTTON_BACK: {"callback_data": ProfileNav("instr").pack()},
        },
        row_width=2,
    )


def link_keyboard() -> Any:
    """Keyboard under the link screen (§S1-3.4): just ``⬅️ Назад``."""
    return quick_markup(
        {texts.BUTTON_BACK: {"callback_data": ProfileNav("profile").pack()}},
        row_width=1,
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
    """Dispatch a ``prf:`` callback to its screen (§S1-1.12 … §S1-3.4).

    Every path answers the callback query, so a crafted or stale payload never
    leaves the button spinner running and never raises into the poller (B5).
    """
    try:
        payload = unpack(getattr(call, "data", None))
    except InvalidCallback:
        await _answer(bot, call, texts.ERROR_STALE_BUTTON)
        return
    if not isinstance(payload, ProfileNav):
        await _answer(bot, call, texts.ERROR_STALE_BUTTON)
        return

    section = payload.section
    if section == "profile":
        await _show_profile(call, bot, container)
    elif section == "qr":
        await _show_qr(call, bot, container)
    elif section == "instr":
        await _show_instr_picker(call, bot, container)
    elif section == "link":
        await _show_link(call, bot, container)
    elif section == "newlink":
        await _show_newlink_request(call, bot, container)
    elif section == "pay":
        await _show_pay(call, bot, container)
    elif section == "support":
        await _show_support(call, bot, container)
    elif section.startswith(INSTR_PREFIX):
        await _show_instructions(
            call, bot, container, platform=section[len(INSTR_PREFIX) :]
        )
    else:
        await _answer(bot, call, texts.ERROR_STALE_BUTTON)


async def _show_pay(call: Any, bot: Any, container: Container) -> None:
    """Open the tariff list from the dashboard button (§S1-4.3).

    Delegates to the exact function ``/pay`` uses, with the card's
    ``message_id``, so the list replaces the dashboard and both entry points
    keep the same approval and panel-client guards.
    """
    chat_id, message_id = _target(call)
    await show_tariffs(
        bot, chat_id, container, int(call.from_user.id), message_id=message_id
    )
    await _answer(bot, call, None)


async def _show_support(call: Any, bot: Any, container: Container) -> None:
    """Enter the support flow from the dashboard button (§S1-4.3).

    The same :func:`enter_support` ``/support`` calls — including the FSM state,
    so a user who pressed the button can write to staff exactly as if they had
    typed the command.
    """
    chat_id, message_id = _target(call)
    await enter_support(
        bot, chat_id, container, int(call.from_user.id), message_id=message_id
    )
    await _answer(bot, call, None)


async def _show_profile(call: Any, bot: Any, container: Container) -> None:
    """Re-render the dashboard card in place (§S1-1.12)."""
    settings = container.settings

    def build(snapshot: ClientTraffic, link: str) -> tuple[str, Any]:
        return render_profile(
            snapshot, _now_ms(), link=link, timezone=settings.timezone
        )

    await _reply_screen(call, bot, container, build)


async def _show_qr(call: Any, bot: Any, container: Container) -> None:
    """Send the subscription QR as an in-memory photo (§S1-2.2).

    A photo cannot be edited into the card, so this screen is the one place that
    sends a new message; ``delete_if_photo`` (inside :func:`_resolve_target`)
    removes an earlier QR first, so re-pressing the button never stacks photos.
    """
    resolved = await _resolve_target(call, bot, container)
    if resolved is None:
        return
    _, link = resolved
    chat_id, _ = _target(call)
    await bot.send_photo(
        chat_id,
        make_qr_png(link),
        caption=texts.QR_CAPTION.replace("{link}", esc(link)),
        reply_markup=qr_keyboard(),
        parse_mode="HTML",
    )
    await _answer(bot, call, None)


async def _show_instr_picker(call: Any, bot: Any, container: Container) -> None:
    """Show the platform picker (§S1-3.2).

    Static copy — no panel read, no link — so it works even while the panel is
    down, and it is edited in place like every other text screen.
    """
    chat_id, message_id = _target(call)
    await delete_if_photo(bot, call)
    await edit_or_send(
        bot, chat_id, message_id, texts.INSTR_PICKER, markup=instr_picker_keyboard()
    )
    await _answer(bot, call, None)


async def _show_instructions(
    call: Any, bot: Any, container: Container, *, platform: str
) -> None:
    """Show one platform's steps, filled with the caller's own link (§S1-3.3)."""
    template = texts.INSTRUCTIONS.get(platform)
    if template is None:
        await _answer(bot, call, texts.ERROR_STALE_BUTTON)
        return

    def build(_snapshot: ClientTraffic, link: str) -> tuple[str, Any]:
        return template.replace("{link}", esc(link)), instructions_keyboard()

    await _reply_screen(call, bot, container, build)


async def _show_link(call: Any, bot: Any, container: Container) -> None:
    """Show the subscription URL on its own screen (§S1-3.4)."""

    def build(_snapshot: ClientTraffic, link: str) -> tuple[str, Any]:
        return texts.PROFILE_LINK_SCREEN.replace("{link}", esc(link)), link_keyboard()

    await _reply_screen(call, bot, container, build)


async def _show_newlink_request(call: Any, bot: Any, container: Container) -> None:
    """«🔄 Новая ссылка» — mint a confirmation token and show its card (§S1-5.2).

    The cooldown is checked here **and** again at confirmation, so a user cannot
    queue a second regeneration by pressing the button while the first card is
    still on screen. Nothing is written to the panel yet — the card is only a
    question, and the old link keeps working until «✅ Подтвердить».
    """
    tg_id = int(call.from_user.id)
    if not _regen_allowed(tg_id, time.monotonic()):
        await _answer(bot, call, texts.NEWLINK_RATE_LIMITED, alert=True)
        return
    store = container.confirmations
    if store is None:  # pragma: no cover - container is wired at startup
        await _answer(bot, call, texts.ERROR_GENERIC)
        return

    token = store.create(tg_id, ACTION_NEWLINK)
    markup = quick_markup(
        {
            texts.BUTTON_CONFIRM: {"callback_data": Confirm(token=token).pack()},
            texts.BUTTON_CANCEL: {
                "callback_data": Confirm(token=token, cancel=True).pack()
            },
        },
        row_width=2,
    )
    chat_id, message_id = _target(call)
    await delete_if_photo(bot, call)
    await edit_or_send(bot, chat_id, message_id, texts.NEWLINK_CONFIRM, markup=markup)
    await _answer(bot, call, None)


@register_action(ACTION_NEWLINK)
async def _confirm_newlink(call: Any, bot: Any, container: Container) -> None:
    """«✅ Подтвердить» — assign a fresh ``sub_id`` and re-render the card (§S1-5.3).

    Runs through the central ``cf:`` dispatcher (§S1-5.1). The cooldown is
    re-checked because the confirmation can be pressed minutes after the card
    was minted. A panel failure leaves the old ``sub_id`` in place: nothing was
    written, so the user is told the old link still works instead of being left
    with a dead one. Only a **successful** regeneration stamps the cooldown.
    """
    tg_id = int(call.from_user.id)
    now = time.monotonic()
    if not _regen_allowed(tg_id, now):
        await _answer(bot, call, texts.NEWLINK_RATE_LIMITED, alert=True)
        return
    panel = container.panel
    if panel is None:  # pragma: no cover - container is wired at startup
        await _answer(bot, call, texts.ERROR_GENERIC)
        return

    try:
        sub_id = await panel.regenerate_sub_id(tg_id)
    except PanelError as exc:
        logger.warning("panel failure regenerating link for %s: %s", tg_id, exc)
        chat_id, message_id = _target(call)
        await delete_if_photo(bot, call)
        await edit_or_send(bot, chat_id, message_id, texts.NEWLINK_FAILED)
        await _answer(bot, call, texts.NEWLINK_FAILED, alert=True)
        await alert_staff(container, "newlink", exc)
        return

    _last_regen[tg_id] = now
    await _audit_regen(container, tg_id, sub_id)
    # Re-reads the panel (cache was invalidated by the write), so the card shows
    # the new link and the QR button; it also answers the callback.
    await _show_profile(call, bot, container)


async def _audit_regen(container: Container, tg_id: int, sub_id: str) -> None:
    """Record ``user.sub_regenerate`` (best effort, never raises, §S1-5.3)."""
    audit = container.audit
    if audit is None:
        return
    await audit.log(tg_id, "user.sub_regenerate", "user", tg_id, sub_id=sub_id)


async def _resolve_target(
    call: Any, bot: Any, container: Container
) -> tuple[ClientTraffic, str] | None:
    """Read ``(snapshot, link)`` for the caller, handling every failure here.

    Returns ``None`` after replying in place — the ``/start`` hint for a user
    with no client, or the outage text plus a staff alert — so screens only ever
    deal with the success path. The stale QR photo (if any) is deleted before
    that reply, because editing a photo message as text would fail.
    """
    chat_id, message_id = _target(call)
    snapshot, error = await load_profile_info(container, int(call.from_user.id))
    await delete_if_photo(bot, call)
    if snapshot is not None:
        return snapshot, sub_url(container.settings, snapshot.sub_id)
    await edit_or_send(bot, chat_id, message_id, error or texts.PROFILE_NO_ACCOUNT)
    await _answer(bot, call, None)
    if error is not None:
        await alert_staff(container, "profile", error)
    return None


async def _reply_screen(
    call: Any,
    bot: Any,
    container: Container,
    build: Callable[[ClientTraffic, str], tuple[str, Any]],
) -> None:
    """Render a text screen in place, with ``build(snapshot, link)`` (§S1-3.2–.4).

    The shared plumbing — panel read, failure branch, stale-photo cleanup,
    ``edit_or_send`` and the callback answer — lives here so the screens cannot
    drift apart: they only supply their copy and keyboard.
    """
    resolved = await _resolve_target(call, bot, container)
    if resolved is None:
        return
    snapshot, link = resolved
    chat_id, message_id = _target(call)
    text, markup = build(snapshot, link)
    await edit_or_send(bot, chat_id, message_id, text, markup=markup)
    await _answer(bot, call, None)


def _target(call: Any) -> tuple[int, int]:
    """Return ``(chat_id, message_id)`` of the message the callback came from."""
    message = call.message
    return int(message.chat.id), int(message.message_id)


async def _answer(
    bot: Any, call: Any, text: str | None, *, alert: bool = False
) -> None:
    """Answer the callback query without ever raising."""
    try:
        await bot.answer_callback_query(getattr(call, "id", None), text, alert)
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
    "ACTION_NEWLINK",
    "INSTR_PREFIX",
    "NAMESPACE",
    "REGEN_COOLDOWN",
    "_last_regen",
    "_regen_allowed",
    "instr_picker_keyboard",
    "instructions_keyboard",
    "link_keyboard",
    "load_profile_info",
    "profile_callback",
    "profile_command",
    "profile_keyboard",
    "profile_state",
    "qr_keyboard",
    "register_profile_handler",
    "render_profile",
]
