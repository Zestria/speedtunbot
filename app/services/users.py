"""User identity, access status and panel side-effects (``TASK_PLAN.md`` §M0-09).

``/start`` is the only place a ``users`` row is created (§M0-05.3): the context
middleware just refreshes an existing row. This service owns that write plus the
``blocked``/``approved`` transitions behind ``/ban`` and ``/unban``, so handlers
stay *parse input → check permission → call a service → render* (§0.1 rule 3).

Block semantics (§0.2): blocking a user also **disables** the panel client;
unblocking re-enables it **only while the subscription has not expired** — and a
client whose expiry is unset (``0``) is only perpetual when it is enabled, so a
never-activated legacy client is pinned to ``now`` instead of being activated
indefinitely. A panel outage never loses the state change — the DB row is written
first and the panel call is best effort, reporting its failure through
:attr:`AccessChange.panel_error` so the admin still gets an answer (§0.1 rule 7;
see ``docs/DECISIONS.md``).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import User, UserStatus
from app.db.repositories import users as users_repo
from app.services.audit import AuditService
from app.services.panel import PanelGateway
from app.services.subscriptions import is_unlimited

logger = logging.getLogger(__name__)

ACTION_BAN = "user.ban"
ACTION_UNBAN = "user.unban"
ACTION_REGISTER = "user.register"


def now_ms() -> int:
    """Current epoch milliseconds (the panel's time base)."""
    return int(time.time() * 1000)


@dataclass(frozen=True)
class Registration:
    """Result of ``/start``: the row to render plus whether the client existed."""

    tg_id: int
    user: User
    returning: bool


@dataclass(frozen=True)
class AccessChange:
    """Outcome of a ban/unban: new status + what happened to the panel client."""

    tg_id: int
    status: UserStatus
    #: ``False`` when the ``users`` table has no row for ``tg_id``.
    found: bool = True
    #: ``"disabled"``/``"enabled"``, ``"skipped"``, or ``None`` (no client).
    client_action: str | None = None
    #: Why the panel call did not happen/failed (rendered as a warning).
    panel_error: str | None = None


class UserService:
    """Access-status transitions over ``users`` + the matching panel client."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        *,
        panel: PanelGateway | None = None,
        audit: AuditService | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._panel = panel
        self._audit = audit

    async def register(
        self, tg_id: int, *, username: str | None = None, first_name: str | None = None
    ) -> Registration:
        """Create/refresh the user row and ensure the panel client exists.

        Access control is still open in M0 (M1 rewrites ``/start``), so a **newly
        created** row is promoted from ``new`` to :data:`UserStatus.APPROVED`
        right away. An existing row keeps its status untouched: ``/start`` must
        not overwrite an administrative state (``pending``/``rejected``/``blocked``)
        back to ``approved``.
        """
        sessionmaker = self._require_sessionmaker()
        async with sessionmaker() as session:
            created = await users_repo.get(session, int(tg_id)) is None
            user = await users_repo.upsert_from_telegram(
                session, int(tg_id), username=username, first_name=first_name
            )
            if created:
                await users_repo.set_status(
                    session, int(tg_id), UserStatus.APPROVED, note="self-registration"
                )
            await session.commit()

        await self._audit_log(
            int(tg_id), ACTION_REGISTER, target=tg_id, username=username
        )
        if self._panel is None:
            return Registration(int(tg_id), user, returning=False)

        # ``get_client`` first so a returning user gets the "welcome back" reply.
        existing = await self._panel.get_client(int(tg_id))
        if existing is not None:
            await self._store_client_uuid(int(tg_id), existing.id)
            return Registration(int(tg_id), user, returning=True)

        client = await self._panel.ensure_client(int(tg_id), username or "")
        await self._store_client_uuid(int(tg_id), client.id)
        return Registration(int(tg_id), user, returning=False)

    async def ban(
        self, tg_id: int, *, actor: int | None = None, role: str | None = None
    ) -> AccessChange:
        """Set ``status='blocked'`` and disable the panel client."""
        found = await self._set_status(
            int(tg_id), UserStatus.BLOCKED, actor=actor, note="ban"
        )
        if not found:
            return AccessChange(int(tg_id), UserStatus.BLOCKED, found=False)
        if await self._client_present(int(tg_id)) is False:
            # No panel client yet (never ran ``/start``): nothing to disable.
            change = AccessChange(int(tg_id), UserStatus.BLOCKED, client_action=None)
        else:
            action, error = await self._panel_disable(int(tg_id), enabled=False)
            change = AccessChange(
                int(tg_id), UserStatus.BLOCKED, client_action=action, panel_error=error
            )
        await self._audit_log(
            actor,
            ACTION_BAN,
            role=role,
            target=tg_id,
            client_action=change.client_action,
            panel_error=change.panel_error,
        )
        return change

    async def unban(
        self, tg_id: int, *, actor: int | None = None, role: str | None = None
    ) -> AccessChange:
        """Set ``status='approved'`` and re-enable the client if not expired."""
        found = await self._set_status(
            int(tg_id), UserStatus.APPROVED, actor=actor, note="unban"
        )
        if not found:
            return AccessChange(int(tg_id), UserStatus.APPROVED, found=False)
        traffic, read_error = await self._traffic(int(tg_id))
        if read_error is not None:
            # The expiry is unknown, so the client is left untouched; the error is
            # reported so the admin can retry once the panel answers again.
            change = AccessChange(
                int(tg_id),
                UserStatus.APPROVED,
                client_action=None,
                panel_error=read_error,
            )
        elif traffic is None:
            change = AccessChange(int(tg_id), UserStatus.APPROVED, client_action=None)
        elif is_unlimited(traffic.expiry_ms, traffic.enable) or (
            traffic.expiry_ms > now_ms()
        ):
            action, error = await self._panel_disable(int(tg_id), enabled=True)
            change = AccessChange(
                int(tg_id), UserStatus.APPROVED, client_action=action, panel_error=error
            )
        else:
            # Expired subscription *or* a never-activated legacy client
            # (``expiry_time == 0`` with ``enable=False``): unban restores bot
            # access only (§0.2). Pinning the unset expiry to ``now`` keeps the
            # client from ever being mistaken for a lifetime one again.
            error = None
            if traffic.expiry_ms == 0:
                error = await self._pin_expiry(int(tg_id))
            change = AccessChange(
                int(tg_id),
                UserStatus.APPROVED,
                client_action="skipped",
                panel_error=error,
            )
        await self._audit_log(
            actor,
            ACTION_UNBAN,
            role=role,
            target=tg_id,
            client_action=change.client_action,
            panel_error=change.panel_error,
        )
        return change

    async def banned(self) -> list[int]:
        """Return the Telegram IDs of every blocked user, ascending."""
        sessionmaker = self._require_sessionmaker()
        async with sessionmaker() as session:
            rows = await users_repo.list_blocked(session)
        return [int(row.tg_id) for row in rows]

    async def needs_panel_retry(self, tg_id: int) -> bool:
        """Return ``True`` when ``/unban`` should still try to enable the client.

        Covers the out-of-sync state left by a failed ``/unban`` (DB ``approved``,
        panel client still disabled) so an admin can retry instead of being told
        the user "is not banned". A failed panel read also returns ``True``: the
        retry attempt then reports the real error through
        :attr:`AccessChange.panel_error`.
        """
        if self._panel is None:
            return False
        try:
            client = await self._panel.get_client(tg_id)
        except Exception as exc:
            logger.warning("panel lookup failed for %s: %s", tg_id, exc)
            return True
        return client is not None and not bool(client.enable)

    async def status(self, tg_id: int) -> str | None:
        """Return the stored ``users.status`` for ``tg_id`` (``None`` if unknown)."""
        sessionmaker = self._require_sessionmaker()
        async with sessionmaker() as session:
            user = await users_repo.get(session, int(tg_id))
        return None if user is None else str(user.status)

    async def broadcast_targets(self) -> list[int]:
        """Return ``approved`` IDs that do not block the bot (§M0-09.4).

        One short read-only session fetches the ids and is closed immediately;
        the caller then sleeps ``PACING`` per recipient with **no** DB connection
        held, otherwise a large broadcast would starve the connection pool.
        """
        sessionmaker = self._require_sessionmaker()
        async with sessionmaker() as session:
            ids = await users_repo.list_broadcast_ids(session)
        return ids

    # --- internals ---------------------------------------------------------

    def _require_sessionmaker(self) -> async_sessionmaker[AsyncSession]:
        if self._sessionmaker is None:
            raise RuntimeError("UserService has no sessionmaker configured")
        return self._sessionmaker

    async def _set_status(
        self, tg_id: int, status: UserStatus, *, actor: int | None, note: str
    ) -> bool:
        """Write the new status; ``False`` when there is no such user row."""
        sessionmaker = self._require_sessionmaker()
        async with sessionmaker() as session:
            user = await users_repo.set_status(
                session, tg_id, status, changed_by=actor, note=note
            )
            await session.commit()
        return user is not None

    async def _client_present(self, tg_id: int) -> bool:
        """``False`` only when the panel is known to have no client for ``tg_id``.

        A failed read returns ``True`` so the caller still attempts the write and
        surfaces the real error through ``panel_error``.
        """
        if self._panel is None:
            return False
        try:
            return await self._panel.get_client(tg_id) is not None
        except Exception as exc:  # reported by the write attempt
            logger.warning("panel lookup failed for %s: %s", tg_id, exc)
            return True

    async def _store_client_uuid(self, tg_id: int, uuid: str | None) -> None:
        sessionmaker = self._require_sessionmaker()
        try:
            async with sessionmaker() as session:
                await users_repo.set_panel_client_uuid(session, tg_id, uuid)
                await session.commit()
        except Exception:  # pragma: no cover - bookkeeping must not break /start
            logger.warning("failed to store panel uuid for %s", tg_id, exc_info=True)

    async def _traffic(self, tg_id: int) -> tuple[Any, str | None]:
        """Return ``(traffic, error)`` for ``tg_id``.

        ``traffic is None`` with ``error is None`` means the panel simply has no
        client for this user; a non-``None`` error means the read failed and the
        caller must not assume anything about the client.
        """
        if self._panel is None:
            return None, None
        try:
            return await self._panel.get_traffic(tg_id), None
        except Exception as exc:  # reported through ``panel_error``
            logger.warning("panel read failed for %s: %s", tg_id, exc)
            return None, str(exc)

    async def _panel_disable(
        self, tg_id: int, *, enabled: bool
    ) -> tuple[str | None, str | None]:
        """Best-effort ``set_enabled``; returns ``(action, error)``."""
        if self._panel is None:
            return None, "panel is not configured"
        try:
            await self._panel.set_enabled(tg_id, enabled)
        except Exception as exc:  # surfaced to the admin as a warning line
            logger.warning("panel set_enabled failed for %s: %s", tg_id, exc)
            return None, str(exc)
        return ("enabled" if enabled else "disabled"), None

    async def _pin_expiry(self, tg_id: int) -> str | None:
        """Best-effort: give an unset expiry (``0``) the explicit value ``now``.

        A legacy client created disabled with ``expiry_time == 0`` must not stay
        indistinguishable from a perpetual one, otherwise the next ``/unban``
        would activate it forever (returns the error string, if any).
        """
        if self._panel is None:
            return "panel is not configured"
        try:
            await self._panel.set_expiry_ms(int(tg_id), now_ms())
        except Exception as exc:  # surfaced to the admin as a warning line
            logger.warning("panel set_expiry failed for %s: %s", tg_id, exc)
            return str(exc)
        return None

    async def _audit_log(
        self,
        actor: int | None,
        action: str,
        *,
        role: str | None = None,
        target: int | None = None,
        **details: object,
    ) -> None:
        if self._audit is None:
            return
        await self._audit.log(actor, action, "user", target, role=role, **details)
