"""Admin «Настройки» screen: maintenance toggle + bank details (§S2-7).

``adm:settings`` renders the two runtime settings an owner may change:

* ``[🔧 Тех. работы: 🟢 выкл]`` — a one-tap toggle of ``maintenance_mode``; the
  value is written through :class:`~app.services.settings_service.SettingsService`,
  so :class:`~app.middlewares.maintenance.MaintenanceMiddleware` reads the new
  state on the **very next** update (that is the B1 fix, §M0-05.6);
* ``[🏦 Реквизиты]`` — a multi-line FSM edit: the admin types the new details, sees
  a preview and confirms through the central ``cf:`` store (§S1-5.1), so a long
  multi-line value never has to fit in callback data. Re-``set`` refreshes the
  service cache, so ``/pay`` shows the new details immediately (§S2-7.4).

The screen is owner-only (``settings.edit`` via :data:`~app.handlers.admin.nav`
``SECTION_PERMISSIONS``); the toggle additionally re-checks ``maintenance.toggle``
and the bank write is confirm-gated, exactly like the server restart (§S2-5).
Every mutation writes an audit row carrying the **old → new** value. Copy and
layout live inline here (``TASK_PLAN.md`` rule 9: a screen is layout, not copy).
"""

from __future__ import annotations

import logging
from typing import Any

from telebot import types

from app import texts
from app.callbacks import AdminNav, Confirm
from app.container import Container
from app.handlers.admin.nav import register_screen
from app.handlers.common import reply
from app.handlers.confirm import register_action
from app.permissions import Permission, check_callback, get_role, require
from app.states import UserStates
from app.ui import edit_or_send
from app.utils.text import esc

logger = logging.getLogger(__name__)

#: ``adm:settings:mt`` — toggle maintenance mode.
OP_TOGGLE = "mt"
#: ``adm:settings:bank`` — start editing the bank details.
OP_BANK = "bank"
#: ``adm:settings:access`` — cycle the access mode (§S3-1).
OP_ACCESS = "access"

#: ``cf:`` action name minted by the bank-details preview (§S2-7.3).
ACTION_BANK = "settings_bank"

#: Access modes in cycle order (§S3-1); the label is what the button shows.
ACCESS_ORDER: tuple[str, ...] = ("open", "approval", "invite_only")
ACCESS_LABELS: dict[str, str] = {
    "open": "открытый",
    "approval": "по заявке",
    "invite_only": "по приглашению",
}

#: Audit action recorded for a settings write (the same string ``/maintenance``
#: uses, so both entry points land in one place).
AUDIT_SETTING = "setting.set"
#: Audit ``target_id`` for the maintenance toggle.
SETTING_MAINTENANCE = "maintenance_mode"
#: Audit ``target_id`` for the bank details.
SETTING_BANK = "bank_details"
#: Audit ``target_id`` for the access mode (§S3-1).
SETTING_ACCESS = "access_mode"

#: Inline copy (§rule 9).
BUTTON_TOGGLE = "🔧 Тех. работы: {state}"
BUTTON_BANK = "🏦 Реквизиты"
BUTTON_ACCESS = "🔐 Доступ: {mode}"
BUTTON_CANCEL = "✖️ Отмена"
MAINTENANCE_STATE_ON = "🔴 вкл"
MAINTENANCE_STATE_OFF = "🟢 выкл"
ACCESS_TOAST = "🔐 Режим доступа: {mode}"

BANK_NOT_SET = "не настроены"
BANK_PROMPT = (
    "🏦 <b>Реквизиты для оплаты</b>\n\n"
    "Текущее значение:\n{current}\n\n"
    "Отправьте новое значение одним сообщением."
)
BANK_PREVIEW = "🏦 <b>Новые реквизиты</b>\n\n<code>{value}</code>\n\nСохранить?"
BANK_EMPTY = "❌ Пустое значение — реквизиты не изменены."
BANK_SAVED = "✅ Реквизиты обновлены."
MAINTENANCE_TOAST_ON = "🔴 Тех. работы включены"
MAINTENANCE_TOAST_OFF = "🟢 Тех. работы выключены"


