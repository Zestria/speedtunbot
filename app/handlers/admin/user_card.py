"""Admin user card and its one-tap actions (``TASK_PLAN.md`` §S2-3).

``adm:users:card:<tg_id>`` renders one user as a card: header, status/expiry,
traffic bar and (with ``payments.view``) a payments line, plus every action the
viewer's role allows. Every action is a *mutation* callback, so each one
re-checks its own permission **inside its handler** (``USERS_EDIT``,
``USERS_BAN``, ``USERS_DELETE``) regardless of the view gate the ``adm:``
dispatcher already passed (§S2-3): hiding a button is cosmetic, the write is
what must be refused.

Actions are encoded as ``adm:users:<op>:<tg_id>`` where ``<op>`` carries both
the kind and its parameter (``gr7``/``gr30``, ``lim50``, ``liminf`` …), because
:class:`~app.callbacks.AdminNav` has a single argument slot and the target id
must be part of the payload (never the clicker's — that comes from ``call``).

Layout and copy live inline here, like the other admin screens (``rule 9``: a
screen is layout, not copy).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from telebot import types
from telebot.util import quick_markup

from app import texts
from app.callbacks import AdminNav, Confirm
from app.container import Container
from app.db.models import UserStatus
from app.db.repositories import payments as payments_repo
from app.db.repositories import users as users_repo
from app.errors import ClientNotFound
from app.handlers.common import reply
from app.handlers.confirm import register_action
from app.handlers.support import enter_writing_to_user
from app.permissions import (
    Permission,
    Role,
    check_callback,
    get_role,
    require,
    role_has,
)
from app.services.panel import ClientTraffic, gb_to_bytes
from app.services.subscriptions import UNLIMITED_REASON
from app.states import UserStates
from app.ui import bar, edit_or_send, fmt_bytes, fmt_left
from app.utils.text import esc

logger = logging.getLogger(__name__)

# --- action vocabulary (§S2-3.3) -------------------------------------------

#: ``adm:users:card:<tg_id>`` — render the card for that user.
OP_CARD = "card"
#: Preset grants: op → days.
GRANT_PRESETS: dict[str, int] = {"gr7": 7, "gr30": 30}
#: «✏️ N дней» — short FSM prompt for a hand-typed number.
OP_GRANT_CUSTOM = "grc"
OP_FREEZE = "fz"
OP_UNFREEZE = "uf"
#: Traffic-limit presets: op → GB (``None`` = ∞ / no quota).
LIMIT_PRESETS: dict[str, int | None] = {
    "lim10": 10,
    "lim50": 50,
    "lim100": 100,
    "liminf": None,
}
OP_RESET_TRAFFIC = "rt"
OP_NEWLINK = "nl"
OP_WRITE = "wr"
OP_BAN = "ban"
OP_UNBAN = "unban"
OP_DELETE = "del"

#: Every action this screen owns (the dispatcher ignores anything else).
CARD_OPS: frozenset[str] = frozenset(
    {
        OP_CARD,
        OP_GRANT_CUSTOM,
        OP_FREEZE,
        OP_UNFREEZE,
        OP_RESET_TRAFFIC,
        OP_NEWLINK,
        OP_WRITE,
        OP_BAN,
        OP_UNBAN,
        OP_DELETE,
        *GRANT_PRESETS,
        *LIMIT_PRESETS,
    }
)

#: ``cf:`` action names minted by the two confirmation flows (§S2-3.10/.13).
ACTION_SUB_REGENERATE = "user_sub_regenerate"
ACTION_DELETE = "user_delete"

#: Audit actions written by this screen (ban/unban audit themselves).
AUDIT_GRANT = "user.grant_days"
AUDIT_FREEZE = "user.freeze"
AUDIT_UNFREEZE = "user.unfreeze"
AUDIT_LIMITS = "user.limits"
AUDIT_RESET_TRAFFIC = "user.reset_traffic"
AUDIT_SUB_REGENERATE = "user.sub_regenerate"
AUDIT_WRITE = "user.write"
AUDIT_DELETE = "user.delete"

#: An approved client inside this window is "expiring" (matches S2-2/S1-1).
EXPIRING_WINDOW_MS = 3 * 24 * 60 * 60 * 1000

# --- screen copy ------------------------------------------------------------

#: Header/traffic/status prefixes of the card body (§S2-3.2).
LINE_TRAFFIC = "📊"
LINE_PAYMENTS = "💳"
#: Shown instead of the status/expiry line for a user without a panel client.
NO_CLIENT = "🔴 Нет клиента на панели"
#: Appended when the panel read failed — the card still renders (§S2-3.1).
PANEL_WARNING = "⚠️ Панель недоступна — данные могут быть неполными."
#: Shown for a user the ``users`` table does not know.
NOT_FOUND = texts.ERROR_UNKNOWN_USER

STATUS_BANNED = "⛔ Забанен"
STATUS_PENDING = "⏳ Ожидает подтверждения"
STATUS_INACTIVE = "🔴 Не активирован"
STATUS_FROZEN = "🔴 Заморожен"
STATUS_EXPIRED = "🔴 Истёк"
STATUS_EXPIRING = "🟡 Истекает"
STATUS_ACTIVE = "🟢 Активен"
STATUS_UNLIMITED = "🟢 Бессрочный"

# Button labels (§S2-3.3).
BUTTON_GRANT_7 = "➕ 7 д"
BUTTON_GRANT_30 = "➕ 30 д"
BUTTON_GRANT_CUSTOM = "✏️ N дней"
#: Perpetual marker: a grant on such a client is a no-op (§S2-3.5).
BUTTON_GRANT_CUSTOM_UNLIMITED = "✏️ N дней (∞)"
BUTTON_FREEZE = "❄️ Заморозить"
BUTTON_UNFREEZE = "🔥 Вернуть"
BUTTON_RESET_TRAFFIC = "♻️ Сбросить трафик"
BUTTON_NEWLINK = "🔄 Новая ссылка"
BUTTON_WRITE = "✉️ Написать"
BUTTON_BAN = "⛔ Бан"
BUTTON_UNBAN = "✅ Разбан"
BUTTON_DELETE = "🗑 Удалить"

#: Traffic-limit preset buttons, keyed like :data:`LIMIT_PRESETS`.
LIMIT_LABELS: dict[str, str] = {
    "lim10": "📊 10 ГБ",
    "lim50": "📊 50 ГБ",
    "lim100": "📊 100 ГБ",
    "liminf": "📊 ∞",
}

#: FSM prompt + its copy.
GRANT_PROMPT = "✏️ Отправьте число дней:"
GRANT_PROMPT_CANCEL = "✖️ Отмена"
#: FSM data key holding the target of an ``grc`` prompt (``admin_grant`` state).
GRANT_TARGET_KEY = "admin_grant_target"
#: Accepted range of a hand-typed grant (§S2-3.6).
GRANT_MIN_DAYS, GRANT_MAX_DAYS = 1, 3650
#: ``users.status_note`` recorded when the card deletes a client (§S2-3.13).
DELETED_NOTE = "deleted by admin"
GRANT_INVALID = "❌ Нужно целое число дней (1…3650)."
GRANT_DONE = "✅ Добавлено {days} дн."
GRANT_UNLIMITED = f"ℹ️ {UNLIMITED_REASON} — срок не изменён."
FROZEN_DONE = "❄️ Клиент заморожен."
UNFROZEN_DONE = "🔥 Клиент возвращён."
LIMIT_DONE = "📊 Лимит трафика обновлён."
TRAFFIC_RESET_DONE = "♻️ Трафик сброшен."
NEWLINK_CONFIRM = (
    "🔄 <b>Новая ссылка пользователя</b>\n\n"
    "⚠️ Старая ссылка перестанет работать — пользователю нужно будет заново "
    "добавить подписку.\n\nПродолжить?"
)
NEWLINK_DONE = "✅ Ссылка обновлена."
WRITE_DONE = "✉️ Режим ответа пользователю включён."
DELETE_CONFIRM = (
    "🗑 <b>Удалить пользователя</b> <code>{tg_id}</code>?\n\n"
    "Клиент будет удалён с панели, статус станет «rejected» с пометкой "
    "«deleted by admin». История платежей сохранится."
)
DELETE_DONE = "🗑 Пользователь удалён."


# --- data -------------------------------------------------------------------


@dataclass(frozen=True)
class CardData:
    """Everything the card renders: the DB row, the client and payment totals.

    ``traffic`` is ``None`` both when the user has no panel client and when the
    panel read failed — ``panel_error`` (a ready-to-render warning string, or
    ``None``) tells the two apart (§S2-3.1).
    """

    tg_id: int
    username: str | None
    first_name: str | None
    status: str
    traffic: ClientTraffic | None = None
    panel_error: str | None = None
    payments_count: int = 0
    payments_sum: int = 0

    @property
    def enable(self) -> bool:
        """Panel ``enable`` flag, ``False`` when there is no client."""
        return bool(self.traffic.enable) if self.traffic is not None else False

    @property
    def unlimited(self) -> bool:
        """True for a perpetual client (``expiry == 0`` **and** enabled)."""
        traffic = self.traffic
        return bool(traffic and traffic.enable and traffic.expiry_ms == 0)

    @property
    def display_name(self) -> str:
        """``@username``, else the first name, else the raw id."""
        if self.username:
            return f"@{self.username}"
        if self.first_name:
            return self.first_name
        return str(self.tg_id)


def _now_ms() -> int:
    """Current epoch milliseconds."""
    return int(time.time() * 1000)


async def _read_user(
    container: Container, tg_id: int
) -> tuple[str | None, str | None, str] | None:
    """Return ``(username, first_name, status)``; ``None`` when absent/unreadable.

    The row is read into plain values *inside* the session so the card never
    touches a detached ORM instance after the transaction closed.
    """
    if container.sessionmaker is None:
        return None
    try:
        async with container.db() as session:
            user = await users_repo.get(session, int(tg_id))
            if user is None:
                return None
            return user.username, user.first_name, str(user.status)
    except Exception:
        logger.warning("user card row read failed for %s", tg_id, exc_info=True)
        return None


async def _read_traffic(
    container: Container, tg_id: int
) -> tuple[ClientTraffic | None, str | None]:
    """Return ``(traffic, warning)``; a panel failure degrades to a warning."""
    panel = container.panel
    if panel is None:
        return None, PANEL_WARNING
    try:
        return await panel.get_traffic(int(tg_id)), None
    except Exception as exc:
        logger.info("user card panel read failed for %s: %s", tg_id, exc)
        return None, PANEL_WARNING


async def _read_payments(container: Container, tg_id: int) -> tuple[int, int]:
    """Return ``(count, sum)`` of the user's approved payments (§S2-3.1)."""
    if container.sessionmaker is None:
        return 0, 0
    try:
        async with container.db() as session:
            return await payments_repo.user_totals(session, int(tg_id))
    except Exception:
        logger.warning("user card payments read failed for %s", tg_id, exc_info=True)
        return 0, 0


