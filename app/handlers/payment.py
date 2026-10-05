"""``/pay`` — DB-backed payment flow (``TASK_PLAN.md`` §M0-10.7–9, fixes B5).

Replaces the legacy FSM handler (``handlers/payment.py``): the whole state of a
payment lives in the ``payments`` table, so a restart between submit and approve
cannot lose a card, and two admins pressing Approve at once produce one winner.

All callback data goes through :func:`app.callbacks.unpack`, so a stale or
crafted button answers "Кнопка устарела" instead of crashing the poller (B5).
"""

from __future__ import annotations

import logging
from typing import Any

from telebot.util import quick_markup

from app import texts
from app.callbacks import Pay, unpack
from app.container import Container
from app.db.models import CardKind, ReceiptKind, UserStatus
from app.db.repositories import admin_cards as admin_cards_repo
from app.db.repositories import tariffs as tariffs_repo
from app.errors import AlreadyProcessed, InvalidCallback, PanelError
from app.handlers.common import alert_staff, reply
from app.handlers.support import support_media
from app.permissions import Permission, check_callback
from app.states import UserStates
from app.utils.text import esc

logger = logging.getLogger(__name__)

#: Callback-data namespace owned by this handler.
NAMESPACE = f"{Pay.ns}:"


async def pay_command(message: Any, bot: Any, container: Container) -> None:
    """Show the active tariffs and let the user pick one (§M0-10.7).

    Requires an **approved** user with a panel client: a stranger or a pending
    account is refused gracefully instead of creating a dangling payment.
    """
    tg_id = int(message.from_user.id)
    chat_id = int(message.chat.id)

    users = container.users
    panel = container.panel
    if users is None or panel is None:
        await reply(bot, chat_id, texts.ERROR_GENERIC)
        return

    if await users.status(tg_id) != UserStatus.APPROVED:
        await reply(bot, chat_id, texts.PAYMENT_NOT_APPROVED)
        return

    try:
        client = await panel.get_client(tg_id)
    except PanelError as exc:
        logger.warning("panel failure in /pay for %s: %s", tg_id, exc)
        await reply(bot, chat_id, texts.PAYMENT_UNAVAILABLE)
        await alert_staff(container, "pay", exc)
        return

    if client is None:
        await reply(bot, chat_id, texts.PAYMENT_NO_CLIENT)
        return

    tariffs = await _active_tariffs(container)
    if not tariffs:
        await reply(bot, chat_id, texts.PAYMENT_NO_TARIFFS)
        return

    markup = quick_markup(
        {
            texts.PAYMENT_TARIFF_BUTTON.format(days=t.days, price=t.price): {
                "callback_data": Pay("sel", int(t.id)).pack()
            }
            for t in tariffs
        },
        row_width=1,
    )
    await reply(
        bot,
        chat_id,
        texts.PAYMENT_CHOOSE_TARIFF,
        reply_markup=markup,
        parse_mode="HTML",
    )


async def _active_tariffs(container: Container) -> list[Any]:
    """Return active tariffs ordered by ``sort_order`` (empty when no DB)."""
    if container.sessionmaker is None:
        return []
    async with container.db() as session:
        return await tariffs_repo.list_all(session, active_only=True)


async def pay_callback(call: Any, bot: Any, container: Container) -> None:
    """Dispatch a ``pay:`` callback (user and admin actions, §M0-10.8)."""
    try:
        payload = unpack(getattr(call, "data", None))
    except InvalidCallback:
        # B5: a stale/crafted button never crashes the poller.
        await _answer(bot, call, texts.ERROR_STALE_BUTTON)
        return
    if not isinstance(payload, Pay):
        await _answer(bot, call, texts.ERROR_STALE_BUTTON)
        return

    action = payload.action
    if action == "sel":
        await _select_tariff(call, bot, container, payload.ref_id)
    elif action == "done":
        await _user_done(call, bot, container, payload.ref_id)
    elif action == "cancel":
        await _user_cancel(call, bot, container, payload.ref_id)
    elif action == "ok":
        await _admin_decide(call, bot, container, payload.ref_id, approve=True)
    elif action == "no":
        await _admin_decide(call, bot, container, payload.ref_id, approve=False)
    elif action == "retry":
        await _admin_retry(call, bot, container, payload.ref_id)
    else:  # pragma: no cover - ``unpack`` already filtered the actions
        await _answer(bot, call, texts.ERROR_STALE_BUTTON)


