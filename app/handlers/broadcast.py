"""Broadcast: ``📢 Рассылка`` screen plus the ``/broadcast`` shortcut (§S2-6).

The admin picks an audience (``all`` / ``active`` / ``soon``), sees a preview
with the recipient count, and confirms; the send loop then edits **one** live
progress message every :data:`PROGRESS_EVERY` sends and finishes with a single
summary (fixes B3 — the legacy handler printed the summary inside the loop).

Everything is in-process (no resumable job table, ``TASK_PLAN.md`` §S2-6): the
pending text/audience live in the telebot FSM data, and each send goes through
:meth:`Notifier.safe_send` with ``patient=True`` so a 429 is honoured fully.
The text is escaped (B9). ``/broadcast <text>`` jumps straight to the audience
step; the legacy behaviour of sending immediately is gone.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from telebot import types

from app import texts
from app.callbacks import AdminNav
from app.container import Container
from app.db.repositories import users as users_repo
from app.handlers.admin.nav import register_screen
from app.handlers.common import reply
from app.permissions import Permission, check_callback, require
from app.states import UserStates
from app.ui import bar, edit_or_send
from app.utils.text import esc

logger = logging.getLogger(__name__)

#: Delay between two sends, to stay well inside Telegram's rate limits (§M0-09.4).
PACING = 0.1
#: Edit the progress message after every this many sends (§S2-6.5).
PROGRESS_EVERY = 20
#: «Истекают ≤ 7 д» window (§S2-6.2).
SOON_WINDOW_MS = 7 * 24 * 60 * 60 * 1000

#: Audience keys offered by the audience row, in button order (§S2-6.4).
AUDIENCE_ALL = "all"
AUDIENCE_ACTIVE = "active"
AUDIENCE_SOON = "soon"
AUDIENCES: tuple[str, ...] = (AUDIENCE_ALL, AUDIENCE_ACTIVE, AUDIENCE_SOON)

#: Audience key → button label (screen copy, ``TASK_PLAN.md`` rule 9).
AUDIENCE_LABELS: dict[str, str] = {
    AUDIENCE_ALL: "Все",
    AUDIENCE_ACTIVE: "🟢 Активные",
    AUDIENCE_SOON: "🟡 Истекают ≤ 7 д",
}

#: ``adm:broadcast:`` actions.
OP_AUDIENCE = "a"
OP_SEND = "go"
OP_CANCEL = "x"

BUTTON_SEND = "✅ Отправить"
BUTTON_CANCEL = "✖ Отмена"

#: FSM-data keys holding the pending broadcast between callbacks (§S2-6.3).
TEXT_KEY = "admin_broadcast_text"
AUDIENCE_KEY = "admin_broadcast_audience"

#: Audit action recorded for a completed broadcast (§S2-6.5).
AUDIT_BROADCAST = "broadcast.send"


def _now_ms() -> int:
    """Return the current epoch time in milliseconds."""
    return int(time.time() * 1000)


# --- audience resolution (§S2-6.2) ------------------------------------------


async def _reachable_ids(container: Container) -> list[int]:
    """Return ``approved`` ids that do not block the bot (§S2-6.2).

    One short read-only session, closed before any send — a large broadcast must
    never hold a DB connection across the pacing sleeps (§M0-09.4).
    """
    if container.sessionmaker is None:
        return []
    try:
        async with container.sessionmaker() as session:
            return await users_repo.list_broadcast_ids(session)
    except Exception:
        logger.warning("broadcast recipient read failed", exc_info=True)
        return []


async def _client_state(container: Container) -> dict[int, tuple[bool, int]]:
    """Return ``{tg_id: (enable, expiry_ms)}`` from one panel read (§S2-6.2).

    ``{}`` on a panel outage, so the audience simply degrades to the reachable
    set instead of failing the whole flow.
    """
    panel = container.panel
    if panel is None:
        return {}
    try:
        clients = await panel.list_clients()
    except Exception:
        logger.info("broadcast panel map unavailable", exc_info=True)
        return {}
    state: dict[int, tuple[bool, int]] = {}
    for client in clients:
        email = str(getattr(client, "email", "") or "")
        if email.isdigit():
            state[int(email)] = (
                bool(getattr(client, "enable", False)),
                int(getattr(client, "expiry_time", 0) or 0),
            )
    return state


def _is_active(state: tuple[bool, int] | None) -> bool:
    """True when the client is enabled and not expired (``0`` = unlimited)."""
    if state is None:
        return False
    enable, expiry = state
    if not enable:
        return False
    return expiry == 0 or expiry > _now_ms()


def _expires_soon(state: tuple[bool, int] | None, now_ms: int) -> bool:
    """True when the client expires within :data:`SOON_WINDOW_MS` (§S2-6.2)."""
    if state is None:
        return False
    _, expiry = state
    return 0 < expiry - now_ms <= SOON_WINDOW_MS


async def audience_ids(container: Container, key: str) -> list[int]:
    """Resolve the recipient ids for an audience ``key`` (§S2-6.2).

    Every audience starts from the reachable set (``approved`` and not
    ``bot_blocked``) so a broadcast can never target someone who blocked the
    bot. ``all`` keeps it whole; ``active`` narrows to enabled, unexpired panel
    clients (the 🟢 icon) and ``soon`` to clients expiring within 7 days (🟡).
    An unknown key falls back to ``all``.
    """
    reachable = await _reachable_ids(container)
    if key not in (AUDIENCE_ACTIVE, AUDIENCE_SOON):
        return reachable
    state = await _client_state(container)
    now_ms = _now_ms()
    if key == AUDIENCE_ACTIVE:
        return [tg for tg in reachable if _is_active(state.get(tg))]
    return [tg for tg in reachable if _expires_soon(state.get(tg), now_ms)]


# --- cards / keyboards (§S2-6.3/.4) -----------------------------------------


def audience_card(text: str) -> tuple[str, Any]:
    """Render the audience step: announcement + ``[Все][🟢][🟡]`` (§S2-6.4)."""
    card = texts.BROADCAST_AUDIENCE.format(body=esc(text))
    rows = [
        [
            types.InlineKeyboardButton(
                AUDIENCE_LABELS[key],
                callback_data=AdminNav("broadcast", OP_AUDIENCE, key).pack(),
            )
            for key in AUDIENCES
        ],
        [
            types.InlineKeyboardButton(
                BUTTON_CANCEL, callback_data=AdminNav("broadcast", OP_CANCEL).pack()
            )
        ],
    ]
    return card, types.InlineKeyboardMarkup(rows)


def preview_card(text: str, count: int) -> tuple[str, Any]:
    """Render the preview: announcement + count + confirm (§S2-6.4)."""
    card = texts.BROADCAST_PREVIEW.format(body=esc(text), count=count)
    rows = [
        [
            types.InlineKeyboardButton(
                BUTTON_SEND, callback_data=AdminNav("broadcast", OP_SEND).pack()
            ),
            types.InlineKeyboardButton(
                BUTTON_CANCEL, callback_data=AdminNav("broadcast", OP_CANCEL).pack()
            ),
        ]
    ]
    return card, types.InlineKeyboardMarkup(rows)


def progress_text(done: int, sent: int, failed: int, total: int) -> str:
    """Return the live progress line for ``done``/``total`` sends (§S2-6.5)."""
    fraction = (done / total) if total else 1.0
    return texts.BROADCAST_PROGRESS.format(
        bar=bar(fraction), pct=round(fraction * 100), sent=sent, failed=failed
    )


# --- FSM data helpers (§S2-6.3) ---------------------------------------------


async def _store(
    bot: Any,
    actor: int,
    chat_id: int,
    *,
    text: str | None = None,
    audience: str | None = None,
) -> None:
    """Persist the pending text/audience in the FSM data (best effort)."""
    try:
        async with bot.retrieve_data(actor, chat_id) as data:
            if text is not None:
                data[TEXT_KEY] = text
            if audience is not None:
                data[AUDIENCE_KEY] = audience
    except Exception:  # pragma: no cover - FSM backend unavailable
        logger.debug("failed to store broadcast data", exc_info=True)


async def _load(bot: Any, actor: int, chat_id: int) -> tuple[str, str]:
    """Return ``(text, audience)`` from the FSM data (empty text when absent)."""
    try:
        async with bot.retrieve_data(actor, chat_id) as data:
            text = str(data.get(TEXT_KEY) or "")
            audience = str(data.get(AUDIENCE_KEY) or AUDIENCE_ALL)
    except Exception:  # pragma: no cover - FSM backend unavailable
        logger.debug("failed to load broadcast data", exc_info=True)
        return "", AUDIENCE_ALL
    return text, audience


async def _set_state(bot: Any, actor: int, chat_id: int) -> None:
    """Enter the ``admin_broadcast`` FSM state (best effort)."""
    try:
        await bot.set_state(actor, UserStates.admin_broadcast, chat_id)
    except Exception:  # pragma: no cover - FSM backend unavailable
        logger.debug("failed to set broadcast state", exc_info=True)


async def _clear_state(bot: Any, actor: int, chat_id: int) -> None:
    """Leave the ``admin_broadcast`` FSM state (best effort)."""
    try:
        await bot.delete_state(actor, chat_id)
    except Exception:  # pragma: no cover - FSM backend unavailable
        logger.debug("failed to clear broadcast state", exc_info=True)


def _target(call: Any) -> tuple[int, int]:
    """Return ``(chat_id, message_id)`` of the callback's message."""
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