async def load_card(container: Container, tg_id: int) -> CardData | None:
    """Collect the card's data; ``None`` when the user is unknown (§S2-3.1).

    Each of the three reads is independent: a panel outage only replaces the
    traffic snapshot with a warning line, and a failing payments query only
    zeroes that line — the card still renders.
    """
    tg_id = int(tg_id)
    row = await _read_user(container, tg_id)
    if row is None:
        return None
    username, first_name, status = row
    traffic, panel_error = await _read_traffic(container, tg_id)
    count, total = await _read_payments(container, tg_id)
    return CardData(
        tg_id=tg_id,
        username=username,
        first_name=first_name,
        status=status,
        traffic=traffic,
        panel_error=panel_error,
        payments_count=count,
        payments_sum=total,
    )


# --- rendering (§S2-3.2) ----------------------------------------------------


def _date(expiry_ms: int, timezone: str) -> str:
    """Render ``expiry_ms`` as ``DD.MM.YYYY`` in the configured timezone."""
    return datetime.fromtimestamp(expiry_ms / 1000, tz=ZoneInfo(timezone)).strftime(
        "%d.%m.%Y"
    )


def _status_line(data: CardData, now_ms: int, timezone: str) -> str:
    """Return the status/expiry line, mirroring the icon rules of §S2-2.

    The DB status wins first (banned/pending/other), then the panel client:
    missing → no client, disabled → frozen, ``expiry == 0`` → perpetual, and a
    positive expiry is expired / soon (≤ 3 days) / active.
    """
    if data.status == UserStatus.BLOCKED:
        return STATUS_BANNED
    if data.status == UserStatus.PENDING:
        return STATUS_PENDING
    if data.status != UserStatus.APPROVED:
        return STATUS_INACTIVE
    traffic = data.traffic
    if traffic is None:
        return NO_CLIENT
    if not traffic.enable:
        return STATUS_FROZEN
    if not traffic.expiry_ms:
        return STATUS_UNLIMITED
    stamp = _date(int(traffic.expiry_ms), timezone)
    left = fmt_left(int(traffic.expiry_ms), now_ms)
    if not left or left == "истёк":
        return f"{STATUS_EXPIRED} · до {stamp}"
    word = (
        STATUS_EXPIRING
        if int(traffic.expiry_ms) - int(now_ms) <= EXPIRING_WINDOW_MS
        else STATUS_ACTIVE
    )
    return f"{word} · до {stamp} ({left})"


