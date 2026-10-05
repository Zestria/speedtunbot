"""DB-backed, atomic payment flow (``TASK_PLAN.md`` §M0-10).

Replaces the legacy FSM flow (``handlers/payment.py``) with rows in the
``payments`` table. The state machine is driven by atomic claims
(:func:`app.db.repositories.payments.claim`), so two admins deciding at the same
instant produce exactly one winner.

A panel failure during ``approve`` does **not** revert the row: it stays
``approved`` with ``applied_at IS NULL`` and the intended expiry is recorded
first, so a retry
(:meth:`PaymentService.apply_approved` / the ``pay:retry:<id>`` button) never
grants the days twice even when the first attempt partially succeeded.

Every send goes through :class:`~app.services.notifier.Notifier` (safe send +
all-copies card sync), so no UI text and no Telegram call lives here.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import texts
from app.db.base import utcnow
from app.db.models import CardKind, Payment, PaymentStatus, ReceiptKind, Tariff
from app.db.repositories import payments as payments_repo
from app.db.repositories import users as users_repo
from app.db.repositories.payments import ACTIVE_STATUSES, PENDING_STATUSES
from app.errors import AlreadyProcessed, ClientNotFound, NotRegistered, PanelError
from app.services.audit import AuditService
from app.services.notifier import Notifier
from app.services.panel import PanelGateway
from app.services.subscriptions import (
    SubscriptionService,
    base_expiry_ms,
    calculate_expiry_ms,
    is_unlimited,
    now_ms,
)
from app.utils.text import esc

logger = logging.getLogger(__name__)

ACTION_APPROVE = "payment.approve"
ACTION_DECLINE = "payment.decline"
ACTION_CANCEL = "payment.cancel"
ACTION_EXPIRE = "payment.expire"

#: How long an unreviewed pending payment may sit before it is auto-closed.
DEFAULT_STALE_HOURS = 24


@dataclass(frozen=True)
class PaymentView:
    """A payment plus the data a review card needs to render it."""

    payment: Payment
    username: str | None
    expiry_ms: int


class PaymentService:
    """Create, submit, decide and cancel payments (§M0-10)."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        *,
        panel: PanelGateway | None = None,
        subscriptions: SubscriptionService | None = None,
        notifier: Notifier | None = None,
        timezone: str = "UTC",
        audit: AuditService | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._panel = panel
        self._subscriptions = subscriptions
        self._notifier = notifier
        self._timezone = timezone
        self._audit = audit

    # --- creation ----------------------------------------------------------

    async def create(self, tg_id: int, tariff: Tariff) -> Payment:
        """Create a ``created`` payment, reusing the user's active one (§M0-10.2).

        The snapshot fields (name/days/price) are copied now, so a later tariff
        edit never rewrites history. The partial unique index allows at most one
        active payment per user, so a second call returns the existing row.

        ``payments.user_tg_id`` is a foreign key into ``users``, so a missing row
        would surface as an opaque ``IntegrityError``: that case raises
        :class:`NotRegistered` instead (the user must run ``/start`` first).
        """
        sessionmaker = self._require_sessionmaker()
        async with sessionmaker() as session:
            if await users_repo.get(session, int(tg_id)) is None:
                raise NotRegistered(texts.PAYMENT_NO_ACCOUNT)
            existing = await payments_repo.get_active_for_user(session, int(tg_id))
            if existing is not None:
                return existing
            payment = Payment(
                user_tg_id=int(tg_id),
                tariff_id=int(tariff.id),
                tariff_name=str(tariff.name),
                days=int(tariff.days),
                price=int(tariff.price),
                status=PaymentStatus.CREATED,
            )
            await payments_repo.add(session, payment)
            await session.commit()
            return payment

    async def submit(self, payment_id: int) -> Payment | None:
        """Move ``created → awaiting_proof`` and fan the review card out (§M0-10.3).

        ``awaiting_proof`` is the pending state where the user may still attach a
        receipt; the admin card is sent immediately so review never waits for it.
        """
        sessionmaker = self._require_sessionmaker()
        async with sessionmaker() as session:
            moved = await payments_repo.mark_awaiting_proof(session, int(payment_id))
            await session.commit()
        if not moved:
            return None
        await self._send_review_card(int(payment_id))
        return await self.get(payment_id)

    # --- decisions ---------------------------------------------------------

    async def approve(self, payment_id: int, actor: int) -> Payment:
        """Approve atomically, apply to the panel, sync cards, notify (§M0-10.4).

        Raises :class:`AlreadyProcessed` when another admin already moved the
        payment, or :class:`PanelError` when the panel write failed. On a panel
        failure the row stays **``approved`` with ``applied_at IS NULL``** (never
        reverted) so a retry finishes the job without re-deciding; the intended
        expiry was recorded first, so the retry cannot grant the days twice.
        """
        await self._claim(
            payment_id,
            from_status=PENDING_STATUSES,
            to_status=PaymentStatus.APPROVED,
            actor=int(actor),
        )
        payment = await self._require(payment_id)

        try:
            await self._apply(payment)
        except PanelError as exc:
            # Keep ``approved``/``applied_at NULL`` and hand the admins a retry
            # button instead of reverting (which could double-grant on retry).
            await self._sync_card(
                payment_id,
                texts.PAYMENT_CARD_APPLY_FAILED.format(actor=self._actor_label(actor)),
                reply_markup=self.retry_markup(payment_id),
            )
            logger.warning("apply failed for payment %s: %s", payment_id, exc)
            raise

        await self._finish_applied(payment, actor)
        return await self._require(payment_id)

    async def decline(self, payment_id: int, actor: int) -> Payment:
        """Decline atomically, notify the user, sync cards, audit (§M0-10.5)."""
        await self._claim(
            payment_id,
            from_status=PENDING_STATUSES,
            to_status=PaymentStatus.DECLINED,
            actor=int(actor),
        )
        payment = await self._require(payment_id)
        await self._sync_card(
            payment_id,
            texts.PAYMENT_CARD_DECLINED.format(actor=self._actor_label(actor)),
        )
        await self._notify_user(int(payment.user_tg_id), texts.PAYMENT_USER_DECLINED)
        await self._audit_log(
            actor, ACTION_DECLINE, payment_id, tg_id=payment.user_tg_id
        )
        return payment

    async def cancel(self, payment_id: int) -> Payment:
        """Cancel a ``created``/``awaiting_proof``/``submitted`` payment (§M0-10.6).

        Anything else means the payment is already decided → ``AlreadyProcessed``.
        Cards are synced only when the card had been fanned out (a bare
        ``created`` payment was never sent).
        """
        payment = await self._require(payment_id)
        if payment.status not in ACTIVE_STATUSES:
            raise AlreadyProcessed(texts.PAYMENT_ALREADY_PROCESSED)
        was_fanned = payment.status in PENDING_STATUSES
        await self._claim(
            payment_id,
            from_status=ACTIVE_STATUSES,
            to_status=PaymentStatus.CANCELLED,
        )
        if was_fanned:
            await self._sync_card(payment_id, texts.PAYMENT_CARD_CANCELLED)
        await self._audit_log(None, ACTION_CANCEL, payment_id, tg_id=payment.user_tg_id)
        return await self._require(payment_id)

    # --- receipts (§M0-10 media routing) -----------------------------------

    async def attach_receipt(
        self, tg_id: int, file_id: str, kind: str | ReceiptKind
    ) -> Payment | None:
        """Attach an uploaded proof to the user's pending payment.

        Returns the updated payment, or ``None`` when the user has no pending
        payment — the caller then routes the media to support instead. A payment
        sitting in ``awaiting_proof`` is promoted to ``submitted`` once the
        receipt lands (``submitted`` = "receipt in hand / ready to review").
        """
        sessionmaker = self._require_sessionmaker()
        async with sessionmaker() as session:
            payment = await payments_repo.get_pending_for_user(session, int(tg_id))
            if payment is None:
                return None
            await payments_repo.set_receipt(
                session, int(payment.id), file_id=str(file_id), kind=str(kind)
            )
            await payments_repo.promote_to_submitted(session, int(payment.id))
            await session.commit()
            payment_id = int(payment.id)
        return await self.get(payment_id)

    async def pending_for_media(self, tg_id: int) -> Payment | None:
        """Return the user's pending payment (``awaiting_proof``/``submitted``)."""
        sessionmaker = self._require_sessionmaker()
        async with sessionmaker() as session:
            return await payments_repo.get_pending_for_user(session, int(tg_id))

    # --- stale pending (§M0-10.14) -----------------------------------------

    async def expire_stale(
        self, *, max_age_hours: int = DEFAULT_STALE_HOURS
    ) -> list[Payment]:
        """Auto-close unreviewed pending payments older than ``max_age_hours``.

        Stops an unresponsive admin from soft-locking a user out of free-text
        interactions forever. Returns the payments that were expired.
        """
        sessionmaker = self._require_sessionmaker()
        cutoff = utcnow() - timedelta(hours=int(max_age_hours))
        async with sessionmaker() as session:
            stale = await payments_repo.list_stale_pending(session, before=cutoff)
        expired: list[Payment] = []
        for payment in stale:
            if not await self._claim_quiet(
                payment.id,
                from_status=PENDING_STATUSES,
                to_status=PaymentStatus.EXPIRED,
            ):
                continue
            await self._sync_card(int(payment.id), texts.PAYMENT_CARD_EXPIRED)
            await self._notify_user(int(payment.user_tg_id), texts.PAYMENT_USER_EXPIRED)
            await self._audit_log(
                None, ACTION_EXPIRE, payment.id, tg_id=payment.user_tg_id
            )
            updated = await self.get(int(payment.id))
            if updated is not None:
                expired.append(updated)
        return expired

    # --- reconcile (§M0-10.10) --------------------------------------------

    async def list_unapplied(self) -> list[Payment]:
        """Return ``approved`` payments whose apply never completed."""
        sessionmaker = self._require_sessionmaker()
        async with sessionmaker() as session:
            return await payments_repo.list_unapplied(session)

    async def apply_approved(self, payment_id: int) -> bool:
        """Idempotently apply an approved-but-unapplied payment.

        Returns ``False`` when there is nothing to do (not ``approved`` or
        already applied), ``True`` after a successful apply. Used by the startup
        reconcile and the ``pay:retry:<id>`` button.
        """
        payment = await self.get(payment_id)
        if (
            payment is None
            or payment.status != PaymentStatus.APPROVED
            or payment.applied_at is not None
        ):
            return False
        await self._apply(payment)
        await self._finish_applied(payment, None)
        return True

    # --- reads -------------------------------------------------------------

    async def get(self, payment_id: int) -> Payment | None:
        """Return the payment or ``None``."""
        sessionmaker = self._require_sessionmaker()
        async with sessionmaker() as session:
            return await payments_repo.get(session, int(payment_id))

    async def get_active_for_user(self, tg_id: int) -> Payment | None:
        """Return the user's active payment (``created``/``submitted``)."""
        sessionmaker = self._require_sessionmaker()
        async with sessionmaker() as session:
            return await payments_repo.get_active_for_user(session, int(tg_id))

    async def has_pending(self, tg_id: int) -> bool:
        """Return ``True`` when the user has a pending payment (§M0-10.9)."""
        sessionmaker = self._require_sessionmaker()
        async with sessionmaker() as session:
            return (
                await payments_repo.get_pending_for_user(session, int(tg_id))
                is not None
            )

    async def card_context(self, payment_id: int) -> PaymentView | None:
        """Return the payment plus the username/expiry a card renders."""
        payment = await self.get(payment_id)
        if payment is None:
            return None
        username = await self._username(int(payment.user_tg_id))
        expiry_ms = await self._current_expiry(int(payment.user_tg_id))
        return PaymentView(payment, username, expiry_ms)

    # --- internals ---------------------------------------------------------

    async def _claim(
        self,
        payment_id: int,
        *,
        from_status: Collection[str],
        to_status: str,
        actor: int | None = None,
    ) -> None:
        """Atomically claim a transition; ``AlreadyProcessed`` when lost."""
        if not await self._claim_quiet(
            payment_id, from_status=from_status, to_status=to_status, actor=actor
        ):
            raise AlreadyProcessed(texts.PAYMENT_ALREADY_PROCESSED)

    async def _claim_quiet(
        self,
        payment_id: int,
        *,
        from_status: Collection[str],
        to_status: str,
        actor: int | None = None,
    ) -> bool:
        """Atomically claim a transition; return whether *this* caller won."""
        sessionmaker = self._require_sessionmaker()
        async with sessionmaker() as session:
            claimed = await payments_repo.claim(
                session,
                int(payment_id),
                from_status=from_status,
                to_status=to_status,
                actor=actor,
            )
            await session.commit()
        return claimed

    async def _require(self, payment_id: int) -> Payment:
        """Return the payment or raise :class:`AlreadyProcessed`."""
        payment = await self.get(payment_id)
        if payment is None:
            raise AlreadyProcessed(texts.PAYMENT_ALREADY_PROCESSED)
        return payment

    async def _apply(self, payment: Payment) -> None:
        """Enable the client and extend it by ``payment.days`` — idempotently.

        The intended expiry (``expiry_after_ms``) is persisted **before** the
        panel write, so a retry after a lost response can compare the live expiry
        against it and skip a second grant. A *perpetual* client
        (``expiry_time == 0`` **and** enabled) is enabled but never shortened
        (§M0-10 AC); a legacy placeholder (``expiry_time == 0`` with
        ``enable=False``) has never been activated and therefore gets a **finite**
        expiry counted from now.
        """
        subscriptions = self._subscriptions
        panel = self._panel
        if subscriptions is None or panel is None:
            raise PanelError("payment service has no panel configured")
        tg_id = int(payment.user_tg_id)
        client = await panel.get_client(tg_id)
        if client is None:
            raise ClientNotFound(str(tg_id))
        current_ms = int(client.expiry_time or 0)
        enabled = bool(client.enable)
        # Read *before* enabling: a perpetual client stays 0/enabled, the legacy
        # placeholder (0/disabled) must count its window from now instead.
        perpetual = is_unlimited(current_ms, enabled)
        # Enable first — idempotent, so a retry may repeat it safely.
        await subscriptions.freeze(tg_id, frozen=False)
        if perpetual:
            # Perpetual: enable only, never shorten. Record the no-op window so
            # the audit trail and a retry both see an explicit ``0``.
            await self._record_target(payment.id, before_ms=0, target_ms=0)
            logger.info(
                "payment %s applied to perpetual client %s (expiry untouched)",
                payment.id,
                tg_id,
            )
            return
        target_ms = int(payment.expiry_after_ms or 0)
        if target_ms and current_ms >= target_ms:
            # A previous attempt already reached the target — do not stack days.
            logger.info(
                "payment %s already applied to panel (expiry %s >= target %s)",
                payment.id,
                current_ms,
                target_ms,
            )
            return
        if not target_ms:
            base_ms = base_expiry_ms(current_ms, enabled, now_ms())
            target_ms = calculate_expiry_ms(base_ms, int(payment.days), now_ms())
            await self._record_target(
                payment.id, before_ms=current_ms, target_ms=target_ms
            )
        # Set the exact target (not ``grant_days``) so a retry cannot overshoot.
        await subscriptions.set_expiry(tg_id, target_ms)

    async def _record_target(
        self, payment_id: int, *, before_ms: int, target_ms: int
    ) -> None:
        sessionmaker = self._require_sessionmaker()
        async with sessionmaker() as session:
            await payments_repo.mark_target(
                session,
                int(payment_id),
                expiry_before_ms=before_ms,
                expiry_after_ms=target_ms,
            )
            await session.commit()

    async def _finish_applied(self, payment: Payment, actor: int | None) -> None:
        """Mark ``applied_at`` and fan the success card/notification out."""
        payment_id = int(payment.id)
        sessionmaker = self._require_sessionmaker()
        async with sessionmaker() as session:
            await payments_repo.mark_applied(session, payment_id)
            await session.commit()
        fresh = await self._require(payment_id)
        await self._sync_card(
            payment_id,
            texts.PAYMENT_CARD_APPROVED.format(
                actor=self._actor_label(actor), days=fresh.days
            ),
        )
        await self._notify_user(
            int(fresh.user_tg_id),
            texts.PAYMENT_USER_APPROVED.format(
                name=esc(fresh.tariff_name), days=fresh.days
            ),
        )
        await self._audit_log(
            actor,
            ACTION_APPROVE,
            payment_id,
            tg_id=fresh.user_tg_id,
            days=fresh.days,
            price=fresh.price,
            expiry_before_ms=fresh.expiry_before_ms,
            expiry_after_ms=fresh.expiry_after_ms,
        )

    async def _send_review_card(self, payment_id: int) -> None:
        """Fan the review card out and remember every delivered copy (§M0-10.3)."""
        notifier = self._notifier
        if notifier is None:
            logger.warning("no notifier; payment %s card not sent", payment_id)
            return
        view = await self.card_context(payment_id)
        if view is None:
            return
        recipients = await notifier.recipients_for_kind(CardKind.PAYMENT)
        if not recipients:
            logger.info("payment %s review card delivered to nobody", payment_id)
            return
        from telebot.util import quick_markup

        markup = quick_markup(
            {
                texts.BUTTON_APPROVE: {"callback_data": f"pay:ok:{payment_id}"},
                texts.BUTTON_DECLINE: {"callback_data": f"pay:no:{payment_id}"},
            },
            row_width=2,
        )
        await notifier.send_card(
            CardKind.PAYMENT,
            int(payment_id),
            recipients,
            self._render_review_card(view),
            reply_markup=markup,
            parse_mode="HTML",
        )

    def _render_review_card(self, view: PaymentView) -> str:
        """Render the §M0-10.3 review card (escaped, unlimited warning)."""
        payment = view.payment
        who = f"@{esc(view.username)}" if view.username else texts.PAYMENT_NO_USERNAME
        body = texts.PAYMENT_CARD.format(
            id=payment.id,
            who=who,
            tg_id=payment.user_tg_id,
            name=esc(payment.tariff_name),
            price=payment.price,
            days=payment.days,
            expiry=self._expiry_label(view.expiry_ms),
        )
        if view.expiry_ms == 0:
            body += "\n\n" + texts.PAYMENT_CARD_UNLIMITED_WARNING
        return body

    async def _sync_card(
        self, payment_id: int, text: str, *, reply_markup: Any | None = None
    ) -> None:
        """Edit every stored copy of the payment card (§M0-10.4/5)."""
        notifier = self._notifier
        if notifier is None:
            return
        await notifier.sync_card(
            CardKind.PAYMENT, int(payment_id), text, reply_markup=reply_markup
        )

    def retry_markup(self, payment_id: int) -> Any:
        """Build the ``pay:retry:<id>`` button shown to owners (§M0-10.10)."""
        from telebot.util import quick_markup

        return quick_markup(
            {
                texts.BUTTON_RETRY_APPLY: {
                    "callback_data": f"pay:retry:{int(payment_id)}"
                }
            },
            row_width=1,
        )

    async def _notify_user(self, tg_id: int, text: str) -> None:
        """Best-effort direct message to the paying user (never raises)."""
        notifier = self._notifier
        if notifier is None:
            return
        await notifier.safe_send(int(tg_id), text, parse_mode="HTML")

    async def _username(self, tg_id: int) -> str | None:
        sessionmaker = self._require_sessionmaker()
        async with sessionmaker() as session:
            user = await users_repo.get(session, int(tg_id))
        return None if user is None else user.username

    async def _current_expiry(self, tg_id: int) -> int:
        """Return the client's *effective* expiry (``0`` = truly unlimited).

        A legacy placeholder (``expiry_time == 0`` with ``enable=False``) has never
        been activated, so it reports the base ``now`` instead of ``0``: the review
        card then shows a finite date and no "бессрочно" warning.
        """
        panel = self._panel
        if panel is None:
            return 0
        try:
            client = await panel.get_client(int(tg_id))
        except PanelError:
            return 0
        if client is None:
            return 0
        expiry_ms = int(client.expiry_time or 0)
        enabled = bool(client.enable)
        if is_unlimited(expiry_ms, enabled):
            return 0
        return base_expiry_ms(expiry_ms, enabled, now_ms())

    async def _audit_log(
        self, actor: int | None, action: str, payment_id: int, **details: Any
    ) -> None:
        audit = self._audit
        if audit is None:
            return
        await audit.log(actor, action, "payment", int(payment_id), **details)

    def _actor_label(self, actor: int | None) -> str:
        return texts.PAYMENT_CARD_SYSTEM if actor is None else f"ID {int(actor)}"

    def _expiry_label(self, expiry_ms: int) -> str:
        if expiry_ms == 0:
            return texts.PAYMENT_EXPIRY_UNLIMITED
        try:
            moment = datetime.fromtimestamp(
                expiry_ms / 1000, tz=ZoneInfo(self._timezone)
            )
        except (OSError, OverflowError, ValueError):  # pragma: no cover - bad clock
            return str(expiry_ms)
        return moment.strftime("%d.%m.%Y %H:%M")

    def _require_sessionmaker(self) -> async_sessionmaker[AsyncSession]:
        if self._sessionmaker is None:
            raise RuntimeError("PaymentService has no sessionmaker configured")
        return self._sessionmaker
