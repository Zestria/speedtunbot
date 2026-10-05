"""Admin «Приглашения» section + the ``inv:`` wizard (``TASK_PLAN.md`` §S3-3).

A two-step, stateless wizard plus a management list:

* ``adm:invites`` — step 1: ``[1 использ.] [5 использ.] [∞]`` (how many times the
  link may be used) and a shortcut to the list;
* ``inv:uses:<n>`` — step 2: ``[1 день] [7 дней] [30 дней]`` (how long it lives);
* ``inv:days:<n>`` — mint the invite and show the link **once** together with its
  QR image (§S1-2's :func:`make_qr_png`, reused verbatim);
* ``inv:list:0`` / ``inv:revoke:<id>`` — the active invites with «🗑 Отозвать».

There is **no FSM and no label prompt**: the two wizard answers ride inside one
int in the callback data (``uses * :data:`DAY_ENCODE` + days``), so nothing is
kept server-side and the raw token never leaves the handler's local scope — it is
sent to the admin once, hashed into the DB by
:meth:`app.services.invites.InviteService.create`, and never logged (§S3-3.5 AC).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from telebot import types

from app import texts
from app.callbacks import AdminNav, Invite, unpack
from app.container import Container
from app.db.models import InviteKind
from app.errors import InvalidCallback
from app.handlers.admin.nav import register_screen
from app.handlers.common import reply
from app.permissions import Permission, check_callback, require
from app.ui import edit_or_send
from app.utils.qr import make_qr_png
from app.utils.text import esc

logger = logging.getLogger(__name__)

#: Callback-data namespace owned by this module (``inv:``).
NAMESPACE = f"{Invite.ns}:"

#: Wizard actions (mirrors :attr:`app.callbacks.Invite.ACTIONS`).
OP_USES = "uses"
OP_DAYS = "days"
OP_REVOKE = "revoke"
OP_LIST = "list"

#: Step-1 options; ``0`` means unlimited (``max_uses`` stored as ``NULL``).
USES_OPTIONS: tuple[int, ...] = (1, 5, 0)
#: Step-2 options, in days.
DAYS_OPTIONS: tuple[int, ...] = (1, 7, 30)
#: ``inv:days:<arg>`` carries ``uses * DAY_ENCODE + days`` in one int, so the
#: wizard needs no server-side state (§S3-3.5).
DAY_ENCODE = 100


def encode_days(uses: int, days: int) -> int:
    """Pack the two wizard answers into one callback int (§S3-3.5)."""
    return int(uses) * DAY_ENCODE + int(days)


def decode_days(arg: int) -> tuple[int, int]:
    """Unpack ``(uses, days)`` from an ``inv:days:<arg>`` callback."""
    return int(arg) // DAY_ENCODE, int(arg) % DAY_ENCODE


def invite_link(bot_username: str | None, token: str) -> str:
    """Build the deep link an invited user opens (§S3-3)."""
    return f"https://t.me/{bot_username or 'bot'}?start=inv_{token}"


# --- rendering --------------------------------------------------------------

_USES_LABELS: dict[int, str] = {
    1: texts.BUTTON_INVITE_USES_1,
    5: texts.BUTTON_INVITE_USES_5,
    0: texts.BUTTON_INVITE_USES_INF,
}
_DAYS_LABELS: dict[int, str] = {
    1: texts.BUTTON_INVITE_DAYS_1,
    7: texts.BUTTON_INVITE_DAYS_7,
    30: texts.BUTTON_INVITE_DAYS_30,
}


def uses_label(uses: int) -> str:
    """Human ``uses`` value for the created card / list (``0`` → ``∞``)."""
    return texts.INVITE_UNLIMITED if int(uses) == 0 else str(int(uses))


def uses_keyboard() -> types.InlineKeyboardMarkup:
    """Step-1 keyboard: the three use-caps plus the list and back (§S3-3.5)."""
    row = [
        types.InlineKeyboardButton(
            _USES_LABELS.get(option, str(option)),
            callback_data=Invite(OP_USES, option).pack(),
        )
        for option in USES_OPTIONS
    ]
    return types.InlineKeyboardMarkup(
        [
            row,
            [
                types.InlineKeyboardButton(
                    texts.BUTTON_INVITE_LIST, callback_data=Invite(OP_LIST, 0).pack()
                )
            ],
            [
                types.InlineKeyboardButton(
                    texts.BUTTON_BACK, callback_data=AdminNav("menu").pack()
                )
            ],
        ]
    )


def days_keyboard(uses: int) -> types.InlineKeyboardMarkup:
    """Step-2 keyboard: the three lifetimes plus a back to step 1 (§S3-3.5)."""
    row = [
        types.InlineKeyboardButton(
            _DAYS_LABELS.get(option, str(option)),
            callback_data=Invite(OP_DAYS, encode_days(uses, option)).pack(),
        )
        for option in DAYS_OPTIONS
    ]
    return types.InlineKeyboardMarkup(
        [
            row,
            [
                types.InlineKeyboardButton(
                    texts.BUTTON_BACK, callback_data=AdminNav("invites").pack()
                )
            ],
        ]
    )


def _expires_label(expires_at: datetime | None) -> str:
    """Return ``дд.мм.гггг`` for an invite expiry (``—`` when unset)."""
    return expires_at.strftime("%d.%m.%Y") if expires_at is not None else "—"


def render_list(
    invites: list[Any], *, now: datetime | None = None
) -> tuple[str, types.InlineKeyboardMarkup]:
    """Render the active-invite list plus its keyboard (§S3-3.6).

    Only redeemable rows are passed in (``InviteService.list_active``), so a
    revoked invite is absent by construction; each row carries its own
    «🗑 Отозвать №<id>» button.
    """
    del now  # reserved for a future "осталось …" column; the list is timeless
    lines = [texts.INVITE_LIST_TITLE]
    if not invites:
        lines += ["", texts.INVITE_LIST_EMPTY]

    keyboard: list[list[Any]] = []
    for invite in invites:
        lines.append(
            texts.INVITE_LIST_ROW.format(
                id=invite.id,
                uses=invite.uses,
                max_uses=(
                    texts.INVITE_UNLIMITED
                    if invite.max_uses is None
                    else invite.max_uses
                ),
                expires=_expires_label(invite.expires_at),
            )
        )
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    f"{texts.BUTTON_INVITE_REVOKE} №{invite.id}",
                    callback_data=Invite(OP_REVOKE, int(invite.id)).pack(),
                )
            ]
        )
    keyboard.append(
        [
            types.InlineKeyboardButton(
                texts.BUTTON_INVITE_CREATE,
                callback_data=AdminNav("invites").pack(),
            )
        ]
    )
    keyboard.append(
        [
            types.InlineKeyboardButton(
                texts.BUTTON_BACK, callback_data=AdminNav("menu").pack()
            )
        ]
    )
    return "\n".join(lines), types.InlineKeyboardMarkup(keyboard)


# --- handlers ---------------------------------------------------------------


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


async def _show_home(bot: Any, call: Any) -> None:
    """Render wizard step 1 into the callback's message (§S3-3.5)."""
    chat_id, message_id = _target(call)
    await edit_or_send(
        bot, chat_id, message_id, texts.INVITE_USES_PROMPT, markup=uses_keyboard()
    )


