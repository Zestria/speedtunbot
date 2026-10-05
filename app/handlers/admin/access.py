"""Admin «Заявки» section + the ``acc:`` review callbacks (``TASK_PLAN.md`` §S3-2).

Two halves in one module, mirroring the payments section (§S2-4):

* ``adm:access`` — the pending-request queue: a paginated list of ``pending``
  users whose rows each open the **standard** review card (the very renderer the
  ``/start`` fan-out uses, so a queue row and a notification can never disagree);
* ``acc:<accept|reject|block>:<tg_id>`` — the card's buttons. The dispatcher
  re-checks ``access.review`` **before** touching the service (hiding a button is
  cosmetic; a forged callback must be refused by the handler that writes), then
  :meth:`app.services.users.UserService.decide_access` claims the request
  atomically — exactly one reviewer wins, and the loser is told «уже обработано»
  with no side effect at all.

A card copy is stored in ``admin_cards`` by :meth:`Notifier.send_access_card`, so
``sync_card`` edits *every* reviewer's copy once the decision lands. Both
callbacks always answer the query, so a stale or forged button never leaves an
admin without feedback.
"""

from __future__ import annotations

import logging
from typing import Any

from telebot import types

from app import texts
from app.callbacks import Access, AdminNav, ProfileNav, unpack
from app.container import Container
from app.db.models import CardKind, User, UserStatus
from app.db.repositories import users as users_repo
from app.errors import InvalidCallback, PanelError
from app.handlers.admin.nav import register_screen
from app.handlers.common import alert_staff
from app.permissions import Permission, check_callback
from app.services.notifier import actor_label, render_access_card
from app.services.users import DECISION_ACCEPT, DECISION_REJECT
from app.ui import edit_or_send
from app.utils.pagination import Page, nav_row, paginate

logger = logging.getLogger(__name__)

#: Callback-data namespace owned by this module (``acc:``).
NAMESPACE = f"{Access.ns}:"

#: ``adm:access:p:<page>`` — the pending queue.
OP_PENDING = "p"
#: ``adm:access:c:<tg_id>`` — open the review card for one request.
OP_CARD = "c"


# --- queue rendering (§S3-2.7) ----------------------------------------------


def _caption(user: User) -> str:
    """Return one queue-row label: ``⏳ @username · Имя`` (id when no username)."""
    who = f"@{user.username}" if user.username else f"ID {user.tg_id}"
    name = user.first_name or ""
    return f"⏳ {who} · {name}".rstrip(" ·")


def _back_row() -> list[Any]:
    """Return the «⬅️ Назад» row pointing at the section home."""
    return [
        types.InlineKeyboardButton(
            texts.BUTTON_BACK, callback_data=AdminNav("access").pack()
        )
    ]


def render_pending_page(rows: list[User], page: Page) -> tuple[str, Any]:
    """Render one page of pending requests plus its keyboard (§S3-2.7).

    Only ``pending`` rows are ever passed in (``users_repo.list_pending``), so the
    screen can never offer a card for a request somebody already decided.
    """
    lines = [
        f"⏳ <b>Заявки на доступ</b> · {len(rows)}",
        f"стр. {page.index + 1}/{page.count}",
    ]
    if not rows:
        lines += ["", "Нет новых заявок."]
    text = "\n".join(lines)

    keyboard: list[list[Any]] = []
    for user in page.items:
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    _caption(user),
                    callback_data=AdminNav(
                        "access", OP_CARD, str(int(user.tg_id))
                    ).pack(),
                )
            ]
        )
    keyboard.append(
        [
            types.InlineKeyboardButton(caption, callback_data=data)
            for caption, data in nav_row(
                page,
                prev_data=AdminNav("access", OP_PENDING, str(page.index - 1)).pack(),
                next_data=AdminNav("access", OP_PENDING, str(page.index + 1)).pack(),
            )
        ]
    )
    keyboard.append(_back_row())
    return text, types.InlineKeyboardMarkup(keyboard)


async def _pending(container: Container) -> list[User]:
    """Read every ``pending`` request (§S3-2.7); ``[]`` when the read fails."""
    if container.sessionmaker is None:
        return []
    try:
        async with container.db() as session:
            return await users_repo.list_pending(session)
    except Exception:
        logger.warning("access pending read failed", exc_info=True)
        return []


async def _load(container: Container, tg_id: int) -> User | None:
    """Read one ``users`` row (read-only, so the handler needs no service)."""
    if container.sessionmaker is None:
        return None
    async with container.db() as session:
        return await users_repo.get(session, int(tg_id))


async def _open_card(call: Any, bot: Any, container: Container, tg_id: int) -> None:
    """Send the standard review card for ``tg_id`` to the acting admin (§S3-2.7)."""
    notifier = container.notifier
    if notifier is None:
        await _answer(bot, call, texts.ERROR_GENERIC)
        return
    user = await _load(container, tg_id)
    if user is None or user.status != UserStatus.PENDING:
        # Decided (or gone) between the render and the click — the same answer as
        # a lost race, so the admin is never shown a dead card.
        await _answer(bot, call, texts.ACCESS_ALREADY, alert=True)
        return
    chat_id, _ = _target(call)
    await notifier.send_access_card(
        tg_id,
        username=user.username,
        first_name=user.first_name,
        created_at=user.created_at,
        recipients=[chat_id],
    )
    await _answer(bot, call, texts.CALLBACK_DONE)


# --- callbacks --------------------------------------------------------------