def _traffic_line(traffic: ClientTraffic) -> str:
    """Return the ``📊`` line with a bar when a quota is set (§S2-3.2)."""
    used = fmt_bytes(traffic.used)
    if traffic.total <= 0:
        return f"{LINE_TRAFFIC} {used} / ∞"
    return (
        f"{LINE_TRAFFIC} {bar(traffic.used / traffic.total)} "
        f"{used} / {fmt_bytes(traffic.total)}"
    )


def render_card(
    data: CardData, now_ms: int, *, timezone: str, can_view_payments: bool
) -> str:
    """Render the user card body (§S2-3.2).

    The payments line is emitted **only** with ``payments.view``, so a support
    user never sees the money figures even though the caller read them.
    """
    lines = [
        f"👤 <b>{esc(data.display_name)}</b> · <code>{data.tg_id}</code>",
        _status_line(data, now_ms, timezone),
    ]
    if data.traffic is not None:
        lines.append(_traffic_line(data.traffic))
    if data.panel_error:
        lines.append(data.panel_error)
    if can_view_payments:
        lines.append(
            f"{LINE_PAYMENTS} Платежей: {data.payments_count} · {data.payments_sum} ₽"
        )
    return "\n".join(lines)


# --- keyboard (§S2-3.3) -----------------------------------------------------


