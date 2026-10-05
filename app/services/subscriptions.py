"""Subscription expiry math and grants (``TASK_PLAN.md`` §2.7 / task M0-04).

Pure date arithmetic lives in :func:`calculate_expiry_ms`; every mutation is
delegated to :class:`~app.services.panel.PanelGateway`, so the read-modify-write
for a grant runs under the gateway's global write lock and concurrent grants
for the same user add up instead of losing one.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from py3xui import Client

from app.services.panel import PanelGateway

logger = logging.getLogger(__name__)

MS_PER_DAY = 86_400_000

#: Signal for callers when a client is perpetual (``expiry_time == 0``).
UNLIMITED_REASON = "клиент бессрочный"


def is_unlimited(expiry_ms: int | None, enabled: bool) -> bool:
    """Return ``True`` **only** for a perpetual client.

    A client is unlimited when ``expiry_time == 0`` **and** it is enabled. The
    legacy import shape — a client created with ``enable=False`` and
    ``expiry_time == 0`` — has never been activated and must not be mistaken for
    a lifetime subscription (otherwise a grant would enable it forever and a
    ``/unban`` would activate it indefinitely). An unknown (``None``) expiry is
    never assumed to be perpetual.
    """
    if expiry_ms is None:
        return False
    return int(expiry_ms) == 0 and bool(enabled)


def base_expiry_ms(expiry_ms: int | None, enabled: bool, now: int) -> int:
    """Normalise a panel expiry for :func:`calculate_expiry_ms`.

    An unset expiry on a **disabled** client (never activated) counts from
    ``now``, so extending it produces a finite date instead of a perpetual
    (``0``) one; ``0`` on an enabled client stays ``0``. An unknown (``None``)
    expiry also counts from ``now``.
    """
    if expiry_ms is None:
        return int(now)
    value = int(expiry_ms)
    if value == 0 and not enabled:
        return int(now)
    return value


def calculate_expiry_ms(old_ms: int | None, days: int, now_ms: int) -> int:
    """Return the new expiry timestamp (epoch ms) after adding ``days``.

    * ``old_ms`` is ``None`` (or negative, the panel's "start on first use"
      marker) → ``now_ms + days`` (a delayed start begins counting from now);
    * ``old_ms`` is ``0`` → ``0`` (perpetual: never modified);
    * ``old_ms`` is in the future → extend from the future date;
    * ``old_ms`` is in the past (expired) → extend from ``now_ms``.

    Callers that read the value straight off a panel client must run it through
    :func:`base_expiry_ms` first, so a disabled placeholder (``0`` with
    ``enable=False``) counts from ``now``.
    """
    delta = int(days) * MS_PER_DAY
    if old_ms is None or old_ms < 0:
        return now_ms + delta
    if old_ms == 0:
        return 0
    return max(old_ms, now_ms) + delta


def now_ms() -> int:
    """Current time as epoch milliseconds."""
    return int(time.time() * 1000)


@dataclass(frozen=True)
class GrantResult:
    """Outcome of a subscription change."""

    tg_id: int
    expiry_ms: int
    changed: bool
    #: Set when nothing changed; e.g. :data:`UNLIMITED_REASON`.
    reason: str | None = None


class SubscriptionService:
    """Expiry/limit mutations on top of :class:`PanelGateway`."""

    def __init__(self, gateway: PanelGateway) -> None:
        self._gateway = gateway

    async def grant_days(
        self, tg_id: int, days: int, actor: int | None = None
    ) -> GrantResult:
        """Add ``days`` to the client's expiry (never shortens it)."""
        days = int(days)

        expected: dict[str, int] = {}

        def apply(client: Client) -> bool:
            if is_unlimited(client.expiry_time, bool(client.enable)):
                return False  # perpetual — leave untouched
            base_ms = base_expiry_ms(client.expiry_time, bool(client.enable), now_ms())
            ms = calculate_expiry_ms(base_ms, days, now_ms())
            expected["ms"] = ms
            client.expiry_time = ms
            return True

        fresh = await self._gateway.mutate(
            tg_id,
            apply,
            lambda c: int(c.expiry_time) == expected["ms"],
            desc=f"grant_days({days})",
        )
        if not expected:
            return GrantResult(tg_id, 0, changed=False, reason=UNLIMITED_REASON)
        logger.info(
            "grant_days tg_id=%s days=%s actor=%s expiry_ms=%s",
            tg_id,
            days,
            actor,
            fresh.expiry_time,
        )
        return GrantResult(tg_id, int(fresh.expiry_time), changed=True)

    async def set_expiry(
        self, tg_id: int, ms: int, actor: int | None = None
    ) -> GrantResult:
        """Set an absolute expiry timestamp (epoch ms), e.g. from a date FSM."""
        ms = int(ms)
        fresh = await self._gateway.set_expiry_ms(tg_id, ms)
        logger.info("set_expiry tg_id=%s ms=%s actor=%s", tg_id, ms, actor)
        return GrantResult(tg_id, int(fresh.expiry_time), changed=True)

    async def freeze(self, tg_id: int, frozen: bool) -> Client:
        """Freeze (``enable=False``) or unfreeze (``enable=True``) a client."""
        client = await self._gateway.set_enabled(tg_id, not frozen)
        logger.info("freeze tg_id=%s frozen=%s", tg_id, frozen)
        return client