async def access_callback(call: Any, bot: Any, container: Container) -> None:
    """Dispatch an ``acc:`` review callback (§S3-2.5).

    Permission first, service second: a caller without ``access.review`` is
    refused by :func:`check_callback` and never reaches ``decide_access``.
    """
    try:
        payload = unpack(getattr(call, "data", None))
    except InvalidCallback:
        await _answer(bot, call, texts.ERROR_STALE_BUTTON)
        return
    if not isinstance(payload, Access):
        await _answer(bot, call, texts.ERROR_STALE_BUTTON)
        return
    if not await check_callback(call, Permission.ACCESS_REVIEW):
        return

    users = container.users
    if users is None:
        await _answer(bot, call, texts.ERROR_GENERIC)
        return

    actor = int(getattr(call.from_user, "id", 0))
    try:
        outcome = await users.decide_access(payload.ref_id, actor, payload.action)
    except PanelError as exc:
        # The row is already ``approved``; the missing client self-heals on the
        # user's next /start, so the admin gets an error toast + a staff alert.
        await _answer(bot, call, texts.ACCESS_PANEL_ERROR, alert=True)
        await alert_staff(container, "access.accept", exc)
        return
    except Exception as exc:
        logger.warning("access decision failed for %s", payload.ref_id, exc_info=True)
        await _answer(bot, call, texts.ERROR_GENERIC, alert=True)
        await alert_staff(container, "access.decide", exc)
        return

    if outcome.already:
        await _answer(bot, call, texts.ACCESS_ALREADY, alert=True)
        return

    decided_by = actor_label(actor, getattr(call.from_user, "username", None))
    await _sync_card(container, payload.action, outcome, decided_by)
    await _notify_user(container, payload.action, payload.ref_id)
    await _answer(bot, call, texts.CALLBACK_DONE)


async def _sync_card(
    container: Container, decision: str, outcome: Any, decided_by: str
) -> None:
    """Edit every stored copy of the card with the decision (§S3-2.5)."""
    notifier = container.notifier
    user = getattr(outcome, "user", None)
    if notifier is None or user is None:
        return
    text = render_access_card(
        int(user.tg_id),
        username=user.username,
        first_name=user.first_name,
        created_at=user.created_at,
        decision=decision,
        decided_by=decided_by,
    )
    await notifier.sync_card(CardKind.ACCESS, int(user.tg_id), text)


def _user_markup() -> Any:
    """Post-approval buttons for the user: profile + pay (§S3-2.4)."""
    return types.InlineKeyboardMarkup(
        [
            [
                types.InlineKeyboardButton(
                    texts.BUTTON_MENU_PROFILE,
                    callback_data=ProfileNav("profile").pack(),
                ),
                types.InlineKeyboardButton(
                    texts.BUTTON_MENU_PAY,
                    callback_data=ProfileNav("pay").pack(),
                ),
            ]
        ]
    )


async def _notify_user(container: Container, decision: str, tg_id: int) -> None:
    """Tell the requesting user the outcome (best effort; never raises)."""
    notifier = container.notifier
    if notifier is None:
        return
    if decision == DECISION_ACCEPT:
        text, markup = texts.ACCESS_USER_ACCEPTED, _user_markup()
    elif decision == DECISION_REJECT:
        text, markup = texts.ACCESS_USER_REJECTED, None
    else:
        text, markup = texts.ACCESS_USER_BLOCKED, None
    await notifier.safe_send(int(tg_id), text, reply_markup=markup, parse_mode="HTML")


# --- screen + registration --------------------------------------------------


def _page_arg(payload: Any) -> int:
    """Return the 0-based page of an ``adm:access:<op>:<n>`` payload (else 0)."""
    arg = getattr(payload, "arg", None)
    if isinstance(arg, str) and arg.isdigit():
        return int(arg)
    return 0


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


@register_screen("access")
async def access_screen(
    call: Any, bot: Any, container: Container, payload: Any = None
) -> None:
    """Render the access section: the pending queue or one request's card.

    Re-checks ``ACCESS_REVIEW`` even though the ``adm:`` dispatcher already gated
    the section: a forged payload must be refused by the screen itself too.
    """
    if not await check_callback(call, Permission.ACCESS_REVIEW):
        return
    chat_id, message_id = _target(call)
    action = payload.action if isinstance(payload, AdminNav) else None

    if action == OP_CARD:
        arg = getattr(payload, "arg", None)
        if not isinstance(arg, str) or not arg.isdigit():
            await _answer(bot, call, texts.ERROR_STALE_BUTTON)
            return
        await _open_card(call, bot, container, int(arg))
        return

    rows = await _pending(container)
    text, markup = render_pending_page(rows, paginate(rows, _page_arg(payload)))
    await edit_or_send(bot, chat_id, message_id, text, markup=markup)


def register_access_handler(bot: Any, container: Container) -> None:
    """Register the ``acc:`` review callbacks on ``bot``.

    Importing this module already registered the ``adm:access`` screen in
    :data:`app.handlers.admin.nav.SCREENS` (§S3-2.6); this adds the module's own
    callback namespace next to the shared ``adm:`` dispatcher.
    """

    @bot.callback_query_handler(
        func=lambda call: (getattr(call, "data", "") or "").startswith(NAMESPACE)
    )
    async def _callback(call: Any) -> None:  # pragma: no cover - thin adapter
        await access_callback(call, bot, container)


__all__ = [
    "NAMESPACE",
    "OP_CARD",
    "OP_PENDING",
    "access_callback",
    "access_screen",
    "register_access_handler",
    "render_pending_page",
]