def _button(caption: str, op: str, tg_id: int) -> Any:
    """Build one card button: ``adm:users:<op>:<tg_id>``."""
    return types.InlineKeyboardButton(
        caption, callback_data=AdminNav("users", op, str(tg_id)).pack()
    )


def card_keyboard(
    role: Role | str | None,
    tg_id: int,
    *,
    enable: bool = False,
    unlimited: bool = False,
) -> Any:
    """Build the card keyboard, filtered by ``role`` (§S2-3.3).

    ``enable`` picks the ❄️/🔥 and ⛔/✅ label pairs (a disabled client is either
    frozen or banned — the button offers the opposite transition either way), and
    ``unlimited`` marks the custom-grant button ``(∞)`` because a grant on a
    perpetual client changes nothing (§S2-3.5). Every button is a mutation
    callback that re-checks its own permission in its handler, so ``support``
    (``users.view`` only) gets a view-only card.
    """
    rows: list[list[Any]] = []
    if role_has(role, Permission.USERS_EDIT):
        custom = BUTTON_GRANT_CUSTOM_UNLIMITED if unlimited else BUTTON_GRANT_CUSTOM
        rows.append(
            [
                _button(BUTTON_GRANT_7, "gr7", tg_id),
                _button(BUTTON_GRANT_30, "gr30", tg_id),
                _button(custom, OP_GRANT_CUSTOM, tg_id),
            ]
        )
        rows.append(
            [
                _button(
                    BUTTON_FREEZE if enable else BUTTON_UNFREEZE,
                    OP_FREEZE if enable else OP_UNFREEZE,
                    tg_id,
                )
            ]
        )
        rows.append([_button(LIMIT_LABELS[op], op, tg_id) for op in LIMIT_PRESETS])
        rows.append(
            [
                _button(BUTTON_RESET_TRAFFIC, OP_RESET_TRAFFIC, tg_id),
                _button(BUTTON_NEWLINK, OP_NEWLINK, tg_id),
            ]
        )
        rows.append([_button(BUTTON_WRITE, OP_WRITE, tg_id)])
    if role_has(role, Permission.USERS_BAN):
        rows.append(
            [
                _button(
                    BUTTON_BAN if enable else BUTTON_UNBAN,
                    OP_BAN if enable else OP_UNBAN,
                    tg_id,
                )
            ]
        )
    if role_has(role, Permission.USERS_DELETE):
        rows.append([_button(BUTTON_DELETE, OP_DELETE, tg_id)])
    rows.append(
        [
            types.InlineKeyboardButton(
                texts.BUTTON_BACK, callback_data=AdminNav("users").pack()
            )
        ]
    )
    return types.InlineKeyboardMarkup(rows)


BAN_DONE_TOAST = "⛔ Пользователь забанен."
UNBAN_DONE_TOAST = "✅ Пользователь разбанен."
#: Permission each op re-checks inside its handler (§S2-3).
OP_PERMISSIONS: dict[str, Permission] = {
    **dict.fromkeys(GRANT_PRESETS, Permission.USERS_EDIT),
    OP_GRANT_CUSTOM: Permission.USERS_EDIT,
    OP_FREEZE: Permission.USERS_EDIT,
    OP_UNFREEZE: Permission.USERS_EDIT,
    OP_RESET_TRAFFIC: Permission.USERS_EDIT,
    OP_NEWLINK: Permission.USERS_EDIT,
    OP_WRITE: Permission.USERS_EDIT,
    **dict.fromkeys(LIMIT_PRESETS, Permission.USERS_EDIT),
    OP_BAN: Permission.USERS_BAN,
    OP_UNBAN: Permission.USERS_BAN,
    OP_DELETE: Permission.USERS_DELETE,
}


# --- plumbing ---------------------------------------------------------------


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


async def _audit(
    container: Container, actor: int, action: str, tg_id: int, **details: Any
) -> None:
    """Write one audit row for a card action (best effort, never raises)."""
    audit = container.audit
    if audit is None:
        return
    role = await get_role(actor)
    await audit.log(
        actor,
        action,
        "user",
        tg_id,
        role=None if role is None else str(role),
        **details,
    )


async def _notice(container: Container, target: int, text: str) -> None:
    """Best-effort direct message to the user the card acts upon."""
    notifier = container.notifier
    if notifier is not None:
        await notifier.safe_send(int(target), text, parse_mode="HTML")


async def _render_at(
    bot: Any,
    chat_id: int,
    message_id: int | None,
    container: Container,
    tg_id: int,
    viewer_id: int,
) -> None:
    """Render the card for ``tg_id`` into a message, as ``viewer_id`` sees it."""
    data = await load_card(container, tg_id)
    if data is None:
        await edit_or_send(bot, chat_id, message_id, NOT_FOUND)
        return
    role = await get_role(viewer_id)
    text = render_card(
        data,
        _now_ms(),
        timezone=container.settings.timezone,
        can_view_payments=role_has(role, Permission.PAYMENTS_VIEW),
    )
    markup = card_keyboard(
        role,
        tg_id,
        enable=data.enable,
        unlimited=data.unlimited,
    )
    await edit_or_send(bot, chat_id, message_id, text, markup=markup)


