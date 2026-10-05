"""Admin «Платежи» section: queue, history and stats (``TASK_PLAN.md`` §S2-4).

One ``adm:payments`` screen with four payloads, mirroring the users list (§S2-2):
the home card, the pending queue, the history page and the stats card.

A queue row opens the **standard** review card — the text comes from
:meth:`app.services.payments.PaymentService.render_review_card` and its buttons
from :meth:`~app.services.payments.PaymentService.review_markup` — and the card
is *delivered* through :meth:`~app.services.notifier.Notifier.send_card`, so it
is registered as a card copy and the existing approve/decline handlers
(``pay:ok`` / ``pay:no``) keep it in sync when the decision lands.

Every figure is an ``approved`` **and applied** payment (``payments_repo.stats``),
the same definition as the dashboard's 30-day counter, so declined/revoked
attempts can never inflate a number. Labels, windows and the chart live inline
here (``TASK_PLAN.md`` rule 9: a screen is layout, not copy).
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from telebot import types

from app import texts
from app.callbacks import AdminNav
from app.container import Container
from app.db.base import utcnow
from app.db.models import CardKind, Payment, PaymentStatus
from app.db.repositories import payments as payments_repo
from app.db.repositories import users as users_repo
from app.db.repositories.payments import PENDING_STATUSES
from app.handlers.admin.nav import register_screen
from app.permissions import Permission, check_callback
from app.ui import bar, edit_or_send
from app.utils.pagination import Page, nav_row, paginate

logger = logging.getLogger(__name__)

# --- action vocabulary (§S2-4.2) ---------------------------------------------

#: ``adm:payments:p:<page>`` — the pending queue.
OP_PENDING = "p"
#: ``adm:payments:h:<page>`` — the history page.
OP_HISTORY = "h"
#: ``adm:payments:st`` — the stats card.
OP_STATS = "st"
#: ``adm:payments:c:<id>`` — send the standard review card for one payment.
OP_CARD = "c"

#: Statuses the history page lists: everything already decided (§S2-4.4).
HISTORY_STATUSES: tuple[PaymentStatus, ...] = (
    PaymentStatus.APPROVED,
    PaymentStatus.DECLINED,
    PaymentStatus.CANCELLED,
    PaymentStatus.EXPIRED,
    PaymentStatus.REVOKED,
)

#: History-row icon per status (§S2-4.4).
STATUS_ICONS: dict[str, str] = {
    PaymentStatus.APPROVED: "✅",
    PaymentStatus.DECLINED: "❌",
    PaymentStatus.CANCELLED: "🚫",
    PaymentStatus.EXPIRED: "⌛",
    PaymentStatus.REVOKED: "🔁",
}

#: Days drawn by the mini bar chart — also the «7 дней» window (§S2-4.5).
CHART_DAYS = 7
#: Calendar days behind the «30 дней» figure.
MONTH_DAYS = 30

#: Lower bound of the «Всего» window — earlier than any stored ``applied_at``.
EPOCH = datetime(1970, 1, 1)


# --- stats (§S2-4.5) --------------------------------------------------------


@dataclass(frozen=True)
class WindowStats:
    """One stat window: its label plus the approved count and revenue sum."""

    label: str
    count: int
    revenue: int


@dataclass(frozen=True)
class Stats:
    """The stats card's data: the four windows and the 7-day chart."""

    windows: tuple[WindowStats, ...]
    #: ``(DD.MM, revenue)`` per day, oldest first.
    days: tuple[tuple[str, int], ...]


def _zone(container: Container) -> ZoneInfo:
    """Return the configured timezone (falls back to UTC when misconfigured)."""
    try:
        return ZoneInfo(container.settings.timezone)
    except Exception:  # pragma: no cover - settings validation already guards this
        return ZoneInfo("UTC")


def _day_start(local_day: date, tz: ZoneInfo) -> datetime:
    """Return naive-UTC midnight of the local calendar day ``local_day``."""
    start = datetime.combine(local_day, time.min, tzinfo=tz)
    return start.astimezone(UTC).replace(tzinfo=None)