async def _show_menu(call: Any, bot: Any, container: Container) -> None:
    """Re-render the dashboard in place (lazy import avoids a nav cycle)."""
    from app.handlers.admin.home import show_dashboard
    from app.permissions import get_role

    actor = int(call.from_user.id)
    chat_id, message_id = _target(call)
    role = await get_role(actor)
    await show_dashboard(
        bot, chat_id, container, actor, role=role, message_id=message_id
    )


@require(Permission.BROADCAST_SEND)
async def broadcast_command(message: Any, bot: Any, container: Container) -> None:
    """``/broadcast <text>`` — shortcut into the audience step (§S2-6.6)."""
    actor = int(message.from_user.id)
    chat_id = int(message.chat.id)
    text = (getattr(message, "text", None) or "").partition(" ")[2].strip()
    if not text:
        await reply(bot, chat_id, texts.BROADCAST_USAGE, parse_mode="HTML")
        return
    await _set_state(bot, actor, chat_id)
    await _store(bot, actor, chat_id, text=text)
    card, markup = audience_card(text)
    await reply(bot, chat_id, card, reply_markup=markup, parse_mode="HTML")


@require(Permission.BROADCAST_SEND)
async def admin_broadcast_message(message: Any, bot: Any, container: Container) -> None:
    """Handle the text typed into the ``admin_broadcast`` prompt (§S2-6.3)."""
    actor = int(message.from_user.id)
    chat_id = int(message.chat.id)
    text = (getattr(message, "text", None) or "").strip()
    if not text:
        await _clear_state(bot, actor, chat_id)
        await reply(bot, chat_id, texts.BROADCAST_EMPTY)
        return
    await _store(bot, actor, chat_id, text=text)
    card, markup = audience_card(text)
    await reply(bot, chat_id, card, reply_markup=markup, parse_mode="HTML")


