"""Admin «Пользователи» screen: list, filters and search (§S2-2).

The screen joins the ``users`` DB rows with **one** cached ``list_clients()``
read, so a page render costs a single panel call regardless of page size. Every
panel failure is swallowed: a clientless row simply has ``expiry_ms``/``enable``
of ``None`` and renders as 🔴, which keeps the list usable during an outage.

Layout (templates, filter labels and button text) lives inline here rather than
in ``app.texts`` (``TASK_PLAN.md`` rule 9): this is a screen, not copy.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from telebot import types

from app import texts
from app.callbacks import AdminNav
from app.container import Container
from app.db.models import UserStatus
from app.db.repositories import users as users_repo
from app.handlers.admin import user_card
from app.handlers.admin.nav import register_screen
from app.handlers.common import reply
from app.permissions import Permission, require
from app.states import UserStates
from app.ui import edit_or_send, fmt_left
from app.utils.pagination import Page, nav_row, paginate

logger = logging.getLogger(__name__)

#: ``expiry`` further out than this keeps a client «active» (🟢), else «soon» 🟡.
EXPIRING_WINDOW_MS = 3 * 24 * 60 * 60 * 1000

#: Filter keys offered by the filter row, in keyboard order (§S2-2.4).
FILTERS: tuple[str, ...] = ("all", "active", "blocked", "soon")

#: Filter key → short button label; the selected one is prefixed with ``•``.
FILTER_LABELS: dict[str, str] = {
    "all": "Все",
    "active": "🟢",
    "blocked": "🔴",
    "soon": "🟡 скоро",
}

ICON_BLOCKED = "⛔"
ICON_PENDING = "⏳"
ICON_ACTIVE = "🟢"
ICON_SOON = "🟡"
ICON_INACTIVE = "🔴"

#: Prompt shown by ``adm:users:s``; inline because it is screen copy (§rule 9).
SEARCH_PROMPT = "🔍 Отправьте Telegram ID или @username:"


@dataclass(frozen=True)
class UserRow:
    """One admin-list row: the ``users`` row plus its panel client (§S2-2.2).

    ``expiry_ms``/``enable`` come from the panel client and stay ``None`` when
    the user has no client (so a panel outage is indistinguishable from a
    clientless user — both render 🔴).
    """

    tg_id: int
    username: str | None
    first_name: str | None
    status: str
    expiry_ms: int | None = None
    enable: bool | None = None


def _now_ms() -> int:
    """Return the current epoch time in milliseconds."""
    return int(time.time() * 1000)


def _rows_from(users: list[Any], clients: dict[int, Any]) -> list[UserRow]:
    """Pair ``users`` rows with their panel client from ``clients``."""
    rows: list[UserRow] = []
    for user in users:
        client = clients.get(int(user.tg_id))
        rows.append(
            UserRow(
                tg_id=int(user.tg_id),
                username=user.username,
                first_name=user.first_name,
                status=str(user.status),
                expiry_ms=int(client.expiry_time) if client is not None else None,
                enable=bool(client.enable) if client is not None else None,
            )
        )
    return rows


async def _client_map(container: Container) -> dict[int, Any]:
    """Return ``{tg_id: client}`` from one panel read (§S2-2.2); ``{}`` on error."""
    panel = container.panel
    if panel is None:
        return {}
    try:
        clients = await panel.list_clients()
    except Exception:
        logger.info("admin users panel map unavailable", exc_info=True)
        return {}
    index: dict[int, Any] = {}
    for client in clients:
        email = str(getattr(client, "email", "") or "")
        if email.isdigit():
            index[int(email)] = client
    return index


async def load_rows(container: Container) -> list[UserRow]:
    """Load every user joined with the panel map, oldest first (§S2-2.2)."""
    clients = await _client_map(container)
    if container.sessionmaker is None:
        return []
    try:
        async with container.db() as session:
            users = await users_repo.list_all(session)
    except Exception:
        logger.warning("admin users list read failed", exc_info=True)
        return []
    return _rows_from(users, clients)


async def _search_rows(container: Container, query: str) -> list[UserRow]:
    """Run :func:`users_repo.search` and pair the hits with the panel map."""
    clients = await _client_map(container)
    if container.sessionmaker is None:
        return []
    try:
        async with container.db() as session:
            users = await users_repo.search(session, query)
    except Exception:
        logger.warning("admin users search failed", exc_info=True)
        return []
    return _rows_from(users, clients)


def status_icon(row: UserRow, now_ms: int) -> str:
    """Return the status icon for ``row`` (§S2-2.3).

    Blocked ⛔; pending ⏳; approved clients are judged by their panel entry:
    expired or disabled 🔴, enabled within the 3-day window 🟡, otherwise 🟢
    (a far expiry **or** an enabled ``0`` = unlimited). Any other status
    (``new``/``rejected``) and an approved user with no client are 🔴.
    """
    if row.status == UserStatus.BLOCKED:
        return ICON_BLOCKED
    if row.status == UserStatus.PENDING:
        return ICON_PENDING
    if row.status != UserStatus.APPROVED:
        return ICON_INACTIVE
    if row.expiry_ms is None or not row.enable:
        return ICON_INACTIVE
    if not row.expiry_ms:
        return ICON_ACTIVE
    left = int(row.expiry_ms) - int(now_ms)
    if left <= 0:
        return ICON_INACTIVE
    if left > EXPIRING_WINDOW_MS:
        return ICON_ACTIVE
    return ICON_SOON


def filter_rows(rows: list[UserRow], key: str, now_ms: int) -> list[UserRow]:
    """Return the ``key`` subset of ``rows`` (§S2-2.4).

    ``all`` keeps everything (including ⛔/⏳ rows, which have no dedicated
    filter); the other keys select by their icon — ``active`` 🟢, ``blocked`` 🔴
    and ``soon`` 🟡. An unknown key is treated as ``all``.
    """
    if key == "all":
        return list(rows)
    wanted = {
        "active": ICON_ACTIVE,
        "blocked": ICON_INACTIVE,
        "soon": ICON_SOON,
    }.get(key)
    if wanted is None:
        return list(rows)
    return [row for row in rows if status_icon(row, now_ms) == wanted]


def _display_name(row: UserRow) -> str:
    """Return ``@username``, else the first name, else the raw id."""
    if row.username:
        return f"@{row.username}"
    if row.first_name:
        return row.first_name
    return str(row.tg_id)


def _left_label(row: UserRow, now_ms: int) -> str:
    """Return the row's time-left text: an em dash, ∞ or :func:`fmt_left`."""
    if row.expiry_ms is None:
        return "—"
    if not row.expiry_ms:
        return "∞"
    return fmt_left(row.expiry_ms, now_ms)


