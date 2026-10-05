"""Invite-link service (``TASK_PLAN.md`` §S3-3.2/§S3-3.3).

A user invite is a deep link ``https://t.me/<bot_username>?start=inv_<token>``.
The raw token is handed to the admin **once** (to be shown + QR-coded) and then
only its **sha256** is persisted — :func:`hash_token` is the single place the
hash is computed, and no method here ever passes the raw token to the logger, so
a leaked log file can never contain a redeemable link (§S3-3.2 AC).

:meth:`InviteService.redeem` is one atomic transaction: the guarded
``UPDATE ... SET uses = uses + 1`` in
:func:`app.db.repositories.invites.increment_use` decides the winner, so two
concurrent redemptions of a single-use invite yield exactly one success and the
loser is told «ссылка недействительна» (§S3-3.3 AC).
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.base import utcnow
from app.db.models import Invite, InviteKind
from app.db.repositories import invites as invites_repo
from app.services.audit import AuditService

logger = logging.getLogger(__name__)

#: ``secrets.token_urlsafe(9)`` → 12 URL-safe chars (~72 bits), enough for a
#: single-use link and short enough to keep the deep link terse (§S3-3.2).
TOKEN_BYTES = 9

ACTION_CREATE = "invite.create"
ACTION_REDEEM = "invite.redeem"
ACTION_REVOKE = "invite.revoke"


def hash_token(raw_token: str) -> str:
    """Return the hex sha256 of ``raw_token`` (the only form ever stored)."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RedeemResult:
    """Outcome of :meth:`InviteService.redeem`.

    ``invite`` is the redeemed row on success (so the caller can branch on its
    ``kind``/``role``) and ``None`` when the token was unknown, revoked, expired
    or already spent — the caller cannot tell which, and deliberately so.
    """

    invite: Invite | None

    @property
    def ok(self) -> bool:
        return self.invite is not None


class InviteService:
    """Mint and redeem invite links over the ``invites`` table."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        *,
        audit: AuditService | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._audit = audit

    async def create(
        self,
        kind: str = InviteKind.USER,
        *,
        max_uses: int | None,
        ttl_days: int,
        actor: int,
        role: str | None = None,
    ) -> tuple[Invite, str]:
        """Persist a new invite and return ``(invite, raw_token)``.

        ``raw_token`` is returned to the caller **once** and never stored; only
        :func:`hash_token` of it reaches the database. ``max_uses=None`` means
        unlimited; ``ttl_days`` sets the expiry.
        """
        sessionmaker = self._require_sessionmaker()
        raw_token = secrets.token_urlsafe(TOKEN_BYTES)
        token_hash = hash_token(raw_token)
        expires_at = utcnow() + timedelta(days=int(ttl_days))
        async with sessionmaker() as session:
            invite = await invites_repo.create(
                session,
                kind=str(kind),
                token_hash=token_hash,
                max_uses=None if max_uses is None else int(max_uses),
                expires_at=expires_at,
                created_by=int(actor),
                role=role,
            )
            await session.commit()
            invite_id = int(invite.id)

        await self._audit_log(
            actor,
            ACTION_CREATE,
            role=role,
            target=invite_id,
            kind=str(kind),
            max_uses=max_uses,
            ttl_days=int(ttl_days),
        )
        return invite, raw_token

    async def redeem(self, raw_token: str) -> RedeemResult:
        """Consume one use of ``raw_token`` in a single transaction (§S3-3.3).

        The guarded increment is the whole race guard: only the caller whose
        ``UPDATE`` matched a row (exists, not revoked, not expired, uses left)
        sees ``rowcount == 1`` and gets a result; everyone else gets
        :attr:`RedeemResult.ok` ``False`` with no side effect.
        """
        if not raw_token:
            return RedeemResult(None)
        sessionmaker = self._require_sessionmaker()
        token_hash = hash_token(raw_token)
        async with sessionmaker() as session:
            won = await invites_repo.increment_use(session, token_hash)
            invite = (
                await invites_repo.get_by_hash(session, token_hash) if won else None
            )
            await session.commit()
            invite_id = None if invite is None else int(invite.id)
            kind = None if invite is None else str(invite.kind)

        if invite is None:
            return RedeemResult(None)
        await self._audit_log(None, ACTION_REDEEM, target=invite_id, kind=kind)
        return RedeemResult(invite)

    async def list_active(self) -> list[Invite]:
        """Return every redeemable invite, newest first (§S3-3.6)."""
        sessionmaker = self._require_sessionmaker()
        async with sessionmaker() as session:
            return await invites_repo.list_active(session)

    async def revoke(self, invite_id: int, *, actor: int | None = None) -> bool:
        """Revoke an invite; ``False`` when it was already revoked or gone."""
        sessionmaker = self._require_sessionmaker()
        async with sessionmaker() as session:
            revoked = await invites_repo.revoke(session, int(invite_id))
            await session.commit()
        if revoked:
            await self._audit_log(actor, ACTION_REVOKE, target=int(invite_id))
        return revoked

    # --- internals ---------------------------------------------------------

    def _require_sessionmaker(self) -> async_sessionmaker[AsyncSession]:
        if self._sessionmaker is None:
            raise RuntimeError("InviteService has no sessionmaker configured")
        return self._sessionmaker

    async def _audit_log(
        self,
        actor: int | None,
        action: str,
        *,
        role: str | None = None,
        target: int | None = None,
        **details: object,
    ) -> None:
        """Append an audit row; the raw token is never among ``details``."""
        if self._audit is None:
            return
        await self._audit.log(actor, action, "invite", target, role=role, **details)


__all__ = [
    "ACTION_CREATE",
    "ACTION_REDEEM",
    "ACTION_REVOKE",
    "TOKEN_BYTES",
    "InviteService",
    "RedeemResult",
    "hash_token",
]