async def _prompt(call: Any, bot: Any, container: Container) -> None:
    """``adm:broadcast`` — ask for the text and enter the FSM (§S2-6.3)."""
    actor = int(call.from_user.id)
    chat_id, message_id = _target(call)
    await _set_state(bot, actor, chat_id)
    markup = types.InlineKeyboardMarkup(
        [
            [
                types.InlineKeyboardButton(
                    BUTTON_CANCEL,
                    callback_data=AdminNav("broadcast", OP_CANCEL).pack(),
                )
            ]
        ]
    )
    await edit_or_send(bot, chat_id, message_id, texts.BROADCAST_PROMPT, markup=markup)


async def _preview(call: Any, bot: Any, container: Container, audience: str) -> None:
    """``adm:broadcast:a:<key>`` — show the preview with the count (§S2-6.4)."""
    actor = int(call.from_user.id)
    chat_id, message_id = _target(call)
    text, _ = await _load(bot, actor, chat_id)
    if not text:
        await _answer(bot, call, texts.ERROR_STALE_BUTTON)
        return
    await _store(bot, actor, chat_id, audience=audience)
    targets = await audience_ids(container, audience)
    if not targets:
        await edit_or_send(bot, chat_id, message_id, texts.BROADCAST_NO_RECIPIENTS)
        return
    card, markup = preview_card(text, len(targets))
    await edit_or_send(bot, chat_id, message_id, card, markup=markup)


async def _cancel(call: Any, bot: Any, container: Container) -> None:
    """``adm:broadcast:x`` — drop the pending text and return to the menu."""
    actor = int(call.from_user.id)
    chat_id, _ = _target(call)
    await _clear_state(bot, actor, chat_id)
    await _show_menu(call, bot, container)