async def _render_card(call: Any, bot: Any, container: Container, tg_id: int) -> None:
    """Re-render the card in place after an action."""
    chat_id, message_id = _target(call)
    await _render_at(bot, chat_id, message_id, container, tg_id, int(call.from_user.id))


async def card_callback(call: Any, bot: Any, container: Container, tg_id: int) -> None:
    """``adm:users:card:<tg_id>`` — render the card (§S2-3.4).

    Re-checks ``USERS_VIEW`` even though the ``adm:`` dispatcher already gated
    the section: a card is a *view* surface, and a forged payload must be
    refused here too.
    """
    if not await check_callback(call, Permission.USERS_VIEW):
        return
    # Opening a card is also the escape hatch of the ``grc`` prompt, so the
    # pending day-count state is dropped here (§S2-3.6).
    chat_id, _ = _target(call)
    await _clear_state(bot, int(call.from_user.id), chat_id)
    await _render_card(call, bot, container, int(tg_id))
    await _answer(bot, call, None)


async def route(call: Any, bot: Any, container: Container, payload: Any) -> bool:
    """Handle a card payload if ``payload`` is one; return whether it was ours.

    The ``adm:users`` screen calls this first, so ``card`` and every action op
    stay inside the single ``adm:`` callback handler (§S2-3.4).
    """
    if not isinstance(payload, AdminNav) or payload.action not in CARD_OPS:
        return False
    if not payload.arg or not payload.arg.isdigit():
        await _answer(bot, call, texts.ERROR_STALE_BUTTON)
        return True
    tg_id = int(payload.arg)
    if payload.action == OP_CARD:
        await card_callback(call, bot, container, tg_id)
    else:
        await _run_action(call, bot, container, payload.action, tg_id)
    return True


async def _run_action(
    call: Any, bot: Any, container: Container, op: str, tg_id: int
) -> None:
    """Authorise ``op`` and dispatch it to its handler (§S2-3.5–.13).

    The permission is checked **here**, per op (``USERS_EDIT``/``USERS_BAN``/
    ``USERS_DELETE``) and independently of the section's ``USERS_VIEW`` gate, so
    a forged or stale button from a role that may only view is refused.
    """
    permission = OP_PERMISSIONS.get(op)
    if permission is None:  # pragma: no cover - CARD_OPS is closed
        await _answer(bot, call, texts.ERROR_STALE_BUTTON)
        return
    if not await check_callback(call, permission):
        return

    if op in GRANT_PRESETS:
        await _grant(call, bot, container, tg_id, GRANT_PRESETS[op])
    elif op == OP_GRANT_CUSTOM:
        await _prompt_custom_grant(call, bot, container, tg_id)
    elif op in (OP_FREEZE, OP_UNFREEZE):
        await _freeze(call, bot, container, tg_id, frozen=op == OP_FREEZE)
    elif op in LIMIT_PRESETS:
        await _set_limit(call, bot, container, tg_id, LIMIT_PRESETS[op])
    elif op == OP_RESET_TRAFFIC:
        await _reset_traffic(call, bot, container, tg_id)
    elif op == OP_NEWLINK:
        await _request_newlink(call, bot, container, tg_id)
    elif op == OP_WRITE:
        await _write_to_user(call, bot, container, tg_id)
    elif op in (OP_BAN, OP_UNBAN):
        await _toggle_ban(call, bot, container, tg_id, ban=op == OP_BAN)
    else:
        await _request_delete(call, bot, container, tg_id)


# --- one-tap actions (§S2-3.5–.13) -----------------------------------------


def _role_str(role: Role | None) -> str | None:
    """Return the audit-friendly role name (``None`` for non-staff)."""
    return None if role is None else str(role)


async def _set_state(bot: Any, tg_id: int, chat_id: int) -> None:
    """Enter the ``admin_grant`` FSM state (best effort)."""
    try:
        await bot.set_state(tg_id, UserStates.admin_grant, chat_id)
    except Exception:  # pragma: no cover - FSM backend unavailable
        logger.debug("failed to set admin grant state", exc_info=True)


async def _clear_state(bot: Any, tg_id: int, chat_id: int) -> None:
    """Leave the ``admin_grant`` FSM state (best effort)."""
    try:
        await bot.delete_state(tg_id, chat_id)
    except Exception:  # pragma: no cover - FSM backend unavailable
        logger.debug("failed to clear admin grant state", exc_info=True)