async def collect_stats(container: Container, *, now: datetime | None = None) -> Stats:
    """Collect the stat windows and the 7-day chart (§S2-4.5).

    Every window is a run of **calendar** days in the configured timezone, and
    each one starts at local midnight: «Сегодня» at today's, «7 дней» six days
    back and «30 дней» twenty-nine days back. The chart then walks the same
    boundaries, so a day line is ``rev(day) - rev(previous day)`` and the seven
    bars add up *exactly* to the «7 дней» figure — one read
    (:func:`~app.db.repositories.payments.stats`) backs both. A failing read
    degrades the whole card to zeros instead of erroring out.
    """
    now = utcnow() if now is None else now
    tz = _zone(container)
    local_today = now.replace(tzinfo=UTC).astimezone(tz).date()
    days = [local_today - timedelta(days=back) for back in range(CHART_DAYS)]
    starts = [_day_start(day, tz) for day in days]
    month_start = _day_start(local_today - timedelta(days=MONTH_DAYS - 1), tz)
    empty = Stats(
        windows=(
            WindowStats("Сегодня", 0, 0),
            WindowStats("7 дней", 0, 0),
            WindowStats("30 дней", 0, 0),
            WindowStats("Всего", 0, 0),
        ),
        days=tuple((day.strftime("%d.%m"), 0) for day in reversed(days)),
    )
    if container.sessionmaker is None:
        return empty
    try:
        async with container.db() as session:
            # ``cumulative[i]`` counts everything applied since the midnight that
            # opened day ``i`` (0 = today), so day ``i`` is the difference from
            # the day before it — and day 0 is the boundary itself.
            cumulative = [
                await payments_repo.stats(session, since=start) for start in starts
            ]
            month = await payments_repo.stats(session, since=month_start)
            total = await payments_repo.stats(session, since=EPOCH)
    except Exception:
        logger.warning("payments stats read failed", exc_info=True)
        return empty

    daily = tuple(
        (
            days[index].strftime("%d.%m"),
            max(
                0,
                cumulative[index][1] - (cumulative[index - 1][1] if index else 0),
            ),
        )
        for index in range(CHART_DAYS)
    )
    return Stats(
        windows=(
            WindowStats("Сегодня", cumulative[0][0], cumulative[0][1]),
            WindowStats(
                "7 дней", cumulative[CHART_DAYS - 1][0], cumulative[CHART_DAYS - 1][1]
            ),
            WindowStats("30 дней", month[0], month[1]),
            WindowStats("Всего", total[0], total[1]),
        ),
        days=tuple(reversed(daily)),
    )


def render_stats(stats: Stats) -> str:
    """Render the stats card body (§S2-4.5): windows, then one bar per day."""
    lines = ["📈 <b>Статистика платежей</b>", ""]
    lines += [
        f"{window.label}: {window.count} · {window.revenue} ₽"
        for window in stats.windows
    ]
    lines += ["", "📊 За 7 дней:"]
    peak = max((revenue for _, revenue in stats.days), default=0)
    for label, revenue in stats.days:
        fraction = revenue / peak if peak else 0.0
        lines.append(f"{label} {bar(fraction)} {revenue} ₽")
    return "\n".join(lines)


# --- list rendering (§S2-4.3/§S2-4.4) ---------------------------------------


def _display_name(username: str | None) -> str:
    """Return ``@username``, else the «нет @username» placeholder."""
    return f"@{username}" if username else texts.PAYMENT_NO_USERNAME


def _short_date(moment: datetime | None, tz: ZoneInfo) -> str:
    """Render ``moment`` (naive UTC) as ``DD.MM`` in ``tz``, else an em dash."""
    if moment is None:
        return "—"
    return moment.replace(tzinfo=UTC).astimezone(tz).strftime("%d.%m")


def _back_row() -> list[Any]:
    """Return the «⬅️ Назад» row pointing at the section home."""
    return [
        types.InlineKeyboardButton(
            texts.BUTTON_BACK, callback_data=AdminNav("payments").pack()
        )
    ]


def render_pending_page(
    payments: list[Payment],
    page: Page,
    usernames: dict[int, str | None],
) -> tuple[str, Any]:
    """Render one pending-queue page plus its keyboard (§S2-4.3).

    Returns ``(text, markup)``: one full-width button per payment
    (``#id · tariff · @user`` → ``adm:payments:c:<id>``), the prev/next row and
    «⬅️ Назад».
    """
    lines = [f"⏳ <b>Ожидают проверки</b> · {len(payments)}"]
    lines.append(f"стр. {page.index + 1}/{page.count}")
    if not payments:
        lines += ["", "Нет заявок на проверке."]
    text = "\n".join(lines)

    keyboard: list[list[Any]] = []
    for payment in page.items:
        caption = (
            f"#{payment.id} · {payment.tariff_name} · "
            f"{_display_name(usernames.get(int(payment.user_tg_id)))}"
        )
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    caption,
                    callback_data=AdminNav("payments", OP_CARD, str(payment.id)).pack(),
                )
            ]
        )
    keyboard.append(
        [
            types.InlineKeyboardButton(caption, callback_data=data)
            for caption, data in nav_row(
                page,
                prev_data=AdminNav("payments", OP_PENDING, str(page.index - 1)).pack(),
                next_data=AdminNav("payments", OP_PENDING, str(page.index + 1)).pack(),
            )
        ]
    )
    keyboard.append(_back_row())
    return text, types.InlineKeyboardMarkup(keyboard)


