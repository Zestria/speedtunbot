"""SQLAlchemy ORM models for the bot database (``TASK_PLAN.md`` §2.4).

Status/role columns are stored as plain ``String`` values; the matching Python
enums (:class:`UserStatus`, :class:`PaymentStatus`, …) are the single source of
truth for the allowed values and are used by the service layer.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONText, utcnow


class UserStatus(StrEnum):
    NEW = "new"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    BLOCKED = "blocked"


class AdminRole(StrEnum):
    ADMIN = "admin"
    SUPPORT = "support"


class InviteKind(StrEnum):
    USER = "user"
    ADMIN = "admin"


class GrantRequestStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class PaymentStatus(StrEnum):
    CREATED = "created"
    #: User pressed "Готово": waiting for review, may still attach a receipt.
    AWAITING_PROOF = "awaiting_proof"
    #: A receipt arrived (or the user skipped it) — fully submitted for review.
    SUBMITTED = "submitted"
    APPROVED = "approved"
    DECLINED = "declined"
    CANCELLED = "cancelled"
    #: Unreviewed for too long — auto-closed so the user is not soft-locked.
    EXPIRED = "expired"
    REVOKED = "revoked"


class ReceiptKind(StrEnum):
    PHOTO = "photo"
    DOCUMENT = "document"


class CardKind(StrEnum):
    PAYMENT = "payment"
    ACCESS = "access"
    ADMIN_GRANT = "admin_grant"


class ReminderKind(StrEnum):
    THREE_DAYS = "3d"
    ONE_DAY = "1d"
    EXPIRED = "expired"


class BroadcastKind(StrEnum):
    TEXT = "text"
    COMPENSATION = "compensation"


class BroadcastAudience(StrEnum):
    ALL = "all"
    ACTIVE = "active"
    EXPIRING7 = "expiring7"
    EXPIRED = "expired"


class BroadcastStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    CANCELLED = "cancelled"
    FAILED = "failed"


class User(Base):
    __tablename__ = "users"

    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(
        String(16), default=UserStatus.NEW, server_default=sa_text("'new'")
    )
    status_note: Mapped[str | None] = mapped_column(String(255))
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime)
    status_changed_by: Mapped[int | None] = mapped_column(BigInteger)
    panel_client_uuid: Mapped[str | None] = mapped_column(String(64))
    invite_id: Mapped[int | None] = mapped_column(ForeignKey("invites.id"))
    bot_blocked: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sa_text("0")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Admin(Base):
    __tablename__ = "admins"

    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    role: Mapped[str] = mapped_column(String(16))
    added_by: Mapped[int] = mapped_column(BigInteger)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
    notify_payments: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=sa_text("1")
    )
    notify_support: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=sa_text("1")
    )
    notify_access: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=sa_text("1")
    )


class Invite(Base):
    __tablename__ = "invites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    role: Mapped[str | None] = mapped_column(String(16))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    label: Mapped[str | None] = mapped_column(String(128))
    max_uses: Mapped[int | None] = mapped_column(Integer)
    uses: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text("0"))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    created_by: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)


class AdminGrantRequest(Base):
    __tablename__ = "admin_grant_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invite_id: Mapped[int] = mapped_column(ForeignKey("invites.id"))
    tg_id: Mapped[int] = mapped_column(BigInteger)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(
        String(16),
        default=GrantRequestStatus.PENDING,
        server_default=sa_text("'pending'"),
    )
    decided_by: Mapped[int | None] = mapped_column(BigInteger)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Tariff(Base):
    __tablename__ = "tariffs"
    __table_args__ = (
        CheckConstraint("days >= 1", name="days_positive"),
        CheckConstraint("price >= 0", name="price_non_negative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    days: Mapped[int] = mapped_column(Integer)
    price: Mapped[int] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=sa_text("1")
    )
    sort_order: Mapped[int] = mapped_column(
        Integer, default=0, server_default=sa_text("0")
    )


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        # At most one *active* payment per user (created/awaiting_proof/submitted).
        Index(
            "uq_payments_active_user",
            "user_tg_id",
            unique=True,
            sqlite_where=sa_text("status IN ('created','awaiting_proof','submitted')"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_tg_id: Mapped[int] = mapped_column(ForeignKey("users.tg_id"))
    tariff_id: Mapped[int | None] = mapped_column(ForeignKey("tariffs.id"))
    # Snapshots taken at creation so later tariff edits never change history.
    tariff_name: Mapped[str] = mapped_column(String(128))
    days: Mapped[int] = mapped_column(Integer)
    price: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(
        String(16), default=PaymentStatus.CREATED, server_default=sa_text("'created'")
    )
    receipt_file_id: Mapped[str | None] = mapped_column(String(255))
    receipt_kind: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    decided_by: Mapped[int | None] = mapped_column(BigInteger)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime)
    expiry_before_ms: Mapped[int | None] = mapped_column(BigInteger)
    expiry_after_ms: Mapped[int | None] = mapped_column(BigInteger)


class AdminCard(Base):
    __tablename__ = "admin_cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    ref_id: Mapped[int] = mapped_column(Integer)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    actor_tg_id: Mapped[int] = mapped_column(BigInteger)
    actor_role: Mapped[str] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[Any] = mapped_column(JSONText, nullable=True)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONText, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_by: Mapped[int | None] = mapped_column(BigInteger)


class ReminderSent(Base):
    __tablename__ = "reminders_sent"
    __table_args__ = (
        UniqueConstraint(
            "user_tg_id", "kind", "expiry_ms", name="uq_reminders_sent_key"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_tg_id: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(8))
    expiry_ms: Mapped[int] = mapped_column(BigInteger)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class BroadcastJob(Base):
    __tablename__ = "broadcast_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_by: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(16))
    text: Mapped[str | None] = mapped_column(Text)
    audience: Mapped[str] = mapped_column(String(16))
    grant_days: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(
        String(16), default=BroadcastStatus.QUEUED, server_default=sa_text("'queued'")
    )
    total: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text("0"))
    sent: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text("0"))
    failed: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text("0"))
    skipped: Mapped[int] = mapped_column(
        Integer, default=0, server_default=sa_text("0")
    )
    # Cursor for resumability: last processed tg_id (ordered ascending).
    cursor_tg_id: Mapped[int] = mapped_column(
        Integer, default=0, server_default=sa_text("0")
    )
    progress_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    progress_message_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
