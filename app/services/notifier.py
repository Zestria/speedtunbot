"""Staff notification routing (``TASK_PLAN.md`` §2.5 / M0-06).

This milestone implements **recipient resolution only** — deciding *which*
staff members should receive a card or alert. The actual delivery (``safe_send``,
``alert_staff``, retries, ``bot_blocked``) arrives in M0-07; nothing here
performs I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.db.models import CardKind
from app.permissions import Permission
from app.services.admins import AdminService


@dataclass(frozen=True)
class Route:
    """A permission + optional ``notify_*`` flag used to pick recipients."""

    permission: Permission
    flag: str | None = None


#: §2.5 routing table: card/alert kind → who should be notified.
ROUTES: dict[str, Route] = {
    CardKind.PAYMENT: Route(Permission.PAYMENTS_REVIEW, "notify_payments"),
    CardKind.ACCESS: Route(Permission.ACCESS_REVIEW, "notify_access"),
    "support": Route(Permission.SUPPORT_REPLY, "notify_support"),
    # Admin-grant requests and health/error alerts go to owners + server.view.
    CardKind.ADMIN_GRANT: Route(Permission.ADMINS_MANAGE),
    "alert": Route(Permission.SERVER_VIEW),
}


class Notifier:
    """Resolve the staff IDs a card/alert must reach (no sending)."""

    def __init__(self, admins: AdminService) -> None:
        self._admins = admins

    async def recipients_for(
        self, permission: Permission, flag: str | None = None
    ) -> list[int]:
        """Return IDs of staff holding ``permission``, filtered by ``flag``."""
        return await self._admins.staff_with_all(permission, flag)

    async def recipients_for_kind(self, kind: str) -> list[int]:
        """Resolve via :data:`ROUTES` for a :class:`~app.db.models.CardKind`."""
        try:
            route = ROUTES[str(kind)]
        except KeyError as exc:
            raise ValueError(f"unknown notification kind: {kind!r}") from exc
        return await self.recipients_for(route.permission, route.flag)
