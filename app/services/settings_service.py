"""Typed access to the ``settings`` key/value table (``TASK_PLAN.md`` §M0-05).

Values are cached in-process (single-process deployment) with a short TTL, so a
toggle (e.g. ``maintenance_mode``) takes effect on the very next middleware call
**without a restart** — this is the structural fix for B1. The TTL also means
out-of-band writers (the M0-11 CLI) are picked up within :data:`CACHE_TTL`
seconds instead of requiring a restart.

A missing key never hides a bug: every read falls back to :data:`DEFAULT_SETTINGS`
(``bank_details`` may additionally be seeded from the environment).
"""

from __future__ import annotations

import logging
from time import monotonic
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.repositories import settings as settings_repo
from app.db.repositories.settings import DEFAULT_SETTINGS

logger = logging.getLogger(__name__)

ACCESS_MODE = "access_mode"
MAINTENANCE_MODE = "maintenance_mode"
BANK_DETAILS = "bank_details"
REMINDERS_ENABLED = "reminders_enabled"

#: Allowed values for :data:`ACCESS_MODE` (§2.5 access model).
ACCESS_MODES: frozenset[str] = frozenset({"invite_only", "approval", "open"})

#: How long a cached value stays fresh before it is re-read from the database.
CACHE_TTL = 20.0


class SettingsService:
    """Typed get/set over ``settings`` with a TTL-bounded in-process cache."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        *,
        defaults: dict[str, Any] | None = None,
        ttl: float = CACHE_TTL,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._defaults: dict[str, Any] = dict(
            DEFAULT_SETTINGS if defaults is None else defaults
        )
        self._ttl = float(ttl)
        # key -> (value, monotonic deadline after which it must be reloaded)
        self._cache: dict[str, tuple[Any, float]] = {}

    # --- generic access ----------------------------------------------------

    async def get(self, key: str) -> Any:
        """Return the value of ``key`` (fresh cache → DB → default)."""
        cached = self._cache.get(key)
        if cached is not None and cached[1] > monotonic():
            return cached[0]
        value = await self._load(key)
        if value is None:
            value = self._defaults.get(key)
        self._cache[key] = (value, monotonic() + self._ttl)
        return value

    async def set(self, key: str, value: Any, *, updated_by: int | None = None) -> Any:
        """Persist ``key`` and refresh the cache; returns the stored value."""
        if self._sessionmaker is None:
            raise RuntimeError("SettingsService has no sessionmaker configured")
        async with self._sessionmaker() as session:
            await settings_repo.set_value(session, key, value, updated_by=updated_by)
            await session.commit()
        self._cache[key] = (value, monotonic() + self._ttl)
        return value

    def invalidate(self, key: str | None = None) -> None:
        """Drop one cached key (or the whole cache when ``key`` is ``None``)."""
        if key is None:
            self._cache.clear()
        else:
            self._cache.pop(key, None)

    async def _load(self, key: str) -> Any:
        """Read ``key`` straight from the database (``None`` when absent)."""
        if self._sessionmaker is None:
            return None
        async with self._sessionmaker() as session:
            return await settings_repo.get(session, key)

    # --- typed accessors ---------------------------------------------------

    async def access_mode(self) -> str:
        """Return the access mode, falling back to the default on bad data."""
        value = await self.get(ACCESS_MODE)
        if value not in ACCESS_MODES:
            return str(self._defaults.get(ACCESS_MODE, "approval"))
        return str(value)

    async def set_access_mode(self, mode: str, *, updated_by: int | None = None) -> str:
        """Validate and store the access mode."""
        if mode not in ACCESS_MODES:
            raise ValueError(f"unknown access mode: {mode!r}")
        await self.set(ACCESS_MODE, mode, updated_by=updated_by)
        return mode

    async def maintenance_mode(self) -> bool:
        """Return ``True`` while the bot is in maintenance mode (B1 fix)."""
        return bool(await self.get(MAINTENANCE_MODE))

    async def set_maintenance_mode(
        self, enabled: bool, *, updated_by: int | None = None
    ) -> bool:
        """Toggle maintenance mode."""
        return bool(
            await self.set(MAINTENANCE_MODE, bool(enabled), updated_by=updated_by)
        )

    async def bank_details(self) -> str | None:
        """Return the bank details shown to paying users (may be ``None``)."""
        value = await self.get(BANK_DETAILS)
        return None if value is None else str(value)

    async def set_bank_details(
        self, details: str | None, *, updated_by: int | None = None
    ) -> str | None:
        """Store the bank details."""
        await self.set(BANK_DETAILS, details, updated_by=updated_by)
        return details

    async def reminders_enabled(self) -> bool:
        """Return ``True`` when expiry reminders may be sent."""
        return bool(await self.get(REMINDERS_ENABLED))

    async def set_reminders_enabled(
        self, enabled: bool, *, updated_by: int | None = None
    ) -> bool:
        """Toggle expiry reminders."""
        return bool(
            await self.set(REMINDERS_ENABLED, bool(enabled), updated_by=updated_by)
        )