def render_history_page(
    payments: list[Payment],
    page: Page,
    usernames: dict[int, str | None],
    *,
    timezone: str = "UTC",
) -> tuple[str, Any]:
    """Render one history page plus its keyboard (§S2-4.4).

    Rows read ``#12 · 150 ₽ · @user · ✅ 03.10`` — the icon and date come from the
    decision, so a cancelled/expired row is distinguishable at a glance.
    """
    tz = ZoneInfo(timezone)
    lines = [f"📜 <b>История платежей</b> · {len(payments)}"]
    lines.append(f"стр. {page.index + 1}/{page.count}")
    if not payments:
        lines += ["", "История пуста."]
    text = "\n".join(lines)

    keyboard: list[list[Any]] = []
    for payment in page.items:
        icon = STATUS_ICONS.get(str(payment.status), "•")
        stamp = _short_date(payment.decided_at or payment.applied_at, tz)
        caption = (
            f"#{payment.id} · {payment.price} ₽ · "
            f"{_display_name(usernames.get(int(payment.user_tg_id)))} · {icon} {stamp}"
        )
        # History rows are informational — a *decided* payment has nothing to
        # approve, and we have no read-only card — so a row simply re-renders its
        # own page. That keeps every payload parseable (`AdminNav`), unlike an
        # inert ``noop`` ``callback_data`` no handler could ever answer.
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    caption,
                    callback_data=AdminNav(
                        "payments", OP_HISTORY, str(page.index)
                    ).pack(),
                )
            ]
        )
    keyboard.append(
        [
            types.InlineKeyboardButton(caption, callback_data=data)
            for caption, data in nav_row(
                page,
                prev_data=AdminNav("payments", OP_HISTORY, str(page.index - 1)).pack(),
                next_data=AdminNav("payments", OP_HISTORY, str(page.index + 1)).pack(),
            )
        ]
    )
    keyboard.append(_back_row())
    return text, types.InlineKeyboardMarkup(keyboard)


def render_home(pending: int) -> tuple[str, Any]:
    """Render the section home card (§S2-4.2)."""
    text = f"💳 <b>Платежи</b>\n\n⏳ В очереди: {pending}"
    markup = types.InlineKeyboardMarkup(
        [
            [
                types.InlineKeyboardButton(
                    f"⏳ Ожидают ({pending})",
                    callback_data=AdminNav("payments", OP_PENDING, "0").pack(),
                ),
                types.InlineKeyboardButton(
                    "📜 История",
                    callback_data=AdminNav("payments", OP_HISTORY, "0").pack(),
                ),
                types.InlineKeyboardButton(
                    "📈 Статистика",
                    callback_data=AdminNav("payments", OP_STATS).pack(),
                ),
            ],
            [
                types.InlineKeyboardButton(
                    texts.BUTTON_BACK, callback_data=AdminNav("menu").pack()
                )
            ],
        ]
    )
    return text, markup


# --- reads ------------------------------------------------------------------


async def _payments(
    container: Container, statuses: Collection[str | PaymentStatus]
) -> list[Payment]:
    """Read every payment in ``statuses`` (§S2-4.3); ``[]`` when the read fails."""
    if container.sessionmaker is None:
        return []
    try:
        async with container.db() as session:
            return await payments_repo.list_by_statuses(session, statuses)
    except Exception:
        logger.warning("payments list read failed", exc_info=True)
        return []


async def _usernames(container: Container) -> dict[int, str | None]:
    """Return ``{tg_id: username}`` from one users read; ``{}`` on error."""
    if container.sessionmaker is None:
        return {}
    try:
        async with container.db() as session:
            users = await users_repo.list_all(session)
    except Exception:
        logger.warning("payments username read failed", exc_info=True)
        return {}
    return {int(user.tg_id): user.username for user in users}