def render_users_page(
    rows: list[UserRow],
    page: Page,
    *,
    filter_key: str = "all",
    now_ms: int | None = None,
) -> tuple[str, Any]:
    """Render one page plus its keyboard (§S2-2.5).

    Returns ``(text, markup)``: one full-width button per row
    (``"{icon} {name} · {left}"`` → ``adm:users:card:<tg_id>``), the filter row,
    a «🔍 Поиск» button and the prev/next navigation row.
    """
    now_ms = _now_ms() if now_ms is None else now_ms
    label = FILTER_LABELS.get(filter_key, FILTER_LABELS["all"])
    lines = [f"👥 <b>Пользователи</b> · {len(rows)}"]
    lines.append(f"Фильтр: {label} · стр. {page.index + 1}/{page.count}")
    if not rows:
        lines.append("")
        lines.append("Никого не найдено.")
    text = "\n".join(lines)

    keyboard: list[list[Any]] = []
    for row in page.items:
        caption = f"{status_icon(row, now_ms)} {_display_name(row)} · "
        caption += _left_label(row, now_ms)
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    caption,
                    callback_data=AdminNav("users", "card", str(row.tg_id)).pack(),
                )
            ]
        )

    filter_buttons = []
    for key in FILTERS:
        caption = FILTER_LABELS[key]
        if key == filter_key:
            caption = f"• {caption}"
        filter_buttons.append(
            types.InlineKeyboardButton(
                caption, callback_data=AdminNav("users", "f", key).pack()
            )
        )
    keyboard.append(filter_buttons)
    keyboard.append(
        [
            types.InlineKeyboardButton(
                "🔍 Поиск", callback_data=AdminNav("users", "s").pack()
            )
        ]
    )

    nav = nav_row(
        page,
        prev_data=AdminNav("users", "p", str(page.index - 1)).pack(),
        next_data=AdminNav("users", "p", str(page.index + 1)).pack(),
    )
    if nav:
        keyboard.append(
            [
                types.InlineKeyboardButton(caption, callback_data=data)
                for caption, data in nav
            ]
        )
    return text, types.InlineKeyboardMarkup(keyboard)


def _target(call: Any) -> tuple[int, int]:
    """Return ``(chat_id, message_id)`` of the message the callback came from."""
    message = call.message
    return int(message.chat.id), int(message.message_id)