async def _show_days(bot: Any, call: Any, uses: int) -> None:
    """Render wizard step 2 for the chosen ``uses`` (§S3-3.5)."""
    chat_id, message_id = _target(call)
    await edit_or_send(
        bot,
        chat_id,
        message_id,
        texts.INVITE_DAYS_PROMPT,
        markup=days_keyboard(uses),
    )
    await _answer(bot, call, None)


async def _show_list(bot: Any, call: Any, container: Container) -> None:
    """Render the active-invite list into the callback's message (§S3-3.6)."""
    service = container.invites
    invites: list[Any] = []
    if service is not None:
        try:
            invites = await service.list_active()
        except Exception:
            logger.warning("invite list read failed", exc_info=True)
    text, markup = render_list(invites)
    chat_id, message_id = _target(call)
    await edit_or_send(bot, chat_id, message_id, text, markup=markup)
    await _answer(bot, call, None)


async def _create(bot: Any, call: Any, container: Container, arg: int) -> None:
    """Mint an invite and show its link **once** plus the QR image (§S3-3.5)."""
    service = container.invites
    if service is None:  # pragma: no cover - container is wired at startup
        await _answer(bot, call, texts.ERROR_GENERIC, alert=True)
        return
    uses, days = decode_days(arg)
    actor = int(getattr(call.from_user, "id", 0))
    try:
        _, token = await service.create(
            InviteKind.USER,
            max_uses=None if uses == 0 else uses,
            ttl_days=days,
            actor=actor,
        )
    except Exception:
        logger.warning("failed to create an invite", exc_info=True)
        await _answer(bot, call, texts.ERROR_GENERIC, alert=True)
        return

    # The raw token lives only here: hashed in the DB, shown to the admin once.
    link = invite_link(container.bot_username, token)
    chat_id, message_id = _target(call)
    await reply(
        bot,
        chat_id,
        texts.INVITE_CREATED.format(
            link=esc(link), uses=uses_label(uses), days=f"{days} дн."
        ),
        parse_mode="HTML",
    )
    try:
        await bot.send_photo(
            chat_id,
            make_qr_png(link),
            caption=texts.INVITE_QR_CAPTION,
            parse_mode="HTML",
        )
    except Exception:  # a QR upload failure must not lose the link
        logger.warning("failed to send an invite QR", exc_info=True)
    # Leave the wizard ready for the next invite (no link is re-rendered here).
    await edit_or_send(
        bot, chat_id, message_id, texts.INVITE_USES_PROMPT, markup=uses_keyboard()
    )
    await _answer(bot, call, None)