def maintenance_state(enabled: bool) -> str:
    """Return the icon+word pair shown for a maintenance flag (§S2-7.1)."""
    return MAINTENANCE_STATE_ON if enabled else MAINTENANCE_STATE_OFF


def bank_line(bank: str | None) -> str:
    """Return the ``🏦 Реквизиты:`` value: escaped details or «не настроены»."""
    if not bank:
        return BANK_NOT_SET
    return f"<code>{esc(bank)}</code>"


def access_label(mode: str) -> str:
    """Human label for an access mode (§S3-1)."""
    return ACCESS_LABELS.get(mode, ACCESS_LABELS["approval"])


def render_settings(*, maintenance: bool, bank: str | None, access: str) -> str:
    """Format the settings card (§S2-7.1, §S3-1)."""
    return (
        "⚙️ <b>Настройки</b>\n\n"
        f"🔧 Тех. работы: {maintenance_state(maintenance)}\n"
        f"🔐 Доступ: {access_label(access)}\n"
        f"🏦 Реквизиты: {bank_line(bank)}"
    )


def settings_keyboard(*, maintenance: bool, access: str) -> types.InlineKeyboardMarkup:
    """Keyboard of the settings card: toggle, access, bank, back (§S2-7.1/S3-1)."""
    return types.InlineKeyboardMarkup(
        [
            [
                types.InlineKeyboardButton(
                    BUTTON_TOGGLE.format(state=maintenance_state(maintenance)),
                    callback_data=AdminNav("settings", OP_TOGGLE).pack(),
                )
            ],
            [
                types.InlineKeyboardButton(
                    BUTTON_ACCESS.format(mode=access_label(access)),
                    callback_data=AdminNav("settings", OP_ACCESS).pack(),
                )
            ],
            [
                types.InlineKeyboardButton(
                    BUTTON_BANK, callback_data=AdminNav("settings", OP_BANK).pack()
                )
            ],
            [
                types.InlineKeyboardButton(
                    texts.BUTTON_BACK, callback_data=AdminNav("menu").pack()
                )
            ],
        ]
    )


async def _read(container: Container) -> tuple[bool, str | None, str]:
    """Return ``(maintenance, bank, access)``; defaults on a read error."""
    settings = container.settings_service
    if settings is None:  # pragma: no cover - container is wired at startup
        return False, None, "approval"
    try:
        return (
            await settings.maintenance_mode(),
            await settings.bank_details(),
            await settings.access_mode(),
        )
    except Exception:
        logger.warning("settings screen read failed", exc_info=True)
        return False, None, "approval"


async def _render(
    bot: Any, container: Container, chat_id: int, message_id: int
) -> None:
    """Render the settings card into ``message_id`` (§S2-7.1)."""
    maintenance, bank, access = await _read(container)
    await edit_or_send(
        bot,
        chat_id,
        message_id,
        render_settings(maintenance=maintenance, bank=bank, access=access),
        markup=settings_keyboard(maintenance=maintenance, access=access),
    )


async def _audit_setting(
    container: Container, actor: int, key: str, old: Any, new: Any
) -> None:
    """Record a settings write with ``old``/``new`` (best effort, §S2-7.2/.4)."""
    audit = container.audit
    if audit is None:  # pragma: no cover - container is wired at startup
        return
    role = await get_role(actor)
    await audit.log(
        actor,
        AUDIT_SETTING,
        "setting",
        key,
        role=None if role is None else str(role),
        old=old,
        new=new,
    )


async def _toggle(call: Any, bot: Any, container: Container) -> None:
    """``adm:settings:mt`` — flip maintenance mode and re-render (§S2-7.2).

    The section gate is ``settings.edit``; the toggle is a mutation, so the
    narrower ``maintenance.toggle`` is re-checked here before anything is written
    (hiding a button is cosmetic, the write must be refused).
    """
    if not await check_callback(call, Permission.MAINTENANCE_TOGGLE):
        return
    settings = container.settings_service
    actor = int(call.from_user.id)
    chat_id, message_id = _target(call)
    if settings is None:  # pragma: no cover - container is wired at startup
        await _answer(bot, call, texts.ERROR_GENERIC, alert=True)
        return
    old = await settings.maintenance_mode()
    new = not old
    await settings.set_maintenance_mode(new, updated_by=actor)
    await _audit_setting(container, actor, SETTING_MAINTENANCE, old, new)
    await _render(bot, container, chat_id, message_id)
    await _answer(bot, call, MAINTENANCE_TOAST_ON if new else MAINTENANCE_TOAST_OFF)


