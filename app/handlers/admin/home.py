"""``/admin`` dashboard + navigation entry (§S2-1).

The home screen is a *live* dashboard: DB counts, the payment queue and the
server line, each collected in its own short read so one failing source (a panel
outage, a DB blip) degrades that line to a warning instead of blanking the whole
card. Templates and button labels live inline here rather than in ``app.texts``
(``TASK_PLAN.md`` rule 9): a dashboard is layout, not copy.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from telebot.util import quick_markup

from app.callbacks import AdminNav
from app.container import Container
from app.db.base import utcnow
from app.db.models import UserStatus
from app.db.repositories import payments as payments_repo
from app.db.repositories import users as users_repo
from app.db.repositories.payments import PENDING_STATUSES
from app.handlers.admin.nav import SCREENS
from app.permissions import Permission, Role, get_role, require, role_has
from app.ui import edit_or_send

logger = logging.getLogger(__name__)

#: How far back the dashboard's revenue figure looks.
REVENUE_WINDOW = timedelta(days=30)


async def dashboard_counts(container: Container) -> dict[str, int]:
    """Collect the dashboard counters, dropping any read that fails (§S2-1.5)."""
    counts: dict[str, int] = {}
    if container.sessionmaker is None:
        return counts

    try:
        async with container.db() as session:
            by_status = await users_repo.counts_by_status(session)
        counts["users"] = sum(by_status.values())
        counts["approved"] = by_status.get(UserStatus.APPROVED, 0)
        counts["blocked"] = by_status.get(UserStatus.BLOCKED, 0)
        counts["pending"] = by_status.get(UserStatus.PENDING, 0)
    except Exception:
        logger.warning("dashboard user counts failed", exc_info=True)

    try:
        async with container.db() as session:
            counts["queued_payments"] = await payments_repo.count_by_statuses(
                session, PENDING_STATUSES
            )
    except Exception:
        logger.warning("dashboard queued payments failed", exc_info=True)

    try:
        async with container.db() as session:
            _, revenue = await payments_repo.revenue_since(
                session, since=utcnow() - REVENUE_WINDOW
            )
        counts["revenue_30d"] = revenue
    except Exception:
        logger.warning("dashboard revenue failed", exc_info=True)

    return counts


def render_dashboard(
    counts: dict[str, int],
    *,
    server_ok: bool,
    online: int | None,
    maintenance: bool,
) -> str:
    """Format the dashboard text (§S2-1.6); a panel outage is a warning line."""
    server_line = (
        f"✅ Xray работает · онлайн: {online if online is not None else 0}"
        if server_ok
        else "⚠️ панель недоступна"
    )
    maintenance_line = "вкл" if maintenance else "выкл"
    return (
        "🛠 <b>Панель администратора</b>\n\n"
        f"👥 Пользователей: {counts.get('users', 0)}   "
        f"🟢 {counts.get('approved', 0)}   "
        f"🔴 {counts.get('blocked', 0)}   "
        f"⏳ {counts.get('pending', 0)}\n"
        f"💳 В очереди платежей: {counts.get('queued_payments', 0)}   "
        f"💰 30 дн.: {counts.get('revenue_30d', 0)} ₽\n"
        f"🖥 Сервер: {server_line}\n"
        f"🔧 Тех. работы: {maintenance_line}"
    )


def dashboard_keyboard(role: Role | None) -> Any:
    """Build the menu, keeping only the screens the viewer may use (§S2-1.7).

    A section is shown only when both its permission matches *and* its screen is
    registered (:data:`app.handlers.admin.nav.SCREENS`), so the dashboard never
    offers a dead button while later tasks are still landing.
    """
    buttons: dict[str, dict[str, str]] = {}
    if role_has(role, Permission.USERS_VIEW) and "users" in SCREENS:
        buttons["👥 Пользователи"] = {"callback_data": AdminNav("users").pack()}
    if role_has(role, Permission.PAYMENTS_VIEW) and "payments" in SCREENS:
        buttons["💳 Платежи"] = {"callback_data": AdminNav("payments").pack()}
    if role_has(role, Permission.SERVER_VIEW) and "server" in SCREENS:
        buttons["🖥 Сервер"] = {"callback_data": AdminNav("server").pack()}
    if role_has(role, Permission.BROADCAST_SEND) and "broadcast" in SCREENS:
        buttons["📢 Рассылка"] = {"callback_data": AdminNav("broadcast").pack()}
    if role_has(role, Permission.SETTINGS_EDIT) and "settings" in SCREENS:
        buttons["⚙️ Настройки"] = {"callback_data": AdminNav("settings").pack()}
    buttons["🔄 Обновить"] = {"callback_data": AdminNav("menu").pack()}
    return quick_markup(buttons, row_width=2)


async def _server_snapshot(container: Container) -> tuple[bool, int | None]:
    """Return ``(ok, online)`` for the dashboard's server line (best effort)."""
    panel = container.panel
    if panel is None:
        return False, None
    try:
        await panel.list_clients()
        status = await panel.server_status()
    except Exception:
        logger.info("dashboard panel snapshot unavailable", exc_info=True)
        return False, None
    online = status.get("online") if isinstance(status, dict) else None
    return True, int(online) if isinstance(online, int) else None


async def show_dashboard(
    bot: Any,
    chat_id: int,
    container: Container,
    tg_id: int,
    *,
    role: Role | None,
    message_id: int | None = None,
) -> None:
    """Render the dashboard into ``message_id`` (or a fresh message) (§S2-1.8)."""
    counts = await dashboard_counts(container)
    server_ok, online = await _server_snapshot(container)
    maintenance = False
    settings = container.settings_service
    if settings is not None:
        try:
            maintenance = await settings.maintenance_mode()
        except Exception:
            logger.warning("dashboard maintenance read failed", exc_info=True)
    text = render_dashboard(
        counts, server_ok=server_ok, online=online, maintenance=maintenance
    )
    await edit_or_send(bot, chat_id, message_id, text, markup=dashboard_keyboard(role))


@require(Permission.USERS_VIEW)
async def admin_command(message: Any, bot: Any, container: Container) -> None:
    """``/admin`` — open the dashboard, clearing any half-finished FSM prompt.

    ``/admin`` is the universal escape hatch: an admin stuck in the search or
    custom-grant prompt (S2-2/S2-3) can always type it to drop the prompt and
    land back on the dashboard.
    """
    tg_id = int(message.from_user.id)
    chat_id = int(message.chat.id)
    try:
        await bot.delete_state(tg_id, chat_id)
    except Exception:  # pragma: no cover - best-effort FSM reset
        logger.debug("failed to clear admin FSM state", exc_info=True)
    role = await get_role(tg_id)
    await show_dashboard(bot, chat_id, container, tg_id, role=role)


__all__ = [
    "admin_command",
    "dashboard_counts",
    "dashboard_keyboard",
    "render_dashboard",
    "show_dashboard",
]