async def _answer(bot: Any, call: Any, text: str, *, alert: bool = False) -> None:
    """Answer a callback query without ever raising."""
    try:
        await bot.answer_callback_query(getattr(call, "id", None), text, alert)
    except Exception:  # pragma: no cover - cosmetic
        logger.debug("failed to answer callback", exc_info=True)


async def _edit(bot: Any, call: Any, text: str, *, markup: Any | None = None) -> bool:
    """Edit the callback's message; fall back to a fresh send when it fails."""
    chat_id = _chat_id(call)
    message_id = _message_id(call)
    try:
        await bot.edit_message_text(
            text, chat_id, message_id, reply_markup=markup, parse_mode="HTML"
        )
        return True
    except Exception:
        return await reply(bot, chat_id, text, reply_markup=markup, parse_mode="HTML")


def _chat_id(call: Any) -> int:
    chat = getattr(getattr(call, "message", None), "chat", None)
    return int(getattr(chat, "id", 0))


def _message_id(call: Any) -> int:
    return int(getattr(getattr(call, "message", None), "message_id", 0))


async def _select_tariff(
    call: Any, bot: Any, container: Container, tariff_id: int
) -> None:
    """``pay:sel:<tariff_id>`` — create/reuse a payment and show instructions."""
    payments = container.payments
    if payments is None or container.sessionmaker is None:
        await _answer(bot, call, texts.ERROR_GENERIC)
        return
    async with container.db() as session:
        tariff = await tariffs_repo.get(session, tariff_id)
    if tariff is None or not tariff.is_active:
        await _answer(bot, call, texts.PAYMENT_UNKNOWN_TARIFF)
        return

    tg_id = int(call.from_user.id)
    payment = await payments.create(tg_id, tariff)
    bank_details = await _bank_details(container)
    if not bank_details:
        await _answer(bot, call, texts.PAYMENT_NO_BANK_DETAILS)
        return

    markup = quick_markup(
        {
            texts.BUTTON_PAY_DONE: {
                "callback_data": Pay("done", int(payment.id)).pack()
            },
            texts.BUTTON_PAY_CANCEL: {
                "callback_data": Pay("cancel", int(payment.id)).pack()
            },
        },
        row_width=2,
    )
    body = texts.PAYMENT_INSTRUCTIONS.format(
        name=esc(payment.tariff_name),
        price=payment.price,
        bank_details=esc(bank_details),
    )
    await _edit(bot, call, body, markup=markup)


async def _bank_details(container: Container) -> str | None:
    settings = container.settings_service
    if settings is None:
        return None
    value = await settings.bank_details()
    return None if value is None else str(value)


async def _user_done(
    call: Any, bot: Any, container: Container, payment_id: int
) -> None:
    """``pay:done:<id>`` — claim ``created → submitted`` and fan the card out."""
    payments = container.payments
    if payments is None:
        await _answer(bot, call, texts.ERROR_GENERIC)
        return
    tg_id = int(call.from_user.id)
    payment = await payments.get(payment_id)
    if payment is None or int(payment.user_tg_id) != tg_id:
        await _answer(bot, call, texts.PAYMENT_NOT_FOUND)
        return
    submitted = await payments.submit(payment_id)
    if submitted is None:
        await _answer(bot, call, texts.PAYMENT_ALREADY_PROCESSED)
        return
    await _answer(bot, call, texts.CALLBACK_DONE)
    # Offer the receipt (optional) *and* a Cancel button so an unreviewed
    # payment never soft-locks the user out of free-text interaction.
    markup = quick_markup(
        {
            texts.BUTTON_PAY_CANCEL: {
                "callback_data": Pay("cancel", int(payment_id)).pack()
            }
        },
        row_width=1,
    )
    await _edit(bot, call, texts.PAYMENT_AWAITING_PROOF, markup=markup)