async def _access(call: Any, bot: Any, container: Container) -> None:
    """``adm:settings:access`` — cycle the access mode and re-render (§S3-1).

    ``access_mode`` decides what ``/start`` does for a brand-new user, so this is
    gated on ``settings.edit`` like the rest of the screen. The value cycles
    ``open → approval → invite_only → open``; the old→new pair is audited.
    """
    if not await check_callback(call, Permission.SETTINGS_EDIT):
        return
    settings = container.settings_service
    actor = int(call.from_user.id)
    chat_id, message_id = _target(call)
    if settings is None:  # pragma: no cover - container is wired at startup
        await _answer(bot, call, texts.ERROR_GENERIC, alert=True)
        return
    old = await settings.access_mode()
    index = ACCESS_ORDER.index(old) if old in ACCESS_ORDER else 0
    new = ACCESS_ORDER[(index + 1) % len(ACCESS_ORDER)]
    await settings.set_access_mode(new, updated_by=actor)
    await _audit_setting(container, actor, SETTING_ACCESS, old, new)
    await _render(bot, container, chat_id, message_id)
    await _answer(bot, call, ACCESS_TOAST.format(mode=access_label(new)))


async def _prompt_bank(call: Any, bot: Any, container: Container) -> None:
    """``adm:settings:bank`` — ask for new details and enter the FSM (§S2-7.3).

    «✖️ Отмена» is ``adm:settings`` itself, so cancelling simply re-opens the card
    (which also drops the state) and the stored value stays untouched.
    """
    if not await check_callback(call, Permission.SETTINGS_EDIT):
        return
    actor = int(call.from_user.id)
    chat_id, message_id = _target(call)
    _, bank, _ = await _read(container)
    await _set_state(bot, actor, chat_id)
    markup = types.InlineKeyboardMarkup(
        [
            [
                types.InlineKeyboardButton(
                    BUTTON_CANCEL, callback_data=AdminNav("settings").pack()
                )
            ]
        ]
    )
    await edit_or_send(
        bot,
        chat_id,
        message_id,
        BANK_PROMPT.format(current=bank_line(bank)),
        markup=markup,
    )
    await _answer(bot, call, None)


@require(Permission.SETTINGS_EDIT)
async def admin_bank_message(message: Any, bot: Any, container: Container) -> None:
    """Handle the text typed into the ``admin_bank`` prompt (§S2-7.3).

    Nothing is written yet: the value is parked in a ``cf:`` token so the admin
    confirms it on a preview card. A mistyped/empty reply just clears the state
    without touching the stored details.
    """
    actor = int(message.from_user.id)
    chat_id = int(message.chat.id)
    value = (getattr(message, "text", None) or "").strip()
    await _clear_state(bot, actor, chat_id)
    if not value:
        await reply(bot, chat_id, BANK_EMPTY, parse_mode="HTML")
        return
    store = container.confirmations
    if store is None:  # pragma: no cover - container is wired at startup
        await reply(bot, chat_id, texts.ERROR_GENERIC, parse_mode="HTML")
        return
    token = store.create(actor, ACTION_BANK, value)
    markup = types.InlineKeyboardMarkup(
        [
            [
                types.InlineKeyboardButton(
                    texts.BUTTON_CONFIRM,
                    callback_data=Confirm(token=token).pack(),
                ),
                types.InlineKeyboardButton(
                    texts.BUTTON_CANCEL,
                    callback_data=Confirm(token=token, cancel=True).pack(),
                ),
            ]
        ]
    )
    await reply(
        bot,
        chat_id,
        BANK_PREVIEW.format(value=esc(value)),
        reply_markup=markup,
        parse_mode="HTML",
    )