async def _grant_days(
    container: Container, actor: int, tg_id: int, days: int
) -> Any | None:
    """Run :meth:`SubscriptionService.grant_days`; ``None`` when it failed.

    ``None`` covers both a missing service and a panel error — the caller shows
    :data:`texts.ERROR_PANEL` either way.
    """
    subscriptions = container.subscriptions
    if subscriptions is None:
        return None
    try:
        return await subscriptions.grant_days(int(tg_id), int(days), actor=int(actor))
    except Exception as exc:
        logger.warning("grant_days(%s) failed for %s: %s", days, tg_id, exc)
        return None


async def _grant(
    call: Any, bot: Any, container: Container, tg_id: int, days: int
) -> None:
    """``gr7``/``gr30`` — add preset days to the subscription (§S2-3.5).

    A perpetual client reports ``changed=False`` (:data:`UNLIMITED_REASON`); the
    card is re-rendered either way and the admin gets the matching toast.
    """
    actor = int(call.from_user.id)
    result = await _grant_days(container, actor, tg_id, days)
    if result is None:
        await _render_card(call, bot, container, tg_id)
        await _answer(bot, call, texts.ERROR_PANEL, alert=True)
        return
    await _audit(
        container,
        actor,
        AUDIT_GRANT,
        tg_id,
        days=int(days),
        changed=bool(result.changed),
        expiry_ms=int(result.expiry_ms),
    )
    await _render_card(call, bot, container, tg_id)
    if not result.changed:
        await _answer(bot, call, GRANT_UNLIMITED, alert=True)
        return
    await _answer(bot, call, GRANT_DONE.format(days=int(days)))


async def _prompt_custom_grant(
    call: Any, bot: Any, container: Container, tg_id: int
) -> None:
    """``grc`` — ask for a day count, remembering the target in the FSM (§S2-3.6).

    The escape button is ``adm:users:card:<tg_id>`` itself, so cancelling simply
    re-opens the card (which also drops the state).
    """
    actor = int(call.from_user.id)
    chat_id, message_id = _target(call)
    await _set_state(bot, actor, chat_id)
    async with bot.retrieve_data(actor, chat_id) as data:
        data[GRANT_TARGET_KEY] = int(tg_id)
    markup = types.InlineKeyboardMarkup(
        [
            [
                types.InlineKeyboardButton(
                    GRANT_PROMPT_CANCEL,
                    callback_data=AdminNav("users", OP_CARD, str(tg_id)).pack(),
                )
            ]
        ]
    )
    await edit_or_send(bot, chat_id, message_id, GRANT_PROMPT, markup=markup)
    await _answer(bot, call, None)


async def _freeze(
    call: Any, bot: Any, container: Container, tg_id: int, *, frozen: bool
) -> None:
    """``fz``/``uf`` — disable/enable the panel client (§S2-3.7)."""
    actor = int(call.from_user.id)
    subscriptions = container.subscriptions
    if subscriptions is None:
        await _answer(bot, call, texts.ERROR_GENERIC)
        return
    try:
        await subscriptions.freeze(int(tg_id), bool(frozen))
    except Exception as exc:
        logger.warning("freeze(%s) failed for %s: %s", frozen, tg_id, exc)
        await _render_card(call, bot, container, tg_id)
        await _answer(bot, call, texts.ERROR_PANEL, alert=True)
        return
    await _audit(
        container,
        actor,
        AUDIT_FREEZE if frozen else AUDIT_UNFREEZE,
        tg_id,
        frozen=bool(frozen),
    )
    await _render_card(call, bot, container, tg_id)
    await _answer(bot, call, FROZEN_DONE if frozen else UNFROZEN_DONE)


async def _set_limit(
    call: Any, bot: Any, container: Container, tg_id: int, gb: int | None
) -> None:
    """``lim10``/``lim50``/``lim100``/``liminf`` — set the traffic quota (§S2-3.8).

    ``gb`` is converted with :func:`~app.services.panel.gb_to_bytes`, so ``∞``
    becomes ``0`` = no quota — the gateway never sees raw gigabytes.
    """
    actor = int(call.from_user.id)
    panel = container.panel
    if panel is None:
        await _answer(bot, call, texts.ERROR_GENERIC)
        return
    total_bytes = gb_to_bytes(gb)
    try:
        await panel.set_limits(int(tg_id), total_bytes)
    except Exception as exc:
        logger.warning("set_limits failed for %s: %s", tg_id, exc)
        await _render_card(call, bot, container, tg_id)
        await _answer(bot, call, texts.ERROR_PANEL, alert=True)
        return
    await _audit(
        container, actor, AUDIT_LIMITS, tg_id, gb=gb, total_bytes=int(total_bytes)
    )
    await _render_card(call, bot, container, tg_id)
    await _answer(bot, call, LIMIT_DONE)


