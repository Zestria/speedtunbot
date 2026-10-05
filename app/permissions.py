"""Roles, permissions and enforcement helpers (``TASK_PLAN.md`` §2.5 / M0-06).

The role → permission matrix lives here as the single source of truth.
:class:`~app.services.admins.AdminService` resolves a Telegram ID to a
:class:`Role`; this module exposes the async enforcement surface
(:func:`get_role`, :func:`has`, :func:`staff_with`), the :func:`require`
decorator for message handlers and :func:`check_callback` for callbacks.

The acting bot and the :class:`AdminService` are injected once at startup with
:func:`configure`, so this module never imports the legacy ``loads`` singletons
(which would need a live ``.env`` at import time).
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class Role(StrEnum):
    """Staff roles. ``owner`` is env-only; others come from the ``admins`` table."""

    OWNER = "owner"
    ADMIN = "admin"
    SUPPORT = "support"


class Permission(StrEnum):
    """Every permission row from ``TASK_PLAN.md`` §2.5."""

    USERS_VIEW = "users.view"
    SUPPORT_REPLY = "support.reply"
    USERS_EDIT = "users.edit"
    USERS_BAN = "users.ban"
    ACCESS_REVIEW = "access.review"
    INVITES_CREATE = "invites.create"
    PAYMENTS_REVIEW = "payments.review"
    PAYMENTS_VIEW = "payments.view"
    PAYMENTS_REVOKE = "payments.revoke"
    BROADCAST_SEND = "broadcast.send"
    SERVER_VIEW = "server.view"
    MAINTENANCE_TOGGLE = "maintenance.toggle"
    USERS_DELETE = "users.delete"
    USERS_MASS_GRANT = "users.mass_grant"
    SERVER_RESTART = "server.restart"
    SERVER_BACKUP = "server.backup"
    SETTINGS_EDIT = "settings.edit"
    ADMINS_MANAGE = "admins.manage"
    AUDIT_VIEW = "audit.view"


_OWNER_ONLY: frozenset[Permission] = frozenset(
    {
        Permission.USERS_DELETE,
        Permission.USERS_MASS_GRANT,
        Permission.SERVER_RESTART,
        Permission.SERVER_BACKUP,
        Permission.SETTINGS_EDIT,
        Permission.ADMINS_MANAGE,
        Permission.AUDIT_VIEW,
    }
)

_ADMIN_EXTRA: frozenset[Permission] = frozenset(
    {
        Permission.USERS_EDIT,
        Permission.USERS_BAN,
        Permission.ACCESS_REVIEW,
        Permission.INVITES_CREATE,
        Permission.PAYMENTS_REVIEW,
        Permission.PAYMENTS_VIEW,
        Permission.PAYMENTS_REVOKE,
        Permission.BROADCAST_SEND,
        Permission.SERVER_VIEW,
        Permission.MAINTENANCE_TOGGLE,
    }
)

_SHARED: frozenset[Permission] = frozenset(
    {Permission.USERS_VIEW, Permission.SUPPORT_REPLY}
)

#: ``owner`` ⊃ ``admin`` ⊃ ``support`` — matches §2.5 exactly.
ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.OWNER: frozenset(Permission),
    Role.ADMIN: _SHARED | _ADMIN_EXTRA,
    Role.SUPPORT: _SHARED,
}

#: Flag names on :class:`app.db.models.Admin` used for notification routing.
NOTIFY_FLAGS: frozenset[str] = frozenset(
    {"notify_payments", "notify_support", "notify_access"}
)


def permissions_for(role: Role | str | None) -> frozenset[Permission]:
    """Return the permission set granted to ``role`` (empty for ``None``)."""
    if role is None:
        return frozenset()
    try:
        return ROLE_PERMISSIONS[Role(role)]
    except ValueError:
        return frozenset()


def role_has(role: Role | str | None, permission: Permission) -> bool:
    """Pure check: does ``role`` grant ``permission``?  (no I/O, cache-free)"""
    return Permission(permission) in permissions_for(role)


# --- runtime context (injected once at startup) ----------------------------


@dataclass
class _Context:
    bot: Any | None = None
    admins: Any | None = None  # app.services.admins.AdminService


_context = _Context()


def configure(*, bot: Any | None = None, admins: Any | None = None) -> None:
    """Wire the acting bot and the :class:`AdminService` used for lookups.

    Called once from ``app.app`` startup. Tests may call it with a fake bot and
    an ``AdminService`` backed by an in-memory session.
    """
    if bot is not None:
        _context.bot = bot
    if admins is not None:
        _context.admins = admins


def _service() -> Any:
    """Return the configured :class:`AdminService` or raise a clear error."""
    if _context.admins is None:
        raise RuntimeError(
            "permissions.configure(admins=...) was never called; "
            "build an AdminService at startup"
        )
    return _context.admins


async def get_role(tg_id: int | None) -> Role | None:
    """Return ``tg_id``'s :class:`Role`, or ``None`` when they are not staff."""
    if tg_id is None:
        return None
    return await _service().get_role(int(tg_id))


async def has(tg_id: int | None, permission: Permission) -> bool:
    """Return ``True`` when ``tg_id`` holds ``permission``."""
    return role_has(await get_role(tg_id), permission)


async def staff_with(
    permission: Permission, notify_flag: str | None = None
) -> list[int]:
    """Return the IDs of every staff member holding ``permission``.

    ``notify_flag`` (one of :data:`NOTIFY_FLAGS`) additionally filters non-owner
    staff by the matching ``admins.notify_*`` column; owners are always included.
    """
    return await _service().staff_with_all(permission, notify_flag)


async def _deny_message(tg_id: int | None, chat_id: int | None, role: Role) -> None:
    """Tell a *staff* caller they lack the permission (non-staff are silent)."""
    bot = _context.bot
    if bot is None or chat_id is None:
        return
    try:
        await bot.send_message(chat_id, "⛔ Недостаточно прав.")
    except Exception:  # pragma: no cover - best-effort notice
        logger.debug("failed to notify %s about denied action", tg_id, exc_info=True)


def require(permission: Permission) -> Callable[[T], T]:
    """Decorator: run the handler only when the caller holds ``permission``.

    Non-staff callers are dropped silently (as the legacy ``ADMIN_IDS`` checks
    did); staff without the specific permission get a short notice.
    """

    def decorator(func: T) -> T:
        @functools.wraps(func)  # type: ignore[arg-type]
        async def wrapper(message: Any, *args: Any, **kwargs: Any) -> Any:
            tg_id = getattr(getattr(message, "from_user", None), "id", None)
            role = await get_role(tg_id)
            if role is None:
                return None
            if not role_has(role, permission):
                chat_id = getattr(getattr(message, "chat", None), "id", None)
                await _deny_message(tg_id, chat_id, role)
                return None
            return await func(message, *args, **kwargs)  # type: ignore[operator]

        return wrapper  # type: ignore[return-value]

    return decorator


async def check_callback(call: Any, permission: Permission) -> bool:
    """Re-check ``permission`` for a callback and answer when denied.

    Returns ``True`` when the caller may proceed. Buttons can be stale or
    forged, so callbacks must always call this (hiding buttons is cosmetic).
    """
    tg_id = getattr(getattr(call, "from_user", None), "id", None)
    if await has(tg_id, permission):
        return True
    bot = _context.bot
    call_id = getattr(call, "id", None)
    if bot is not None and call_id is not None:
        try:
            await bot.answer_callback_query(
                call_id, "Недостаточно прав", show_alert=True
            )
        except Exception:  # pragma: no cover - best-effort alert
            logger.debug("failed to answer denied callback", exc_info=True)
    return False