async def _user_cancel(
    call: Any, bot: Any, container: Container, payment_id: int
) -> None:
    """``pay:cancel:<id>`` — cancel the caller's own active payment."""
    payments = container.payments
    if payments is None:
        await _answer(bot, call, texts.ERROR_GENERIC)
        return
    tg_id = int(call.from_user.id)
    payment = await payments.get(payment_id)
    if payment is None or int(payment.user_tg_id) != tg_id:
        await _answer(bot, call, texts.PAYMENT_NOT_FOUND)
        return
    try:
        await payments.cancel(payment_id)
    except AlreadyProcessed:
        await _answer(bot, call, texts.PAYMENT_ALREADY_PROCESSED)
        return
    await _answer(bot, call, texts.CALLBACK_DONE)
    await _edit(bot, call, texts.PAYMENT_CANCELLED_BY_USER)


async def _admin_decide(
    call: Any,
    bot: Any,
    container: Container,
    payment_id: int,
    *,
    approve: bool,
) -> None:
    """``pay:ok``/``pay:no`` — claim the payment and apply/decline it."""
    if not await check_callback(call, Permission.PAYMENTS_REVIEW):
        return
    payments = container.payments
    if payments is None:
        await _answer(bot, call, texts.ERROR_GENERIC)
        return

    actor = int(call.from_user.id)
    try:
        if approve:
            await payments.approve(payment_id, actor)
        else:
            await payments.decline(payment_id, actor)
    except AlreadyProcessed:
        await _answer(bot, call, texts.PAYMENT_ALREADY_PROCESSED, alert=True)
        return
    except PanelError as exc:
        # The row stays ``approved``/``applied_at NULL`` and the service has
        # re-rendered every card with a "Повторить применение" button.
        await _answer(bot, call, texts.PAYMENT_UNAVAILABLE, alert=True)
        await alert_staff(container, "payment.approve", exc)
        return

    answer = (
        texts.PAYMENT_APPROVED_BY_ADMIN if approve else texts.PAYMENT_DECLINED_BY_ADMIN
    )
    await _answer(bot, call, answer)


async def _admin_retry(
    call: Any, bot: Any, container: Container, payment_id: int
) -> None:
    """``pay:retry:<id>`` — idempotently re-apply an approved payment (§M0-10.10)."""
    if not await check_callback(call, Permission.PAYMENTS_REVIEW):
        return
    payments = container.payments
    if payments is None:
        await _answer(bot, call, texts.ERROR_GENERIC)
        return
    try:
        applied = await payments.apply_approved(payment_id)
    except PanelError as exc:
        await _answer(bot, call, texts.PAYMENT_UNAVAILABLE, alert=True)
        await alert_staff(container, "payment.retry", exc)
        return
    await _answer(
        bot, call, texts.PAYMENT_RETRY_OK if applied else texts.PAYMENT_RETRY_NOOP
    )


async def pending_notice(message: Any, bot: Any, container: Container) -> None:
    """Reply "заявка на рассмотрении" to a user with a ``submitted`` payment.

    Replaces the legacy FSM-state handler (§M0-10.9): the check is a DB read, no
    FSM state is consulted. Commands are ignored so ``/profile`` & co. still work.
    """
    text = getattr(message, "text", None) or ""
    if text.startswith("/"):
        return
    payments = container.payments
    if payments is None:
        return
    tg_id = int(message.from_user.id)
    if await payments.has_pending(tg_id):
        await reply(bot, int(message.chat.id), texts.PAYMENT_ALREADY_SUBMITTED)


def _is_free_text(message: Any) -> bool:
    """Only non-command text messages reach :func:`pending_notice`."""
    return not (getattr(message, "text", None) or "").startswith("/")


def _media_of(message: Any) -> tuple[str | None, str]:
    """Return ``(file_id, kind)`` for a photo/document message (§M0-10 media)."""
    photos = getattr(message, "photo", None)
    if photos:
        return str(photos[-1].file_id), str(ReceiptKind.PHOTO)
    document = getattr(message, "document", None)
    if document is not None:
        return str(document.file_id), str(ReceiptKind.DOCUMENT)
    return None, ""


