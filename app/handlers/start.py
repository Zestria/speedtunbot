"""``/start`` + ``/help`` — registration and the main menu (§M0-09.1, §S1-4).

``/start`` is the only place a ``users`` row is created (the context middleware
just refreshes an existing one) and the only place a panel client is created for
a new user. A panel outage must still produce an answer: the handler replies with
a friendly message **and** alerts staff instead of dying silently (B7).

Since S1-4 both commands end with the same inline main menu: ``/start`` keeps its
welcome/returning copy and appends :data:`~app.texts.START_MENU`, ``/help`` shows
the command reference — one :func:`menu_keyboard` for both, so the two can never
drift apart. The menu buttons are plain ``prf:`` sections, so the menu needs no
namespace of its own and cannot shadow the dashboard's callback handler.
"""

from __future__ import annotations

import logging
from typing import Any

from telebot.util import quick_markup

from app import texts
from app.callbacks import AdminNav, ProfileNav
from app.container import Container
from app.db.models import UserStatus
from app.errors import PanelError
from app.handlers.common import alert_staff, reply
from app.permissions import Permission, has

logger = logging.getLogger(__name__)

#: Deep-link payload prefix used by user invites (§S3-3).
INVITE_PREFIX = "inv_"


async def _is_staff(tg_id: int) -> bool:
    """Return ``True`` when ``tg_id`` holds ``users.view`` (§S2-1.11).

    Defensive: an RBAC context that was never configured (some unit tests) must
    not break ``/start`` — the menu simply drops the staff button.
    """
    try:
        return await has(tg_id, Permission.USERS_VIEW)
    except Exception:  # pragma: no cover - RBAC not configured
        logger.debug("staff check failed for %s", tg_id, exc_info=True)
        return False


def menu_keyboard(*, is_staff: bool = False) -> Any:
    """Build the main inline menu (§S1-4.5, §S2-1.11).

    Everyone gets the four ``prf:`` sections; staff additionally get
    ``🛠 Админ-панель`` (``adm:menu``), gated by ``users.view`` so a
    plain user never sees a button they cannot use.
    """
    buttons: dict[str, dict[str, str]] = {
        texts.BUTTON_MENU_PROFILE: {"callback_data": ProfileNav("profile").pack()},
        texts.BUTTON_MENU_PAY: {"callback_data": ProfileNav("pay").pack()},
        texts.BUTTON_MENU_INSTR: {"callback_data": ProfileNav("instr").pack()},
        texts.BUTTON_MENU_SUPPORT: {"callback_data": ProfileNav("support").pack()},
    }
    if is_staff:
        buttons[texts.BUTTON_MENU_ADMIN] = {"callback_data": AdminNav("menu").pack()}
    return quick_markup(buttons, row_width=2)


async def start_command(message: Any, bot: Any, container: Container) -> None:
    """Apply the access policy and answer (§M0-09.1, §S3-1).

    Order matters: an ``inv_…`` deep-link payload is redeemed **before** any
    role/status/``access_mode`` check, so a valid invite can lift a
    ``pending``/``rejected``/unknown user into ``approved`` (§S3-3). Otherwise
    staff and ``approved`` users get the menu, an existing administrative status
    is answered as-is, and a brand-new user follows the configured ``access_mode``.
    """
    from_user = message.from_user
    tg_id = int(from_user.id)
    chat_id = int(message.chat.id)
    username = getattr(from_user, "username", None)
    first_name = getattr(from_user, "first_name", None)

    users = container.users
    if users is None:
        await reply(bot, chat_id, texts.ERROR_GENERIC)
        return

    # 1. Invite redemption wins over every status/access_mode check (§S3-3).
    payload = _start_payload(message)
    if payload.startswith(INVITE_PREFIX):
        await _redeem_invite(payload, message, bot, container)
        return

    # 2. Staff always get in (an approved row + client is ensured).
    is_staff = await _is_staff(tg_id)
    if is_staff:
        await _greet_approved(
            bot, chat_id, container, tg_id, username, first_name, is_staff=True
        )
        return

    status = await users.status(tg_id)
    # 3. An approved user re-opens the menu (self-healing a missing client).
    if status == UserStatus.APPROVED:
        await _greet_approved(
            bot, chat_id, container, tg_id, username, first_name, is_staff=False
        )
        return
    # 4. An existing administrative state is answered, never overwritten.
    if status == UserStatus.PENDING:
        await reply(bot, chat_id, texts.ACCESS_PENDING)
        return
    if status == UserStatus.REJECTED:
        await reply(bot, chat_id, texts.ACCESS_REJECTED)
        return
    if status == UserStatus.BLOCKED:
        return  # a ban is silent; the middleware drops this too

    # 5. A brand-new user follows the configured access mode.
    await _start_new_user(bot, chat_id, container, tg_id, username, first_name)