async def _pending_count(container: Container) -> int:
    """Count every payment awaiting review (§S2-4.2); ``0`` when the read fails."""
    if container.sessionmaker is None:
        return 0
    try:
        async with container.db() as session:
            return await payments_repo.count_by_statuses(session, PENDING_STATUSES)
    except Exception:
        logger.warning("payments pending count failed", exc_info=True)
        return 0


# --- plumbing ---------------------------------------------------------------


def _target(call: Any) -> tuple[int, int]:
    """Return ``(chat_id, message_id)`` of the message the callback came from."""
    message = call.message
    return int(message.chat.id), int(message.message_id)


def _page_arg(payload: Any) -> int:
    """Return the 0-based page of an ``adm:payments:<op>:<n>`` payload (else 0)."""
    arg = getattr(payload, "arg", None)
    if isinstance(arg, str) and arg.isdigit():
        return int(arg)
    return 0


async def _answer(
    bot: Any, call: Any, text: str | None, *, alert: bool = False
) -> None:
    """Answer the callback query without ever raising."""
    try:
        await bot.answer_callback_query(getattr(call, "id", None), text, alert)
    except Exception:  # pragma: no cover - cosmetic
        logger.debug("failed to answer callback", exc_info=True)


async def _open_card(
    call: Any, bot: Any, container: Container, payment_id: int
) -> None:
    """Send the standard review card for ``payment_id`` to the acting admin.

    Delivered through ``Notifier.send_card`` so the copy lands in ``admin_cards``
    and the existing ``pay:ok``/``pay:no`` handlers sync it (and every other
    staff copy) once the decision lands (§S2-4.3).
    """
    payments = container.payments
    notifier = container.notifier
    if payments is None or notifier is None:
        await _answer(bot, call, texts.ERROR_GENERIC)
        return
    text = await payments.render_review_card(payment_id)
    if text is None:
        await _answer(bot, call, texts.PAYMENT_NOT_FOUND, alert=True)
        return
    chat_id, _ = _target(call)
    await notifier.send_card(
        CardKind.PAYMENT,
        int(payment_id),
        [chat_id],
        text,
        reply_markup=payments.review_markup(payment_id),
        parse_mode="HTML",
    )
    await _answer(bot, call, texts.CALLBACK_DONE)


@register_screen("payments")
async def payments_screen(
    call: Any, bot: Any, container: Container, payload: Any = None
) -> None:
    """Render the payments section: home, queue, history or stats (§S2-4.2).

    Re-checks ``PAYMENTS_VIEW`` even though the ``adm:`` dispatcher already gated
    the section: a forged payload must be refused by the screen itself too.
    """
    if not await check_callback(call, Permission.PAYMENTS_VIEW):
        return
    chat_id, message_id = _target(call)
    action = payload.action if isinstance(payload, AdminNav) else None

    if action == OP_PENDING:
        rows = await _payments(container, PENDING_STATUSES)
        text, markup = render_pending_page(
            rows, paginate(rows, _page_arg(payload)), await _usernames(container)
        )
        await edit_or_send(bot, chat_id, message_id, text, markup=markup)
    elif action == OP_HISTORY:
        rows = await _payments(container, HISTORY_STATUSES)
        text, markup = render_history_page(
            rows,
            paginate(rows, _page_arg(payload)),
            await _usernames(container),
            timezone=container.settings.timezone,
        )
        await edit_or_send(bot, chat_id, message_id, text, markup=markup)
    elif action == OP_STATS:
        markup = types.InlineKeyboardMarkup(
            [
                [
                    types.InlineKeyboardButton(
                        "🔄 Обновить",
                        callback_data=AdminNav("payments", OP_STATS).pack(),
                    )
                ],
                _back_row(),
            ]
        )
        await edit_or_send(
            bot,
            chat_id,
            message_id,
            render_stats(await collect_stats(container)),
            markup=markup,
        )
    elif action == OP_CARD:
        arg = getattr(payload, "arg", None)
        if not isinstance(arg, str) or not arg.isdigit():
            await _answer(bot, call, texts.ERROR_STALE_BUTTON)
            return
        await _open_card(call, bot, container, int(arg))
    else:
        text, markup = render_home(await _pending_count(container))
        await edit_or_send(bot, chat_id, message_id, text, markup=markup)


__all__ = [
    "CHART_DAYS",
    "HISTORY_STATUSES",
    "OP_CARD",
    "OP_HISTORY",
    "OP_PENDING",
    "OP_STATS",
    "STATUS_ICONS",
    "Stats",
    "WindowStats",
    "collect_stats",
    "payments_screen",
    "render_history_page",
    "render_home",
    "render_pending_page",
    "render_stats",
]