def _route(payload: Any, *, page: int, filter_key: str) -> tuple[int, str]:
    """Resolve ``page``/``filter_key`` from a callback payload (§S2-2.5).

    Explicit keyword arguments win when ``payload`` is ``None``; otherwise the
    ``adm:users:p:<n>`` and ``adm:users:f:<key>`` actions override them.
    """
    if isinstance(payload, AdminNav):
        if payload.action == "p" and payload.arg and payload.arg.isdigit():
            page = int(payload.arg)
        elif payload.action == "f" and payload.arg in FILTER_LABELS:
            filter_key = payload.arg
    if filter_key not in FILTER_LABELS:
        filter_key = "all"
    return max(0, int(page)), filter_key


async def _set_state(bot: Any, tg_id: int, chat_id: int) -> None:
    """Put the admin into the search FSM state (best effort)."""
    try:
        await bot.set_state(tg_id, UserStates.admin_search, chat_id)
    except Exception:  # pragma: no cover - FSM backend unavailable
        logger.debug("failed to set admin search state", exc_info=True)


async def _clear_state(bot: Any, tg_id: int, chat_id: int) -> None:
    """Drop the search FSM state (best effort)."""
    try:
        await bot.delete_state(tg_id, chat_id)
    except Exception:  # pragma: no cover - FSM backend unavailable
        logger.debug("failed to clear admin search state", exc_info=True)


async def _show_search_prompt(call: Any, bot: Any, container: Container) -> None:
    """``adm:users:s`` — prompt for a query with a «✖️ Отмена» escape (§S2-2.8)."""
    chat_id, message_id = _target(call)
    await _set_state(bot, int(call.from_user.id), chat_id)
    markup = types.InlineKeyboardMarkup(
        [
            [
                types.InlineKeyboardButton(
                    "✖️ Отмена", callback_data=AdminNav("users").pack()
                )
            ]
        ]
    )
    await edit_or_send(bot, chat_id, message_id, SEARCH_PROMPT, markup=markup)


@register_screen("users")
async def list_screen(
    call: Any,
    bot: Any,
    container: Container,
    payload: Any = None,
    *,
    page: int = 0,
    filter_key: str = "all",
) -> None:
    """Render the users list (or the search prompt) in place (§S2-2.6)."""
    chat_id, message_id = _target(call)
    # The card and its one-tap actions are the same ``adm:users`` section, so
    # this screen routes them before touching the list (§S2-3.4).
    if await user_card.route(call, bot, container, payload):
        return
    if isinstance(payload, AdminNav):
        if payload.action == "s":
            await _show_search_prompt(call, bot, container)
            return
        if payload.action is None:
            # Plain ``adm:users`` is the «✖️ Отмена» escape: dropping the state
            # means the admin's next message is not swallowed by the prompt.
            await _clear_state(bot, int(call.from_user.id), chat_id)
    page, filter_key = _route(payload, page=page, filter_key=filter_key)
    rows = await load_rows(container)
    subset = filter_rows(rows, filter_key, _now_ms())
    text, markup = render_users_page(
        subset, paginate(subset, page), filter_key=filter_key
    )
    await edit_or_send(bot, chat_id, message_id, text, markup=markup)


@require(Permission.USERS_VIEW)
async def admin_search_message(message: Any, bot: Any, container: Container) -> None:
    """Run a search typed into the ``admin_search`` prompt (§S2-2.8).

    ``0`` hits replies «не найден»; one or more hits render the matching rows as
    a list page (the single-hit card itself lands in S2-3); either way the FSM
    state is dropped so the next message is not swallowed.
    """
    tg_id = int(message.from_user.id)
    chat_id = int(message.chat.id)
    query = (getattr(message, "text", None) or "").strip()
    await _clear_state(bot, tg_id, chat_id)
    rows = await _search_rows(container, query)
    if not rows:
        await reply(bot, chat_id, texts.ERROR_UNKNOWN_USER)
        return
    text, markup = render_users_page(rows, paginate(rows, 0))
    await reply(bot, chat_id, text, reply_markup=markup, parse_mode="HTML")


def register_users_handler(bot: Any, container: Container) -> None:
    """Register the ``admin_search`` message handler.

    The ``adm:`` callbacks themselves are served by the shared
    :func:`~app.handlers.admin.nav.admin_callback` dispatcher.
    """

    @bot.message_handler(state=UserStates.admin_search, content_types=["text"])
    async def _search(message: Any) -> None:  # pragma: no cover - thin adapter
        await admin_search_message(message, bot, container)


__all__ = [
    "EXPIRING_WINDOW_MS",
    "FILTERS",
    "FILTER_LABELS",
    "UserRow",
    "admin_search_message",
    "filter_rows",
    "list_screen",
    "load_rows",
    "register_users_handler",
    "render_users_page",
    "status_icon",
]
