"""Payments rewrite tests (``TASK_PLAN.md`` §M0-10.12).

Acceptance criteria covered:

* two admins approving simultaneously → exactly one succeeds, days applied once;
* a restart between submit and approve keeps the card working (state in DB);
* a panel failure during approve keeps ``approved`` with ``applied_at IS NULL``
  (never reverted) so a retry finishes the job without double-granting days;
* a perpetual client (``expiry == 0`` **and** enabled) is enabled but never
  shortened, while a legacy placeholder (``expiry == 0`` with ``enable=False``)
  gets a finite expiry counted from now;
* ``create`` refuses a Telegram user without a ``users`` row instead of raising a
  foreign-key error (the ``pay:sel`` callback answers gracefully);
* a stale/malformed callback answers "Кнопка устарела" without crashing;
* a free-text message from a user with a pending payment gets the
  "заявка на рассмотрении" reply (no FSM read);
* unreviewed payments expire, the user can cancel them, and uploaded media is
  routed to the pending payment (or to support when there is none).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import texts
from app.callbacks import Pay
from app.container import Container
from app.db.base import utcnow
from app.db.models import (
    AuditLog,
    Payment,
    PaymentStatus,
    ReceiptKind,
    Tariff,
    UserStatus,
)
from app.db.repositories import users as users_repo
from app.errors import AlreadyProcessed, NotRegistered, PanelError
from app.handlers.payment import pay_callback, payment_media, pending_notice
from app.services.payments import PaymentService
from app.services.subscriptions import MS_PER_DAY, now_ms
from app.states import UserStates
from tests.fakes import FakeBot, FakePanel

OWNER = 1
ADMIN = 2
USER = 500
CHAT = 99


async def seed_user(
    factory: async_sessionmaker[AsyncSession],
    tg_id: int,
    status: UserStatus = UserStatus.APPROVED,
) -> None:
    """Insert a ``users`` row directly."""
    async with factory() as session:
        await users_repo.upsert_from_telegram(session, tg_id, username="neo")
        await users_repo.set_status(session, tg_id, status)
        await session.commit()


async def seed_tariff(
    factory: async_sessionmaker[AsyncSession],
    *,
    days: int = 30,
    price: int = 150,
) -> Tariff:
    """Insert one active tariff and return it."""
    async with factory() as session:
        tariff = Tariff(
            name="30 дней — 150 ₽", days=days, price=price, is_active=True, sort_order=1
        )
        session.add(tariff)
        await session.commit()
        await session.refresh(tariff)
        return tariff


async def load_payment(
    factory: async_sessionmaker[AsyncSession], payment_id: int
) -> Payment:
    async with factory() as session:
        row = await session.get(Payment, payment_id)
    assert row is not None
    return row


async def audit_actions(
    factory: async_sessionmaker[AsyncSession],
) -> list[tuple[str, str | None]]:
    async with factory() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
    return [(row.action, row.target_id) for row in rows]


def panel_of(container: Container) -> FakePanel:
    panel = container.panel
    assert isinstance(panel, FakePanel)
    return panel


def payments_of(container: Container) -> PaymentService:
    payments = container.payments
    assert isinstance(payments, PaymentService)
    return payments


# --- repository + creation -------------------------------------------------


async def test_create_reuses_the_active_payment(
    handler_container: Container, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """AC: a second ``create`` returns the existing active row (M0-10.2)."""
    await seed_user(session_factory, USER)
    tariff = await seed_tariff(session_factory)

    first = await payments_of(handler_container).create(USER, tariff)
    second = await payments_of(handler_container).create(USER, tariff)

    assert first.id == second.id
    assert first.tariff_name == tariff.name
    assert first.price == tariff.price
    assert first.days == tariff.days


async def test_submit_stores_a_card_copy_per_recipient(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC: ``submit`` fans the card out and records each ``message_id`` (M0-10.3)."""
    from app.db.repositories import admin_cards as admin_cards_repo

    await seed_user(session_factory, USER)
    tariff = await seed_tariff(session_factory)
    panel_of(handler_container).seed(USER, expiry_ms=now_ms() + 5 * MS_PER_DAY)

    payment = await payments_of(handler_container).create(USER, tariff)
    submitted = await payments_of(handler_container).submit(payment.id)

    assert submitted is not None
    assert submitted.status == PaymentStatus.AWAITING_PROOF
    assert [chat for chat, _ in fake_bot.sent] == [OWNER]
    async with session_factory() as session:
        cards = await admin_cards_repo.list_for(session, "payment", payment.id)
    assert [card.chat_id for card in cards] == [OWNER]
    assert all(isinstance(card.message_id, int) for card in cards)


