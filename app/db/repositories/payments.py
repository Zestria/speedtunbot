"""Payment repository (``payments`` table, ``TASK_PLAN.md`` §M0-10.1).

Thin query layer over :class:`~app.db.models.Payment`. The interesting part is
:func:`claim`: an atomic ``UPDATE ... WHERE id=:id AND status=:from`` whose
``rowcount`` tells the caller whether *it* was the one to make the transition.
Two admins pressing "Approve" at the same time therefore produce exactly one
winner (the loser sees ``rowcount == 0`` and is told "уже обработано").
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import Payment, PaymentStatus

#: Statuses that count as "active" (blocked by the partial unique index).
ACTIVE_STATUSES: frozenset[str] = frozenset(
    {PaymentStatus.CREATED, PaymentStatus.AWAITING_PROOF, PaymentStatus.SUBMITTED}
)

#: Statuses that count as "pending review" (an admin may still decide/expire).
PENDING_STATUSES: frozenset[str] = frozenset(
    {PaymentStatus.AWAITING_PROOF, PaymentStatus.SUBMITTED}
)


async def add(session: AsyncSession, payment: Payment) -> Payment:
    """Insert ``payment`` and return it (flushed, id assigned)."""
    session.add(payment)
    await session.flush()
    return payment


async def get(session: AsyncSession, payment_id: int) -> Payment | None:
    """Return the payment with ``payment_id`` or ``None``."""
    return await session.get(Payment, int(payment_id))


async def get_active_for_user(session: AsyncSession, tg_id: int) -> Payment | None:
    """Return the user's active payment (``created``/``submitted``) or ``None``."""
    result = await session.execute(
        select(Payment).where(
            Payment.user_tg_id == int(tg_id),
            Payment.status.in_(ACTIVE_STATUSES),
        )
    )
    return result.scalars().first()


async def get_pending_for_user(session: AsyncSession, tg_id: int) -> Payment | None:
    """Return the user's pending payment (awaiting proof / submitted) or ``None``."""
    result = await session.execute(
        select(Payment)
        .where(
            Payment.user_tg_id == int(tg_id),
            Payment.status.in_(PENDING_STATUSES),
        )
        .order_by(Payment.id.desc())
    )
    return result.scalars().first()


async def get_awaiting_proof_for_user(
    session: AsyncSession, tg_id: int
) -> Payment | None:
    """Return the user's ``awaiting_proof`` payment or ``None`` (media routing)."""
    result = await session.execute(
        select(Payment)
        .where(
            Payment.user_tg_id == int(tg_id),
            Payment.status == PaymentStatus.AWAITING_PROOF,
        )
        .order_by(Payment.id.desc())
    )
    return result.scalars().first()


async def list_stale_pending(
    session: AsyncSession, *, before: datetime
) -> list[Payment]:
    """Return pending payments whose ``submitted_at`` is older than ``before``."""
    result = await session.execute(
        select(Payment).where(
            Payment.status.in_(PENDING_STATUSES),
            Payment.submitted_at.is_not(None),
            Payment.submitted_at < before,
        )
    )
    return list(result.scalars().all())


async def list_by_status(
    session: AsyncSession, status: str | PaymentStatus
) -> list[Payment]:
    """Return every payment in ``status``, newest first."""
    result = await session.execute(
        select(Payment).where(Payment.status == str(status)).order_by(Payment.id.desc())
    )
    return list(result.scalars().all())


async def list_unapplied(session: AsyncSession) -> list[Payment]:
    """Return ``approved`` payments that were never applied to the panel."""
    result = await session.execute(
        select(Payment)
        .where(
            Payment.status == PaymentStatus.APPROVED,
            Payment.applied_at.is_(None),
        )
        .order_by(Payment.id)
    )
    return list(result.scalars().all())


async def claim(
    session: AsyncSession,
    payment_id: int,
    *,
    from_status: str | PaymentStatus | Collection[str | PaymentStatus],
    to_status: str | PaymentStatus,
    actor: int | None = None,
) -> bool:
    """Atomically move ``payment_id`` from ``from_status`` to ``to_status``.

    Returns ``True`` only when the ``UPDATE`` matched exactly one row, i.e. when
    *this* caller performed the transition. ``False`` means somebody else already
    moved it (or the id does not exist) — the caller raises ``AlreadyProcessed``.

    ``from_status`` may be a single status or a collection (e.g.
    :data:`PENDING_STATUSES`) so an admin decision can accept both ``submitted``
    and ``awaiting_proof`` without a read-modify-write race.
    """
    values: dict[str, Any] = {"status": str(to_status)}
    now = utcnow()
    if to_status in (PaymentStatus.APPROVED, PaymentStatus.DECLINED):
        values["decided_at"] = now
        values["decided_by"] = actor
    if to_status == PaymentStatus.EXPIRED:
        values["decided_at"] = now
    if isinstance(from_status, (str, PaymentStatus)):
        from_condition = Payment.status == str(from_status)
    else:
        from_condition = Payment.status.in_([str(s) for s in from_status])
    result = await session.execute(
        update(Payment)
        .where(Payment.id == int(payment_id), from_condition)
        .values(**values)
    )
    await session.flush()
    return int(getattr(result, "rowcount", 0)) == 1


async def mark_awaiting_proof(session: AsyncSession, payment_id: int) -> bool:
    """Move ``created → awaiting_proof`` atomically (records ``submitted_at``)."""
    result = await session.execute(
        update(Payment)
        .where(
            Payment.id == int(payment_id),
            Payment.status == PaymentStatus.CREATED,
        )
        .values(status=PaymentStatus.AWAITING_PROOF, submitted_at=utcnow())
    )
    await session.flush()
    return int(getattr(result, "rowcount", 0)) == 1


async def promote_to_submitted(session: AsyncSession, payment_id: int) -> bool:
    """Move ``awaiting_proof → submitted`` atomically (receipt arrived)."""
    result = await session.execute(
        update(Payment)
        .where(
            Payment.id == int(payment_id),
            Payment.status == PaymentStatus.AWAITING_PROOF,
        )
        .values(status=PaymentStatus.SUBMITTED, submitted_at=utcnow())
    )
    await session.flush()
    return int(getattr(result, "rowcount", 0)) == 1


async def set_receipt(
    session: AsyncSession,
    payment_id: int,
    *,
    file_id: str,
    kind: str,
) -> None:
    """Store the payment proof (photo/document ``file_id``)."""
    await session.execute(
        update(Payment)
        .where(Payment.id == int(payment_id))
        .values(receipt_file_id=str(file_id), receipt_kind=str(kind))
    )
    await session.flush()


async def mark_target(
    session: AsyncSession,
    payment_id: int,
    *,
    expiry_before_ms: int,
    expiry_after_ms: int,
) -> None:
    """Record the intended expiry window *before* the panel write (§M0-10 retry).

    Persisting the target first makes the panel write idempotent: a retry can
    compare the live expiry against ``expiry_after_ms`` and skip a second grant
    when the first attempt actually succeeded but its response was lost.
    """
    await session.execute(
        update(Payment)
        .where(Payment.id == int(payment_id))
        .values(
            expiry_before_ms=int(expiry_before_ms),
            expiry_after_ms=int(expiry_after_ms),
        )
    )
    await session.flush()


async def mark_applied(session: AsyncSession, payment_id: int) -> None:
    """Record that an ``approved`` payment was applied to the panel."""
    await session.execute(
        update(Payment).where(Payment.id == int(payment_id)).values(applied_at=utcnow())
    )
    await session.flush()
