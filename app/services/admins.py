"""Admin role resolution and staff lookup (``TASK_PLAN.md`` §2.5 / M0-06).

Read side of admin management: resolves a Telegram ID to a :class:`Role`
(owners straight from settings, everyone else from the ``admins`` table) with a
small in-process cache that :meth:`AdminService.invalidate` clears on every
``admins`` write. Also answers "who holds permission X?" for notifier routing.

Writes to the ``admins`` table land with the ``admins.manage`` UI in a later
milestone; this service already owns the cache so those writers only need to
call :meth:`invalidate`.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Admin
from app.permissions import NOTIFY_FLAGS, Permission, Role, role_has
from app.settings import Settings

logger = logging.getLogger(__name__)


class AdminService:
    """Resolve roles and enumerate staff, with cache invalidation."""

    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._owner_ids: frozenset[int] = frozenset(settings.owner_ids)
        self._sessionmaker = sessionmaker
        # Cache only the ``admins``-table rows (owners never change at runtime).
        self._cache: dict[int, Role | None] = {}

    @property
    def owner_ids(self) -> frozenset[int]:
        """Owner Telegram IDs (env-only; §2.5)."""
        return self._owner_ids

    def invalidate(self, tg_id: int | None = None) -> None:
        """Drop the cached role for ``tg_id`` (or the whole cache when ``None``)."""
        if tg_id is None:
            self._cache.clear()
        else:
            self._cache.pop(int(tg_id), None)

    async def get_role(self, tg_id: int) -> Role | None:
        """Return ``tg_id``'s role: owner (env), then ``admins`` row, else ``None``."""
        tg_id = int(tg_id)
        if tg_id in self._owner_ids:
            return Role.OWNER
        if tg_id in self._cache:
            return self._cache[tg_id]
        role = await self._lookup(tg_id)
        self._cache[tg_id] = role
        return role

    async def _lookup(self, tg_id: int) -> Role | None:
        """Read one non-revoked ``admins`` row and map its role string."""
        sessionmaker = self._sessionmaker
        if sessionmaker is None:
            return None
        async with sessionmaker() as session:
            row = await session.get(Admin, tg_id)
        if row is None or row.revoked_at is not None:
            return None
        try:
            return Role(row.role)
        except ValueError:
            logger.warning("admin %s has unknown role %r", tg_id, row.role)
            return None

    async def _all_admins(self) -> list[Admin]:
        """Return every active (non-revoked) ``admins`` row."""
        sessionmaker = self._sessionmaker
        if sessionmaker is None:
            return []
        async with sessionmaker() as session:
            result = await session.execute(
                select(Admin).where(Admin.revoked_at.is_(None))
            )
            return list(result.scalars().all())

    async def staff_with_all(
        self, permission: Permission, notify_flag: str | None = None
    ) -> list[int]:
        """Return IDs of staff holding ``permission`` (owners always included)."""
        if notify_flag is not None and notify_flag not in NOTIFY_FLAGS:
            raise ValueError(f"unknown notify flag: {notify_flag!r}")
        ids: set[int] = set(self._owner_ids)
        for row in await self._all_admins():
            if not role_has(row.role, permission):
                continue
            if notify_flag is not None and not getattr(row, notify_flag, False):
                continue
            ids.add(int(row.tg_id))
        return sorted(ids)