# --- approve ---------------------------------------------------------------


async def test_concurrent_approve_applies_days_exactly_once(
    handler_container: Container, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """AC: two simultaneous approves → one winner, days applied once (M0-10.4)."""
    await seed_user(session_factory, USER)
    tariff = await seed_tariff(session_factory, days=30)
    start = now_ms() + 5 * MS_PER_DAY
    panel_of(handler_container).seed(USER, expiry_ms=start)

    payments = payments_of(handler_container)
    payment = await payments.create(USER, tariff)
    await payments.submit(payment.id)

    results = await asyncio.gather(
        payments.approve(payment.id, ADMIN),
        payments.approve(payment.id, OWNER),
        return_exceptions=True,
    )
    wins = [r for r in results if isinstance(r, Payment)]
    losses = [r for r in results if isinstance(r, AlreadyProcessed)]
    assert len(wins) == 1
    assert len(losses) == 1

    client = await panel_of(handler_container).get_client(USER)
    assert client is not None
    assert int(client.expiry_time) == start + 30 * MS_PER_DAY
    row = await load_payment(session_factory, payment.id)
    assert row.status == PaymentStatus.APPROVED
    assert row.applied_at is not None
    assert row.expiry_before_ms == start
    assert row.expiry_after_ms == start + 30 * MS_PER_DAY


async def test_restart_between_submit_and_approve_keeps_the_card(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC: the card lives in the DB, so a restart does not break approval."""
    await seed_user(session_factory, USER)
    tariff = await seed_tariff(session_factory, days=60)
    start = now_ms() + 5 * MS_PER_DAY
    panel_of(handler_container).seed(USER, expiry_ms=start)

    payments = payments_of(handler_container)
    payment = await payments.create(USER, tariff)
    await payments.submit(payment.id)

    # A fresh service instance simulates a process restart mid-flow.
    restarted = PaymentService(
        session_factory,
        panel=handler_container.panel,
        subscriptions=handler_container.subscriptions,
        notifier=handler_container.notifier,
        audit=handler_container.audit,
    )
    approved = await restarted.approve(payment.id, ADMIN)

    assert approved.status == PaymentStatus.APPROVED
    client = await panel_of(handler_container).get_client(USER)
    assert client is not None and int(client.expiry_time) == start + 60 * MS_PER_DAY


async def test_panel_failure_during_approve_keeps_approved_for_retry(
    handler_container: Container, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """AC: a panel failure keeps ``approved`` + ``applied_at NULL`` and is retryable.

    Reverting to ``submitted`` would re-apply the days on the next approval if
    the panel had actually processed the first attempt (double-grant), so the
    row must stay ``approved`` and surface the retry path instead.
    """
    await seed_user(session_factory, USER)
    tariff = await seed_tariff(session_factory)
    panel = panel_of(handler_container)
    start = now_ms() + 5 * MS_PER_DAY
    panel.seed(USER, expiry_ms=start)

    payments = payments_of(handler_container)
    payment = await payments.create(USER, tariff)
    await payments.submit(payment.id)

    panel.drop_writes = True
    with pytest.raises(PanelError):
        await payments.approve(payment.id, ADMIN)

    row = await load_payment(session_factory, payment.id)
    assert row.status == PaymentStatus.APPROVED
    assert row.applied_at is None
    assert row.decided_by == ADMIN
    # The intended target is recorded before the panel write, so retry is safe.
    assert row.expiry_before_ms == start
    assert row.expiry_after_ms == start + 30 * MS_PER_DAY
    # The reconcile/retry button path sees it as still unapplied.
    assert [item.id for item in await payments.list_unapplied()] == [payment.id]

    panel.drop_writes = False
    assert await payments.apply_approved(payment.id) is True
    client = await panel.get_client(USER)
    assert client is not None and int(client.expiry_time) == start + 30 * MS_PER_DAY


async def test_retry_does_not_stack_days_when_panel_already_updated(
    handler_container: Container, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """AC: a retry after a lost response does not grant the days a second time."""
    await seed_user(session_factory, USER)
    tariff = await seed_tariff(session_factory, days=30)
    panel = panel_of(handler_container)
    start = now_ms() + 5 * MS_PER_DAY
    panel.seed(USER, expiry_ms=start)

    payments = payments_of(handler_container)
    payment = await payments.create(USER, tariff)
    await payments.submit(payment.id)

    # Emulate "panel processed the write but the response was lost": approve
    # records the target, then fails verification.
    panel.drop_writes = True
    with pytest.raises(PanelError):
        await payments.approve(payment.id, ADMIN)
    panel.drop_writes = False

    # The panel *did* reach the target despite the error.
    target = start + 30 * MS_PER_DAY
    await handler_container.subscriptions.set_expiry(USER, target)

    assert await payments.apply_approved(payment.id) is True
    client = await panel.get_client(USER)
    assert client is not None and int(client.expiry_time) == target  # not +30 again


async def test_unlimited_client_is_not_shortened(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC: a perpetual client (expiry ``0`` *and* enabled) is not shortened."""
    await seed_user(session_factory, USER)
    tariff = await seed_tariff(session_factory)
    panel_of(handler_container).seed(USER, expiry_ms=0, enable=True)

    payments = payments_of(handler_container)
    payment = await payments.create(USER, tariff)
    await payments.submit(payment.id)
    row = await payments.approve(payment.id, ADMIN)

    client = await panel_of(handler_container).get_client(USER)
    assert client is not None
    assert int(client.expiry_time) == 0
    assert bool(client.enable) is True
    assert row.expiry_before_ms == 0
    assert row.expiry_after_ms == 0
    assert [text for chat, text in fake_bot.sent if chat == USER]


async def test_legacy_placeholder_client_gets_a_finite_expiry(
    handler_container: Container,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC: ``expiry==0`` + ``enable=False`` is never activated → days from now.

    The legacy import shape (created disabled, ``expiry_time=0``) must not become a
    lifetime subscription just because a payment was approved.
    """
    await seed_user(session_factory, USER)
    tariff = await seed_tariff(session_factory, days=30)
    panel_of(handler_container).seed(USER, expiry_ms=0, enable=False)

    payments = payments_of(handler_container)
    payment = await payments.create(USER, tariff)
    await payments.submit(payment.id)
    before = now_ms()
    row = await payments.approve(payment.id, ADMIN)
    after = now_ms()

    client = await panel_of(handler_container).get_client(USER)
    assert client is not None
    assert bool(client.enable) is True
    expiry = int(client.expiry_time)
    assert expiry != 0  # finite, not unlimited
    assert before + 30 * MS_PER_DAY <= expiry <= after + 30 * MS_PER_DAY
    assert row.expiry_before_ms == 0
    assert row.expiry_after_ms == expiry


async def test_review_card_shows_not_activated_for_a_placeholder(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (S0-1.5): a ``0`` + disabled placeholder is not a date, not «бессрочно»."""
    await seed_user(session_factory, USER)
    tariff = await seed_tariff(session_factory)
    panel_of(handler_container).seed(USER, expiry_ms=0, enable=False)

    payments = payments_of(handler_container)
    payment = await payments.create(USER, tariff)
    await payments.submit(payment.id)

    (card_text,) = fake_bot.texts_to(OWNER)
    assert texts.PAYMENT_EXPIRY_NOT_ACTIVATED in card_text
    assert texts.PAYMENT_EXPIRY_UNLIMITED not in card_text
    assert texts.PAYMENT_CARD_UNLIMITED_WARNING not in card_text


async def test_create_refuses_a_user_without_a_row(
    handler_container: Container, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """AC: ``/pay`` never raises an FK error for an unregistered Telegram user."""
    tariff = await seed_tariff(session_factory)  # no ``users`` row for USER

    with pytest.raises(NotRegistered):
        await payments_of(handler_container).create(USER, tariff)


async def test_admin_callback_selecting_a_tariff_answers_gracefully(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC: a crafted ``pay:sel`` from an unregistered user gets a friendly answer."""
    tariff = await seed_tariff(session_factory)

    await pay_callback(
        make_call("sel", int(tariff.id), user_id=USER), fake_bot, handler_container
    )

    assert fake_bot.callback_answers[-1][1] == texts.PAYMENT_NO_ACCOUNT


async def test_approve_notifies_user_and_writes_audit(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC: the user is notified and ``payment.approve`` is audited (M0-10.4)."""
    await seed_user(session_factory, USER)
    tariff = await seed_tariff(session_factory)
    panel_of(handler_container).seed(USER, expiry_ms=now_ms())

    payments = payments_of(handler_container)
    payment = await payments.create(USER, tariff)
    await payments.submit(payment.id)
    await payments.approve(payment.id, ADMIN)

    user_texts = [text for chat, text in fake_bot.sent if chat == USER]
    expected = texts.PAYMENT_USER_APPROVED.format(name=tariff.name, days=tariff.days)
    assert expected in user_texts
    assert ("payment.approve", str(payment.id)) in await audit_actions(session_factory)


# --- decline / cancel ------------------------------------------------------


async def test_second_decline_is_refused(
    handler_container: Container, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """AC: a decided payment cannot be declined twice (M0-10.5)."""
    await seed_user(session_factory, USER)
    tariff = await seed_tariff(session_factory)
    panel_of(handler_container).seed(USER, expiry_ms=now_ms())

    payments = payments_of(handler_container)
    payment = await payments.create(USER, tariff)
    await payments.submit(payment.id)
    await payments.decline(payment.id, ADMIN)

    with pytest.raises(AlreadyProcessed):
        await payments.decline(payment.id, OWNER)
    row = await load_payment(session_factory, payment.id)
    assert row.status == PaymentStatus.DECLINED
    assert row.decided_by == ADMIN


async def test_user_can_cancel_a_submitted_payment(
    handler_container: Container, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """AC: ``cancel`` moves ``submitted → cancelled`` and is then refused."""
    await seed_user(session_factory, USER)
    tariff = await seed_tariff(session_factory)
    panel_of(handler_container).seed(USER, expiry_ms=now_ms())

    payments = payments_of(handler_container)
    payment = await payments.create(USER, tariff)
    await payments.submit(payment.id)

    cancelled = await payments.cancel(payment.id)
    assert cancelled.status == PaymentStatus.CANCELLED
    with pytest.raises(AlreadyProcessed):
        await payments.cancel(payment.id)


# --- reconcile (§M0-10.10) -------------------------------------------------


async def test_reconcile_lists_and_reapplies_unapplied_payments(
    handler_container: Container, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """AC: an approved-but-unapplied payment is reconciled exactly once."""
    await seed_user(session_factory, USER)
    tariff = await seed_tariff(session_factory, days=30)
    start = now_ms() + 5 * MS_PER_DAY
    panel = panel_of(handler_container)
    panel.seed(USER, expiry_ms=start)

    payments = payments_of(handler_container)
    payment = await payments.create(USER, tariff)
    await payments.submit(payment.id)

    # Fail the apply so the row stays ``approved`` but unapplied.
    panel.drop_writes = True
    with pytest.raises(PanelError):
        await payments.approve(payment.id, ADMIN)
    panel.drop_writes = False

    unapplied = await payments.list_unapplied()
    assert [row.id for row in unapplied] == [payment.id]

    assert await payments.apply_approved(payment.id) is True
    # Idempotent: a second call is a no-op.
    assert await payments.apply_approved(payment.id) is False
    client = await panel.get_client(USER)
    assert client is not None and int(client.expiry_time) == start + 30 * MS_PER_DAY
    assert await payments.list_unapplied() == []


# --- handler: stale callbacks + pending notice -----------------------------


def make_call(
    action: str, ref_id: int, *, user_id: int = USER, data: str | None = None
) -> SimpleNamespace:
    """Build a minimal callback-query stand-in."""
    return SimpleNamespace(
        id="cb-1",
        data=data if data is not None else Pay(action, ref_id).pack(),
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(chat=SimpleNamespace(id=CHAT), message_id=7),
    )


async def test_malformed_callback_answers_stale_button(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """B5: a crafted/stale button answers "Кнопка устарела", never raises."""
    call = make_call("sel", 1, data="pay:sel:not-a-number")
    await pay_callback(call, fake_bot, handler_container)

    assert fake_bot.callback_answers
    assert texts.ERROR_STALE_BUTTON in [
        text for _, text, _ in fake_bot.callback_answers
    ]


async def test_pending_notice_replies_to_free_text(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC: a user with a ``submitted`` payment gets the "on review" reply."""
    await seed_user(session_factory, USER)
    tariff = await seed_tariff(session_factory)
    panel_of(handler_container).seed(USER, expiry_ms=now_ms())

    payments = payments_of(handler_container)
    payment = await payments.create(USER, tariff)
    await payments.submit(payment.id)

    message = SimpleNamespace(
        text="я оплатил",
        from_user=SimpleNamespace(id=USER),
        chat=SimpleNamespace(id=CHAT),
    )
    await pending_notice(message, fake_bot, handler_container)
    assert (CHAT, texts.PAYMENT_ALREADY_SUBMITTED) in fake_bot.sent

    # Commands are ignored so other handlers keep working.
    fake_bot.sent.clear()
    command = SimpleNamespace(
        text="/profile",
        from_user=SimpleNamespace(id=USER),
        chat=SimpleNamespace(id=CHAT),
    )
    await pending_notice(command, fake_bot, handler_container)
    assert fake_bot.sent == []


# --- user cancellation + stale expiry (§M0-10.11) --------------------------


async def test_user_cancels_pending_payment_via_callback(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC: the "Отмена" button lets the user cancel a pending payment."""
    await seed_user(session_factory, USER)
    tariff = await seed_tariff(session_factory)
    panel_of(handler_container).seed(USER, expiry_ms=now_ms())

    payments = payments_of(handler_container)
    payment = await payments.create(USER, tariff)
    await payments.submit(payment.id)

    await pay_callback(make_call("cancel", payment.id), fake_bot, handler_container)

    row = await load_payment(session_factory, payment.id)
    assert row.status == PaymentStatus.CANCELLED
    # ``_edit`` now succeeds against FakeBot's edit slice (added for S1-1), so
    # the notice lands in ``edits`` rather than falling back to ``sent``.
    delivered = [text for cid, _, text, _ in fake_bot.edits if cid == CHAT]
    delivered += [text for cid, text in fake_bot.sent if cid == CHAT]
    assert texts.PAYMENT_CANCELLED_BY_USER in delivered


async def test_expire_stale_closes_unreviewed_pending_payment(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC: a pending payment older than the TTL is expired and the user notified."""
    await seed_user(session_factory, USER)
    tariff = await seed_tariff(session_factory)
    panel_of(handler_container).seed(USER, expiry_ms=now_ms())

    payments = payments_of(handler_container)
    payment = await payments.create(USER, tariff)
    await payments.submit(payment.id)

    # Backdate ``submitted_at`` beyond the default 24h TTL.
    async with session_factory() as session:
        row = await session.get(Payment, payment.id)
        assert row is not None
        row.submitted_at = utcnow() - timedelta(hours=25)
        await session.commit()

    expired = await payments.expire_stale()
    assert [item.id for item in expired] == [payment.id]
    fresh = await load_payment(session_factory, payment.id)
    assert fresh.status == PaymentStatus.EXPIRED
    assert (USER, texts.PAYMENT_USER_EXPIRED) in fake_bot.sent
    # No longer pending → free text is not blocked anymore.
    assert await payments.has_pending(USER) is False


# --- media routing: receipt vs support (§M0-10 media) ---------------------


def make_media(
    *,
    user_id: int = USER,
    kind: str = "photo",
    file_id: str = "file-1",
    caption: str | None = None,
) -> SimpleNamespace:
    """Build a minimal photo/document message stand-in."""
    media = SimpleNamespace(file_id=file_id)
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id, username="neo", first_name="Neo"),
        chat=SimpleNamespace(id=CHAT),
        photo=[media] if kind == "photo" else None,
        document=media if kind == "document" else None,
        caption=caption,
    )


async def test_media_attaches_to_the_pending_payment(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC: a receipt lands on the pending payment, not on support."""
    await seed_user(session_factory, USER)
    tariff = await seed_tariff(session_factory)
    panel_of(handler_container).seed(USER, expiry_ms=now_ms())

    payments = payments_of(handler_container)
    payment = await payments.create(USER, tariff)
    await payments.submit(payment.id)

    await payment_media(
        make_media(kind="document", file_id="doc-9", caption="оплатил"),
        fake_bot,
        handler_container,
    )

    row = await load_payment(session_factory, payment.id)
    assert row.receipt_file_id == "doc-9"
    assert row.receipt_kind == ReceiptKind.DOCUMENT
    assert row.status == PaymentStatus.SUBMITTED
    # Forwarded to the staff who hold the card, never "unrouted".
    assert [chat for chat, _, _, _ in fake_bot.media] == [OWNER]
    assert texts.PAYMENT_MEDIA_UNROUTED not in [text for _, text in fake_bot.sent]


async def test_media_without_pending_payment_is_unrouted(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC: media from a user with no pending payment is not attached to one."""
    await seed_user(session_factory, USER)

    await payment_media(make_media(), fake_bot, handler_container)

    assert (CHAT, texts.PAYMENT_MEDIA_UNROUTED) in fake_bot.sent
    assert fake_bot.media == []


async def test_media_in_support_state_is_routed_to_support(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC: media from a user in the help state goes to support, not "unrouted"."""
    await seed_user(session_factory, USER)
    await fake_bot.set_state(USER, UserStates.waiting_for_help, CHAT)

    await payment_media(make_media(caption="помогите"), fake_bot, handler_container)

    assert texts.PAYMENT_MEDIA_UNROUTED not in [text for _, text in fake_bot.sent]
    assert [chat for chat, _, _, _ in fake_bot.media] == [OWNER]