async def _reset_traffic(call: Any, bot: Any, container: Container, tg_id: int) -> None:
    """``rt`` — zero the client's up/down counters (§S2-3.9)."""
    actor = int(call.from_user.id)
    panel = container.panel
    if panel is None:
        await _answer(bot, call, texts.ERROR_GENERIC)
        return
    try:
        await panel.reset_traffic(int(tg_id))
    except Exception as exc:
        logger.warning("reset_traffic failed for %s: %s", tg_id, exc)
        await _render_card(call, bot, container, tg_id)
        await _answer(bot, call, texts.ERROR_PANEL, alert=True)
        return
    await _audit(container, actor, AUDIT_RESET_TRAFFIC, tg_id)
    await _render_card(call, bot, container, tg_id)
    await _answer(bot, call, TRAFFIC_RESET_DONE)


async def _request_newlink(
    call: Any, bot: Any, container: Container, tg_id: int
) -> None:
    """``nl`` — ask for confirmation before killing the old link (§S2-3.10).

    Nothing is written yet: the token (bound to the admin + ``tg_id``) is
    consumed by :func:`confirm_sub_regenerate` through the central ``cf:``
    dispatcher, so the old ``sub_id`` keeps working until «✅ Подтвердить».
    """
    store = container.confirmations
    if store is None:  # pragma: no cover - container is wired at startup
        await _answer(bot, call, texts.ERROR_GENERIC)
        return
    token = store.create(int(call.from_user.id), ACTION_SUB_REGENERATE, int(tg_id))
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
    await edit_or_send(bot, chat_id, message_id, NEWLINK_CONFIRM, markup=markup)
    await _answer(bot, call, None)


async def _write_to_user(call: Any, bot: Any, container: Container, tg_id: int) -> None:
    """``wr`` — enter the support-reply mode aimed at ``tg_id`` (§S2-3.11)."""
    actor = int(call.from_user.id)
    chat_id, message_id = _target(call)
    await enter_writing_to_user(
        bot, chat_id, container, actor, int(tg_id), message_id=message_id
    )
    await _audit(container, actor, AUDIT_WRITE, tg_id)
    await _answer(bot, call, WRITE_DONE)


async def _toggle_ban(
    call: Any, bot: Any, container: Container, tg_id: int, *, ban: bool
) -> None:
    """``ban``/``unban`` — flip ``users.status`` via :class:`UserService` (§S2-3.12).

    The service writes the ``user.ban``/``user.unban`` audit row itself, so this
    handler only adds the user notice + the matching toast. Staff can never be
    banned, exactly like ``/ban`` (§M0-09.5).
    """
    actor = int(call.from_user.id)
    users = container.users
    if users is None:
        await _answer(bot, call, texts.ERROR_GENERIC)
        return
    if ban and await get_role(int(tg_id)) is not None:
        await _answer(bot, call, texts.BAN_STAFF_REFUSED, alert=True)
        return
    role = _role_str(await get_role(actor))
    try:
        if ban:
            change = await users.ban(int(tg_id), actor=actor, role=role)
        else:
            change = await users.unban(int(tg_id), actor=actor, role=role)
    except Exception as exc:
        logger.warning("ban/unban failed for %s: %s", tg_id, exc)
        await _render_card(call, bot, container, tg_id)
        await _answer(bot, call, texts.ERROR_PANEL, alert=True)
        return
    if not change.found:
        await _answer(bot, call, texts.ERROR_UNKNOWN_USER, alert=True)
        return
    await _notice(
        container,
        int(tg_id),
        texts.BAN_USER_NOTICE if ban else texts.UNBAN_USER_NOTICE,
    )
    await _render_card(call, bot, container, tg_id)
    await _answer(bot, call, BAN_DONE_TOAST if ban else UNBAN_DONE_TOAST)


async def _request_delete(
    call: Any, bot: Any, container: Container, tg_id: int
) -> None:
    """``del`` — ask for confirmation before deleting (§S2-3.13)."""
    store = container.confirmations
    if store is None:  # pragma: no cover - container is wired at startup
        await _answer(bot, call, texts.ERROR_GENERIC)
        return
    token = store.create(int(call.from_user.id), ACTION_DELETE, int(tg_id))
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
    await edit_or_send(
        bot, chat_id, message_id, DELETE_CONFIRM.format(tg_id=int(tg_id)), markup=markup
    )
    await _answer(bot, call, None)


# --- confirmations (§S2-3.10, §S2-3.13) -------------------------------------


@register_action(ACTION_SUB_REGENERATE)
async def confirm_sub_regenerate(
    call: Any, bot: Any, container: Container, tg_id: int
) -> None:
    """«✅ Подтвердить» for ``nl`` — regenerate the ``sub_id`` (§S2-3.10).

    Runs through the central ``cf:`` dispatcher, which already proved the token
    belongs to this admin, and re-checks ``USERS_EDIT`` because a role may have
    been revoked between the question and the answer. A panel failure leaves the
    old ``sub_id`` untouched (nothing was written).
    """
    if not await check_callback(call, Permission.USERS_EDIT):
        return
    panel = container.panel
    if panel is None:  # pragma: no cover - container is wired at startup
        await _answer(bot, call, texts.ERROR_GENERIC)
        return
    actor = int(call.from_user.id)
    try:
        sub_id = await panel.regenerate_sub_id(int(tg_id))
    except Exception as exc:
        logger.warning("panel failure regenerating link for %s: %s", tg_id, exc)
        await _render_card(call, bot, container, int(tg_id))
        await _answer(bot, call, texts.ERROR_PANEL, alert=True)
        return
    await _audit(container, actor, AUDIT_SUB_REGENERATE, tg_id, sub_id=sub_id)
    await _render_card(call, bot, container, int(tg_id))
    await _answer(bot, call, NEWLINK_DONE)