async def _run_send(
    bot: Any,
    chat_id: int,
    message_id: int,
    notifier: Any,
    targets: list[int],
    body: str,
) -> tuple[int, int]:
    """Send ``body`` to every target, editing one progress line (§S2-6.5).

    Returns ``(sent, failed)``. Sends are paced by :data:`PACING` and each one
    goes through ``safe_send(patient=True)`` so a 429 is retried after the full
    ``retry_after``; a failing send is counted, never raised (fixes B3).
    """
    total = len(targets)
    sent = failed = 0
    for index, target in enumerate(targets, start=1):
        if await notifier.safe_send(target, body, patient=True, parse_mode="HTML"):
            sent += 1
        else:
            failed += 1
        if index % PROGRESS_EVERY == 0:
            await edit_or_send(
                bot,
                chat_id,
                message_id,
                progress_text(index, sent, failed, total),
            )
        await asyncio.sleep(PACING)
    return sent, failed


async def _send(call: Any, bot: Any, container: Container) -> None:
    """``adm:broadcast:go`` — run the confirmed broadcast (§S2-6.5)."""
    actor = int(call.from_user.id)
    chat_id, message_id = _target(call)
    text, audience = await _load(bot, actor, chat_id)
    notifier = container.notifier
    if not text or notifier is None:
        await _answer(bot, call, texts.ERROR_GENERIC, alert=True)
        return
    targets = await audience_ids(container, audience)
    if not targets:
        await edit_or_send(bot, chat_id, message_id, texts.BROADCAST_NO_RECIPIENTS)
        await _clear_state(bot, actor, chat_id)
        return
    body = f"{texts.BROADCAST_HEADER}\n\n{esc(text)}"
    await _answer(bot, call, None)
    sent, failed = await _run_send(bot, chat_id, message_id, notifier, targets, body)
    await _audit(container, actor, audience, sent, failed, len(targets))
    # B3: the summary is sent exactly once, after the loop.
    await edit_or_send(
        bot,
        chat_id,
        message_id,
        texts.BROADCAST_SUMMARY.format(sent=sent, failed=failed),
    )
    await _clear_state(bot, actor, chat_id)


async def _audit(
    container: Container,
    actor: int,
    audience: str,
    sent: int,
    failed: int,
    total: int,
) -> None:
    """Record a completed broadcast (best effort, never raises, §S2-6.5)."""
    audit = container.audit
    if audit is None:  # pragma: no cover - container is wired at startup
        return
    await audit.log(
        actor,
        AUDIT_BROADCAST,
        "broadcast",
        audience,
        sent=sent,
        failed=failed,
        total=total,
    )


@register_screen("broadcast")
async def broadcast_screen(
    call: Any, bot: Any, container: Container, payload: Any = None
) -> None:
    """``adm:broadcast`` — prompt, preview, send or cancel (§S2-6).

    The ``adm:`` dispatcher already gates the section on ``BROADCAST_SEND``, but
    every mutation re-checks it here (defence in depth, as in every §S2 screen).
    """
    if not await check_callback(call, Permission.BROADCAST_SEND):
        return
    action = payload.action if isinstance(payload, AdminNav) else None
    arg = payload.arg if isinstance(payload, AdminNav) else None
    if action == OP_CANCEL:
        await _cancel(call, bot, container)
    elif action == OP_SEND:
        await _send(call, bot, container)
    elif action == OP_AUDIENCE and arg in AUDIENCES:
        await _preview(call, bot, container, str(arg))
    else:
        await _prompt(call, bot, container)


def register_broadcast_handler(bot: Any, container: Container) -> None:
    """Register ``/broadcast`` and the ``admin_broadcast`` text prompt.

    The ``adm:`` buttons themselves are served by the shared ``adm:``
    dispatcher; this only owns the two message entry points.
    """

    @bot.message_handler(commands=["broadcast"])
    async def _broadcast(message: Any) -> None:  # pragma: no cover - thin adapter
        await broadcast_command(message, bot, container)

    @bot.message_handler(state=UserStates.admin_broadcast, content_types=["text"])
    async def _text(message: Any) -> None:  # pragma: no cover - thin adapter
        await admin_broadcast_message(message, bot, container)


__all__ = [
    "AUDIENCE_ACTIVE",
    "AUDIENCE_ALL",
    "AUDIENCE_SOON",
    "AUDIT_BROADCAST",
    "PACING",
    "PROGRESS_EVERY",
    "SOON_WINDOW_MS",
    "admin_broadcast_message",
    "audience_card",
    "audience_ids",
    "broadcast_command",
    "broadcast_screen",
    "preview_card",
    "progress_text",
    "register_broadcast_handler",
]
