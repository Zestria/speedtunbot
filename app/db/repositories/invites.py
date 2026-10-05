"""Invite repository (``invites`` table, ``TASK_PLAN.md`` §S3-3.1).

Thin query layer over :class:`~app.db.models.Invite`. Only the **sha256** of a
token is ever stored (:func:`app.services.invites.InviteService.create` hashes
it), so every lookup here is by ``token_hash`` — the raw token never reaches the
database (or a log line).

:func:`increment_use` is the race guard for redemption: a single guarded
``UPDATE ... SET uses = uses + 1 WHERE token_hash=:hash AND revoked_at IS NULL
AND (expires_at IS NULL OR expires_at > :now) AND (max_uses IS NULL OR
uses < max_uses)`` whose ``rowcount`` tells the caller whether *it* consumed the
last remaining use. Two users opening a single-use link at the same moment
therefore produce exactly one winner, mirroring :func:`payments_repo.claim`
(§M0-10) and :func:`users_repo.claim_pending` (§S3-2.2).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import Invite


async def create(
    session: AsyncSession,
    *,
    kind: str,
    token_hash: str,
    expires_at: datetime,
    created_by: int,
    max_uses: int | None = None,
    role: str | None = None,
) -> Invite:
    """Insert an invite row and return it (flushed, ``id`` assigned).

    ``token_hash`` is unique, so a collision surfaces as an ``IntegrityError``
    rather than silently aliasing two links.
    """
    invite = Invite(
        kind=str(kind),
        role=role,
        token_hash=token_hash,
        max_uses=max_uses,
        expires_at=expires_at,
        created_by=int(created_by),
        created_at=utcnow(),
    )
    session.add(invite)
    await session.flush()
    return invite


async def get_by_hash(session: AsyncSession, token_hash: str) -> Invite | None:
    """Return the invite stored under ``token_hash`` or ``None``."""
    result = await session.execute(
        select(Invite).where(Invite.token_hash == token_hash)
    )
    return result.scalars().first()


async def list_active(
    session: AsyncSession, *, now: datetime | None = None
) -> list[Invite]:
    """Return every *redeemable* invite, newest first (§S3-3.6).

    An invite is active while it is neither revoked nor expired; a per-use
    ``max_uses`` that is already spent is **not** filtered out here, so the list
    keeps showing a used-up link until an admin revokes it (revoking is how it
    leaves the list).
    """
    reference = now or utcnow()
    result = await session.execute(
        select(Invite)
        .where(Invite.revoked_at.is_(None), Invite.expires_at > reference)
        .order_by(Invite.id.desc())
    )
    return list(result.scalars().all())


async def increment_use(
    session: AsyncSession, token_hash: str, *, now: datetime | None = None
) -> bool:
    """Consume one use of the invite matching ``token_hash`` (§S3-3.3).

    Returns ``True`` only for the single caller whose guarded ``UPDATE`` matched
    a row — i.e. the invite exists, is not revoked, is not expired and still has
    uses left. The ``NULL``-safe predicates accept an ``∞`` ``max_uses`` and a
    never-expiring ``expires_at`` alike. The caller owns the transaction, so the
    increment and whatever the redemption write afterwards commit together.
    """
    reference = now or utcnow()
    result = await session.execute(
        update(Invite)
        .where(
            Invite.token_hash == token_hash,
            Invite.revoked_at.is_(None),
            Invite.expires_at.is_not(None),
            Invite.expires_at > reference,
            (Invite.max_uses.is_(None)) | (Invite.uses < Invite.max_uses),
        )
        .values(uses=Invite.uses + 1)
    )
    await session.flush()
    return int(getattr(result, "rowcount", 0)) == 1


async def revoke(session: AsyncSession, invite_id: int) -> bool:
    """Stamp ``revoked_at`` on the invite; ``False`` when already revoked/gone."""
    result = await session.execute(
        update(Invite)
        .where(Invite.id == int(invite_id), Invite.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    await session.flush()
    return int(getattr(result, "rowcount", 0)) == 1


__all__ = [
    "create",
    "get_by_hash",
    "increment_use",
    "list_active",
    "revoke",
]
