"""Audit trail service (``TASK_PLAN.md`` §2.4 / §M0-08.1).

:meth:`AuditService.log` appends one row to ``audit_log`` in its **own** short
transaction and never raises: auditing is a side effect of an action that has
already happened, so a database hiccup while writing the trail must not fail the
use case that triggered it (``TASK_PLAN.md`` §0.1 rule 6 — no side effect may
take down a handler).

``details`` is stored as JSON (any JSON-serialisable mapping/list); the schema
columns ``actor_tg_id``/``actor_role`` are ``NOT NULL``, so a missing actor is
recorded as :data:`SYSTEM_ACTOR` and a missing role as an empty string.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.repositories import audit as audit_repo

logger = logging.getLogger(__name__)

#: ``actor_tg_id`` recorded for actions nobody triggered (jobs, startup, timers).
SYSTEM_ACTOR = 0
#: ``actor_role`` recorded when the caller does not pass one.
UNKNOWN_ROLE = ""


class AuditService:
    """Append-only audit writer bound to the application sessionmaker."""

    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession] | None = None
    ) -> None:
        self._sessionmaker = sessionmaker

    async def log(
        self,
        actor: int | None,
        action: str,
        target_type: str | None = None,
        target_id: str | int | None = None,
        *,
        role: str | None = None,
        **details: Any,
    ) -> bool:
        """Append one audit row; return ``True`` when it was written.

        Never raises: failures are logged and reported as ``False`` so callers
        can ignore the result entirely.
        """
        sessionmaker = self._sessionmaker
        if sessionmaker is None:
            logger.warning("audit(%s) skipped: no sessionmaker configured", action)
            return False
        try:
            async with sessionmaker() as session:
                await audit_repo.add(
                    session,
                    actor_tg_id=SYSTEM_ACTOR if actor is None else int(actor),
                    actor_role=UNKNOWN_ROLE if role is None else str(role),
                    action=action,
                    target_type=target_type,
                    target_id=None if target_id is None else str(target_id),
                    details=details or None,
                )
                await session.commit()
        except Exception:  # never break the use case on an audit failure
            logger.warning("failed to write audit row %s", action, exc_info=True)
            return False
        return True