async def payment_media(message: Any, bot: Any, container: Container) -> None:
    """Route an uploaded photo/document unambiguously (§M0-10 media routing).

    The decision is an explicit DB read: a user with a pending payment
    (``awaiting_proof``/``submitted``) has the file attached to that payment and
    forwarded under every staff card; anyone else who is in the support state
    has it routed to support, and everyone else gets a short hint.
    """
    tg_id = int(message.from_user.id)
    chat_id = int(message.chat.id)
    file_id, kind = _media_of(message)
    if file_id is None:
        return

    payments = container.payments
    payment = None
    if payments is not None:
        try:
            payment = await payments.attach_receipt(tg_id, file_id, kind)
        except Exception:  # pragma: no cover - never break the poller
            logger.warning("failed to attach receipt for %s", tg_id, exc_info=True)
            payment = None

    if payment is not None:
        await _notify_receipt(bot, container, payment, file_id, kind)
        return

    if await bot.get_state(tg_id, chat_id) == UserStates.waiting_for_help.name:
        await support_media(message, bot, container)
        return
    await reply(bot, chat_id, texts.PAYMENT_MEDIA_UNROUTED)


async def _notify_receipt(
    bot: Any, container: Container, payment: Any, file_id: str, kind: str
) -> None:
    """Reply under every staff card with the proof, best effort."""
    if container.sessionmaker is None:
        return
    async with container.db() as session:
        cards = await admin_cards_repo.list_for(session, CardKind.PAYMENT, payment.id)
    caption = texts.PAYMENT_RECEIPT_CARD.format(id=payment.id)
    for card in cards:
        try:
            if kind == ReceiptKind.DOCUMENT:
                await bot.send_document(
                    card.chat_id,
                    file_id,
                    caption=caption,
                    reply_to_message_id=card.message_id,
                )
            else:
                await bot.send_photo(
                    card.chat_id,
                    file_id,
                    caption=caption,
                    reply_to_message_id=card.message_id,
                )
        except Exception:  # pragma: no cover - a dead card must not stop others
            logger.debug("receipt forward to %s failed", card.chat_id, exc_info=True)
    await reply(
        bot,
        int(payment.user_tg_id),
        texts.PAYMENT_RECEIPT_SAVED.format(id=payment.id),
    )


async def reconcile_payments(container: Container) -> int:
    """Alert owners about ``approved`` payments that were never applied (§M0-10.10).

    Each alert carries a ``pay:retry:<id>`` button whose handler is idempotent,
    so a payment is applied exactly once even if an admin presses it repeatedly.
    Returns how many alerts were sent.
    """
    payments = container.payments
    notifier = container.notifier
    if payments is None or notifier is None:
        return 0
    # Auto-close unreviewed payments first so a delayed admin cannot soft-lock
    # a user out of free-text interaction forever.
    try:
        await payments.expire_stale()
    except Exception:  # pragma: no cover - reconcile must never crash startup
        logger.warning("stale payment sweep failed", exc_info=True)
    pending = await payments.list_unapplied()
    sent = 0
    for payment in pending:
        markup = payments.retry_markup(int(payment.id))
        text = texts.PAYMENT_RETRY_ALERT.format(
            id=payment.id, tg_id=payment.user_tg_id, name=esc(payment.tariff_name)
        )
        delivered = await notifier.alert_staff(
            text, kind="alert", reply_markup=markup, parse_mode="HTML"
        )
        if delivered:
            sent += 1
    return sent


def register_payment_handler(bot: Any, container: Container) -> None:
    """Register ``/pay``, the ``pay:`` callbacks and the pending notice."""

    @bot.message_handler(commands=["pay"])
    async def _pay(message: Any) -> None:  # pragma: no cover - thin adapter
        await pay_command(message, bot, container)

    @bot.message_handler(content_types=["text"], func=_is_free_text)
    async def _pending(message: Any) -> None:  # pragma: no cover - thin adapter
        await pending_notice(message, bot, container)

    @bot.message_handler(content_types=["photo", "document"])
    async def _media(message: Any) -> None:  # pragma: no cover - thin adapter
        await payment_media(message, bot, container)

    @bot.callback_query_handler(
        func=lambda call: (getattr(call, "data", "") or "").startswith(NAMESPACE)
    )
    async def _callback(call: Any) -> None:  # pragma: no cover - thin adapter
        await pay_callback(call, bot, container)
