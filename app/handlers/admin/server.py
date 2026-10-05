"""Admin «Сервер» screen and the confirmed Xray restart (``TASK_PLAN.md`` §S2-5).

``adm:server`` renders a best-effort snapshot of the panel's ``/server/status``
payload: a bar per CPU/RAM/disk and a summary line that only carries the fields
the panel actually returned — a missing field drops its part instead of printing
a bogus zero. A panel outage renders a warning card: this is the screen an admin
opens *because* something looks wrong, so it must never blank out.

The restart is a *mutation*, so it is owner-only (``SERVER_RESTART``) and
confirm-gated through the central ``cf:`` store (§S1-5.1): ``adm:server:rs``
mints a ``server_restart`` token, and the registered ``cf:`` action re-checks the
permission, restarts, waits for Xray to settle, re-reads the status and audits
the action. It is **never** called automatically.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from telebot.util import quick_markup

from app import texts
from app.callbacks import AdminNav, Confirm
from app.container import Container
from app.handlers.admin.nav import register_screen
from app.handlers.confirm import register_action
from app.permissions import Permission, Role, check_callback, get_role, role_has
from app.ui import bar, edit_or_send

logger = logging.getLogger(__name__)

#: ``adm:server:rs`` — ask for confirmation before restarting Xray.
OP_RESTART = "rs"
#: ``cf:`` action name minted by the restart button (§S2-5.5).
ACTION_RESTART = "server_restart"
#: Audit action recorded for a restart (matches ``Permission.SERVER_RESTART``).
AUDIT_RESTART = "server.restart"
#: How long the panel is given to bring Xray back before the status is re-read.
RESTART_SETTLE_S = 3.0

#: Warning card when the panel cannot be reached at all (§S2-5.4).
PANEL_DOWN = "⚠️ <b>Панель сервера недоступна.</b>\n\nПопробуйте позже."
#: Confirmation card before the restart (inline: a screen is layout, rule 9).
RESTART_ASK = "♻️ <b>Перезапустить Xray?</b>\n\nАктивные соединения кратко прервутся."
#: Toast/summary after a successful restart.
RESTART_DONE = "✅ Xray перезапущен"


def _percent(value: Any) -> float | None:
    """Normalise a raw CPU/RAM/disk value into a ``0..1`` fraction.

    The panel reports CPU as a percentage but RAM/disk as ``{current, total}``
    byte pairs, and the real field names are still unverified
    (``docs/DECISIONS.md`` §S2-5), so both shapes are accepted; anything else
    yields ``None`` and the caller drops the line.
    """
    if isinstance(value, dict):
        current = value.get("current")
        total = value.get("total")
        if current is None or total is None or not total:
            return None
        try:
            return float(current) / float(total)
        except (TypeError, ValueError):
            return None
    try:
        return float(value) / 100.0
    except (TypeError, ValueError):
        return None


def _bar_line(label: str, fraction: float | None) -> str | None:
    """Return ``LABEL  ▰▰▱… 18%`` or ``None`` when the value is absent."""
    if fraction is None:
        return None
    pct = round(min(1.0, max(0.0, float(fraction))) * 100)
    return f"{label.ljust(4)} {bar(fraction)} {pct}%"


def _uptime_label(seconds: Any) -> str | None:
    """Format an uptime in seconds as ``12 д. 4 ч``, else ``None``."""
    try:
        hours_total = int(seconds) // 3600
    except (TypeError, ValueError):
        return None
    if hours_total <= 0:
        return None
    days, hours = divmod(hours_total, 24)
    return f"{days} д. {hours} ч" if days else f"{hours} ч"


def _xray_label(xray: Any) -> str | None:
    """Format the Xray part from a state string or a ``{state, version}`` map."""
    if isinstance(xray, dict):
        state, version = xray.get("state"), xray.get("version")
    else:
        state, version = xray, None
    if not state and not version:
        return None
    icon = "✅" if str(state).lower() in {"running", "true", "1"} else "⚠️"
    return f"Xray: {icon} v{version}" if version else f"Xray: {icon}"


def render_server(status: dict[str, Any]) -> str:
    """Format the server card (§S2-5.3); absent fields drop their line."""
    lines = ["🖥 <b>Сервер</b>", ""]
    for label, key in (("CPU", "cpu"), ("RAM", "mem"), ("Диск", "disk")):
        line = _bar_line(label, _percent(status.get(key)))
        if line is not None:
            lines.append(line)

    summary: list[str] = []
    uptime = _uptime_label(status.get("uptime"))
    if uptime is not None:
        summary.append(f"⏱ Аптайм: {uptime}")
    xray = _xray_label(status.get("xray"))
    if xray is not None:
        summary.append(xray)
    online = status.get("online")
    if isinstance(online, int) and not isinstance(online, bool):
        summary.append(f"онлайн: {online}")
    if summary:
        lines.append(" · ".join(summary))
    return "\n".join(lines)


def server_keyboard(role: Role | None) -> Any:
    """Build the server card buttons; the restart is owner-only (§S2-5.4)."""
    rows: dict[str, dict[str, str]] = {
        "🔄 Обновить": {"callback_data": AdminNav("server").pack()},
    }
    if role_has(role, Permission.SERVER_RESTART):
        rows["♻️ Перезапустить Xray"] = {
            "callback_data": AdminNav("server", OP_RESTART).pack()
        }
    rows[texts.BUTTON_BACK] = {"callback_data": AdminNav("menu").pack()}
    return quick_markup(rows, row_width=2)


async def _server_status(container: Container) -> dict[str, Any] | None:
    """Read the panel status, returning ``None`` on any failure (§S2-5.4)."""
    panel = container.panel
    if panel is None:
        return None
    try:
        return await panel.server_status()
    except Exception:
        logger.info("server status unavailable", exc_info=True)
        return None


@register_screen("server")
async def server_screen(
    call: Any, bot: Any, container: Container, payload: Any = None
) -> None:
    """Render the server card, or mint the restart confirmation (§S2-5.4/.5)."""
    if not await check_callback(call, Permission.SERVER_VIEW):
        return
    if isinstance(payload, AdminNav) and payload.action == OP_RESTART:
        await _request_restart(call, bot, container)
        return

    role = await get_role(int(getattr(call.from_user, "id", 0)))
    chat_id, message_id = _target(call)
    status = await _server_status(container)
    text = PANEL_DOWN if status is None else render_server(status)
    await edit_or_send(bot, chat_id, message_id, text, markup=server_keyboard(role))


async def _request_restart(call: Any, bot: Any, container: Container) -> None:
    """``rs`` — ask before restarting; nothing is written yet (§S2-5.5).

    The ``adm:server`` gate is only ``SERVER_VIEW``, so the stronger
    ``SERVER_RESTART`` is re-checked here before the button is even offered.
    """
    if not await check_callback(call, Permission.SERVER_RESTART):
        return
    store = container.confirmations
    if store is None:  # pragma: no cover - container is wired at startup
        await _answer(bot, call, texts.ERROR_GENERIC)
        return
    token = store.create(int(call.from_user.id), ACTION_RESTART)
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
    await edit_or_send(bot, chat_id, message_id, RESTART_ASK, markup=markup)
    await _answer(bot, call, None)


@register_action(ACTION_RESTART)
async def restart_action(call: Any, bot: Any, container: Container) -> None:
    """Run the confirmed restart, re-read the status and audit (§S2-5.5)."""
    if not await check_callback(call, Permission.SERVER_RESTART):
        return
    panel = container.panel
    if panel is None:  # pragma: no cover - container is wired at startup
        await _answer(bot, call, texts.ERROR_GENERIC)
        return
    actor = int(call.from_user.id)
    chat_id, message_id = _target(call)
    role = await get_role(actor)
    try:
        await panel.restart_xray()
        await asyncio.sleep(RESTART_SETTLE_S)
        status = await panel.server_status()
    except Exception as exc:
        logger.warning("Xray restart failed: %s", exc)
        await edit_or_send(
            bot, chat_id, message_id, texts.ERROR_PANEL, markup=server_keyboard(role)
        )
        await _answer(bot, call, texts.ERROR_PANEL, alert=True)
        return
    await _audit(container, actor)
    await edit_or_send(
        bot,
        chat_id,
        message_id,
        f"{RESTART_DONE}\n\n{render_server(status)}",
        markup=server_keyboard(role),
    )
    await _answer(bot, call, RESTART_DONE)


async def _audit(container: Container, actor: int) -> None:
    """Record ``server.restart`` (best effort, never raises, §S2-5.5)."""
    audit = container.audit
    if audit is None:  # pragma: no cover - container is wired at startup
        return
    await audit.log(actor, AUDIT_RESTART, "server", None)


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


__all__ = [
    "ACTION_RESTART",
    "AUDIT_RESTART",
    "OP_RESTART",
    "PANEL_DOWN",
    "RESTART_ASK",
    "RESTART_DONE",
    "RESTART_SETTLE_S",
    "render_server",
    "restart_action",
    "server_keyboard",
    "server_screen",
]