def _start_payload(message: Any) -> str:
    """Return the deep-link payload after ``/start`` (``""`` when absent)."""
    text = getattr(message, "text", None) or ""
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


async def _greet_approved(
    bot: Any,
    chat_id: int,
    container: Container,
    tg_id: int,
    username: str | None,
    first_name: str | None,
    *,
    is_staff: bool,
) -> None:
    """Approve the user (idempotent), then reply with the main menu (§S3-1)."""
    users = container.users
    assert users is not None
    try:
        result = await users.approve(tg_id, username=username, first_name=first_name)
    except PanelError as exc:
        # B7: reply to the user *and* tell the owners — never a bare traceback.
        logger.warning("panel failure in /start for %s: %s", tg_id, exc)
        await reply(bot, chat_id, texts.ERROR_PANEL)
        await alert_staff(container, "start", exc)
        return

    greeting = texts.START_RETURNING if result.returning else texts.START_WELCOME
    await reply(
        bot,
        chat_id,
        f"{greeting}\n\n{texts.START_MENU}",
        reply_markup=menu_keyboard(is_staff=is_staff),
        parse_mode="HTML",
    )


async def _start_new_user(
    bot: Any,
    chat_id: int,
    container: Container,
    tg_id: int,
    username: str | None,
    first_name: str | None,
) -> None:
    """Apply the configured ``access_mode`` to a brand-new user (§S3-1)."""
    users = container.users
    assert users is not None
    settings = container.settings_service
    mode = "approval"
    if settings is not None:
        try:
            mode = await settings.access_mode()
        except Exception:
            logger.warning("access mode read failed", exc_info=True)

    if mode == "open":
        await _greet_approved(
            bot, chat_id, container, tg_id, username, first_name, is_staff=False
        )
        return
    if mode == "invite_only":
        # No row, no staff notification — just the polite refusal (§S3-1).
        await reply(bot, chat_id, texts.ACCESS_INVITE_ONLY)
        return

    # ``approval`` (default): park the request and notify reviewers (§S3-2).
    try:
        await users.request_access(
            tg_id, username=username, first_name=first_name, status=UserStatus.PENDING
        )
    except Exception as exc:  # a DB hiccup must never kill /start
        logger.warning("request_access failed for %s: %s", tg_id, exc)
        await reply(bot, chat_id, texts.ERROR_GENERIC)
        return
    await _notify_access_request(
        container, tg_id, username=username, first_name=first_name
    )
    await reply(bot, chat_id, texts.ACCESS_PENDING)


async def _notify_access_request(
    container: Container, tg_id: int, *, username: str | None, first_name: str | None
) -> None:
    """Send the access-request card to ``access.review`` staff (§S3-2.4).

    Fanned out by :meth:`Notifier.send_access_card`, which also stores every copy
    in ``admin_cards`` so the later decision can edit them all. Only the *first*
    ``/start`` reaches here: a repeat while the row is already ``pending`` is
    answered by :func:`start_command` without touching this function, so one user
    can never spawn two cards.
    """
    notifier = container.notifier
    if notifier is None:
        return
    try:
        await notifier.send_access_card(tg_id, username=username, first_name=first_name)
    except Exception:  # a card fan-out must never break /start
        logger.warning("failed to send access card for %s", tg_id, exc_info=True)


async def _redeem_invite(
    payload: str, message: Any, bot: Any, container: Container
) -> None:
    """Redeem an ``inv_…`` deep-link token (§S3-3).

    Implemented in S3-3; until then an invite link simply reports a stale link.
    """
    await reply(bot, int(message.chat.id), texts.ERROR_STALE_BUTTON)


async def help_command(message: Any, bot: Any, container: Container) -> None:
    """Show the command reference and the same menu ``/start`` shows (§S1-4.6)."""
    is_staff = await _is_staff(int(message.from_user.id))
    await reply(
        bot,
        int(message.chat.id),
        texts.HELP_TEXT,
        reply_markup=menu_keyboard(is_staff=is_staff),
        parse_mode="HTML",
    )


def register_start_handler(bot: Any, container: Container) -> None:
    """Register ``/start`` on ``bot``."""

    @bot.message_handler(commands=["start"])
    async def _start(message: Any) -> None:  # pragma: no cover - thin adapter
        await start_command(message, bot, container)


def register_help_handler(bot: Any, container: Container) -> None:
    """Register ``/help`` on ``bot``."""

    @bot.message_handler(commands=["help"])
    async def _help(message: Any) -> None:  # pragma: no cover - thin adapter
        await help_command(message, bot, container)


__all__ = [
    "help_command",
    "menu_keyboard",
    "register_help_handler",
    "register_start_handler",
    "start_command",
]