@register_action(ACTION_BANK)
async def bank_action(call: Any, bot: Any, container: Container, value: str) -> None:
    """Store the confirmed bank details, re-render and audit (§S2-7.4)."""
    if not await check_callback(call, Permission.SETTINGS_EDIT):
        return
    settings = container.settings_service
    if settings is None:  # pragma: no cover - container is wired at startup
        await _answer(bot, call, texts.ERROR_GENERIC, alert=True)
        return
    actor = int(call.from_user.id)
    old = await settings.bank_details()
    # ``set`` refreshes the service cache, so the next ``/pay`` already sees it.
    await settings.set_bank_details(str(value), updated_by=actor)
    await _audit_setting(container, actor, SETTING_BANK, old, str(value))
    chat_id, message_id = _target(call)
    await _render(bot, container, chat_id, message_id)
    await _answer(bot, call, BANK_SAVED)


@register_screen("settings")
async def settings_screen(
    call: Any, bot: Any, container: Container, payload: Any = None
) -> None:
    """``adm:settings`` — home, maintenance toggle or bank prompt (§S2-7.1).

    The ``adm:`` dispatcher already gated the section on ``SETTINGS_EDIT``, but
    every branch re-checks its own permission here too (defence in depth).
    """
    if not await check_callback(call, Permission.SETTINGS_EDIT):
        return
    action = payload.action if isinstance(payload, AdminNav) else None
    if action == OP_TOGGLE:
        await _toggle(call, bot, container)
        return
    if action == OP_ACCESS:
        await _access(call, bot, container)
        return
    if action == OP_BANK:
        await _prompt_bank(call, bot, container)
        return

    actor = int(call.from_user.id)
    chat_id, message_id = _target(call)
    # The home card is also the «✖️ Отмена» target of the bank prompt, so it
    # drops a half-finished FSM edit (a stale state would swallow the next text).
    await _clear_state(bot, actor, chat_id)
    await _render(bot, container, chat_id, message_id)
    await _answer(bot, call, None)


def register_settings_handler(bot: Any, container: Container) -> None:
    """Register the ``admin_bank`` message handler (§S2-7.3).

    The ``adm:`` buttons themselves ride the shared ``adm:`` callback dispatcher;
    this owns only the free-text prompt.
    """

    @bot.message_handler(state=UserStates.admin_bank, content_types=["text"])
    async def _bank(message: Any) -> None:  # pragma: no cover - thin adapter
        await admin_bank_message(message, bot, container)


async def _set_state(bot: Any, tg_id: int, chat_id: int) -> None:
    """Enter the ``admin_bank`` FSM state (best effort)."""
    try:
        await bot.set_state(tg_id, UserStates.admin_bank, chat_id)
    except Exception:  # pragma: no cover - FSM backend unavailable
        logger.debug("failed to set admin bank state", exc_info=True)


async def _clear_state(bot: Any, tg_id: int, chat_id: int) -> None:
    """Leave the ``admin_bank`` FSM state (best effort)."""
    try:
        await bot.delete_state(tg_id, chat_id)
    except Exception:  # pragma: no cover - FSM backend unavailable
        logger.debug("failed to clear admin bank state", exc_info=True)


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
    "ACCESS_LABELS",
    "ACCESS_ORDER",
    "ACTION_BANK",
    "AUDIT_SETTING",
    "BANK_NOT_SET",
    "BANK_PREVIEW",
    "BANK_PROMPT",
    "BANK_SAVED",
    "MAINTENANCE_TOAST_OFF",
    "MAINTENANCE_TOAST_ON",
    "OP_ACCESS",
    "OP_BANK",
    "OP_TOGGLE",
    "SETTING_ACCESS",
    "SETTING_BANK",
    "SETTING_MAINTENANCE",
    "access_label",
    "admin_bank_message",
    "bank_action",
    "bank_line",
    "maintenance_state",
    "register_settings_handler",
    "render_settings",
    "settings_keyboard",
    "settings_screen",
]