async def _revoke(bot: Any, call: Any, container: Container, invite_id: int) -> None:
    """Revoke an invite and re-render the list so it disappears (§S3-3.6)."""
    service = container.invites
    if service is None:  # pragma: no cover - container is wired at startup
        await _answer(bot, call, texts.ERROR_GENERIC, alert=True)
        return
    actor = int(getattr(call.from_user, "id", 0))
    try:
        revoked = await service.revoke(int(invite_id), actor=actor)
    except Exception:
        logger.warning("failed to revoke invite %s", invite_id, exc_info=True)
        await _answer(bot, call, texts.ERROR_GENERIC, alert=True)
        return
    await _show_list(bot, call, container)
    toast = texts.INVITE_REVOKED if revoked else texts.INVITE_REVOKE_GONE
    await _answer(bot, call, toast)


async def invites_callback(call: Any, bot: Any, container: Container) -> None:
    """Dispatch an ``inv:`` wizard callback (§S3-3.5/.6).

    Permission first, service second (the same rule as the ``acc:`` review): a
    caller without ``invites.create`` is refused before any invite is minted.
    """
    try:
        payload = unpack(getattr(call, "data", None))
    except InvalidCallback:
        await _answer(bot, call, texts.ERROR_STALE_BUTTON)
        return
    if not isinstance(payload, Invite):
        await _answer(bot, call, texts.ERROR_STALE_BUTTON)
        return
    if not await check_callback(call, Permission.INVITES_CREATE):
        return

    if payload.action == OP_USES:
        await _show_days(bot, call, payload.ref_id)
    elif payload.action == OP_DAYS:
        await _create(bot, call, container, payload.ref_id)
    elif payload.action == OP_LIST:
        await _show_list(bot, call, container)
    elif payload.action == OP_REVOKE:
        await _revoke(bot, call, container, payload.ref_id)
    else:  # pragma: no cover - ``Invite.ACTIONS`` already rejects unknown actions
        await _answer(bot, call, texts.ERROR_STALE_BUTTON)


@register_screen("invites")
async def invites_screen(
    call: Any, bot: Any, container: Container, payload: Any = None
) -> None:
    """``adm:invites`` — open the wizard at step 1 (§S3-3.5).

    The shared ``adm:`` dispatcher already gated the section on
    ``INVITES_CREATE``; the permission is re-checked here too (defence in depth).
    """
    if not await check_callback(call, Permission.INVITES_CREATE):
        return
    await _show_home(bot, call)
    await _answer(bot, call, None)


def register_invites_handler(bot: Any, container: Container) -> None:
    """Register ``/invite`` plus the ``inv:`` wizard namespace (§S3-3.5/.8)."""

    @bot.message_handler(commands=["invite"])
    async def _invite(message: Any) -> None:  # pragma: no cover - thin adapter
        await invite_command(message, bot, container)

    @bot.callback_query_handler(
        func=lambda call: (getattr(call, "data", "") or "").startswith(NAMESPACE)
    )
    async def _callback(call: Any) -> None:  # pragma: no cover - thin adapter
        await invites_callback(call, bot, container)


@require(Permission.INVITES_CREATE)
async def invite_command(message: Any, bot: Any, container: Container) -> None:
    """``/invite`` — open the wizard at step 1 (§S3-3.8)."""
    await reply(
        bot,
        int(message.chat.id),
        texts.INVITE_USES_PROMPT,
        reply_markup=uses_keyboard(),
        parse_mode="HTML",
    )


__all__ = [
    "DAY_ENCODE",
    "DAYS_OPTIONS",
    "NAMESPACE",
    "OP_DAYS",
    "OP_LIST",
    "OP_REVOKE",
    "OP_USES",
    "USES_OPTIONS",
    "days_keyboard",
    "decode_days",
    "encode_days",
    "invite_link",
    "invite_command",
    "invites_callback",
    "invites_screen",
    "register_invites_handler",
    "render_list",
    "uses_keyboard",
    "uses_label",
]