@register_action(ACTION_DELETE)
async def confirm_delete(call: Any, bot: Any, container: Container, tg_id: int) -> None:
    """«✅ Подтвердить» for ``del`` — drop the client, keep the history (§S2-3.13).

    The panel client is removed first; only then is the user row flipped to
    ``rejected`` (``note="deleted by admin"``), so a panel failure never leaves a
    live client with no user row. The payments rows are never touched.
    """
    if not await check_callback(call, Permission.USERS_DELETE):
        return
    actor = int(call.from_user.id)
    panel = container.panel
    if panel is not None:
        try:
            await panel.delete_client(int(tg_id))
        except ClientNotFound:
            pass  # already gone — deleting twice is not an error
        except Exception as exc:
            logger.warning("panel failure deleting %s: %s", tg_id, exc)
            await _answer(bot, call, texts.ERROR_PANEL, alert=True)
            return
    await _mark_rejected(container, int(tg_id), actor)
    await _audit(container, actor, AUDIT_DELETE, tg_id)
    # The card's user is gone, so the list it was opened from is re-rendered.
    await _show_users_list(call, bot, container)
    await _answer(bot, call, DELETE_DONE)


async def _mark_rejected(container: Container, tg_id: int, actor: int) -> None:
    """Record the deletion in the ``users`` row (best effort)."""
    if container.sessionmaker is None:
        return
    try:
        async with container.db() as session:
            await users_repo.set_status(
                session,
                int(tg_id),
                UserStatus.REJECTED,
                changed_by=int(actor),
                note=DELETED_NOTE,
            )
    except Exception:
        logger.warning("failed to mark %s rejected", tg_id, exc_info=True)


async def _show_users_list(call: Any, bot: Any, container: Container) -> None:
    """Re-render the users list the card was opened from (lazy: no cycle)."""
    from app.handlers.admin.users import list_screen

    await list_screen(call, bot, container)


# --- typed days prompt (§S2-3.6) --------------------------------------------


@require(Permission.USERS_EDIT)
async def admin_grant_message(message: Any, bot: Any, container: Container) -> None:
    """Apply the day count typed after ``grc`` (§S2-3.6).

    The target comes from the FSM data written by :func:`_prompt_custom_grant`,
    never from the message text (a stray number must not pick a victim). The
    state is dropped either way, so a mistyped reply does not swallow the next
    message; the prompt message is then edited into the refreshed card.
    """
    actor = int(message.from_user.id)
    chat_id = int(message.chat.id)
    text = (getattr(message, "text", None) or "").strip()
    async with bot.retrieve_data(actor, chat_id) as data:
        target = data.get(GRANT_TARGET_KEY)
    await _clear_state(bot, actor, chat_id)

    if not target:
        await reply(bot, chat_id, texts.ERROR_UNKNOWN_USER)
        return
    if not text.isdigit() or not GRANT_MIN_DAYS <= int(text) <= GRANT_MAX_DAYS:
        await reply(bot, chat_id, GRANT_INVALID)
        return

    days = int(text)
    result = await _grant_days(container, actor, int(target), days)
    if result is None:
        await reply(bot, chat_id, texts.ERROR_PANEL)
        return
    await _audit(
        container,
        actor,
        AUDIT_GRANT,
        int(target),
        days=days,
        changed=bool(result.changed),
        expiry_ms=int(result.expiry_ms),
    )
    await _render_at(
        bot,
        chat_id,
        getattr(message, "message_id", None),
        container,
        int(target),
        actor,
    )
    if not result.changed:
        await reply(bot, chat_id, GRANT_UNLIMITED)
        return
    await reply(bot, chat_id, GRANT_DONE.format(days=days))


def register_user_card_handler(bot: Any, container: Container) -> None:
    """Register the ``admin_grant`` message handler.

    The card's buttons themselves are served by the shared ``adm:`` dispatcher:
    :func:`route` is called by the ``adm:users`` screen (§S2-3.4).
    """

    @bot.message_handler(state=UserStates.admin_grant, content_types=["text"])
    async def _grant(message: Any) -> None:  # pragma: no cover - thin adapter
        await admin_grant_message(message, bot, container)


__all__ = [
    "CARD_OPS",
    "CardData",
    "card_callback",
    "card_keyboard",
    "confirm_delete",
    "confirm_sub_regenerate",
    "load_card",
    "register_user_card_handler",
    "render_card",
    "route",
]
