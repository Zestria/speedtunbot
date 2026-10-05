"""Idempotent legacy import (``TASK_PLAN.md`` §M0-11, hardening pass).

The legacy bot kept its customer list on the 3x-ui panel only: a client whose
``email`` is the Telegram id, with ``@username`` in its ``comment``, and bans in a
JSON file next to the process. ``/broadcast``, ``/ban`` and ``/pay`` now read the
``users`` table and the JSON ban middleware is gone, so an **un-imported**
deployment is blind: broadcasts reach nobody, ``/ban`` answers "unknown user",
banned legacy clients keep working and ``/pay`` would violate the
``payments → users`` foreign key.

:class:`LegacyImporter` is the single routine behind both
``python -m app.cli import-legacy`` and the ``AUTO_IMPORT_LEGACY`` startup hook
(:func:`app.app.ensure_legacy_import`), so a manual and an automatic import can
never disagree. It is idempotent (a second run changes nothing) and, with
``dry_run=True``, reports what it *would* do without writing anything.

It also rewrites the legacy client shape that broke the unlimited logic: a client
created with ``enable=False`` **and** ``expiry_time=0`` (never activated) gets an
explicit ``now`` expiry, so it can never be read as a lifetime subscription
(:func:`app.services.subscriptions.is_unlimited`).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.base import utcnow
from app.db.models import Setting, User, UserStatus
from app.db.repositories import users as users_repo
from app.services.panel import PanelGateway
from app.services.subscriptions import is_unlimited, now_ms

logger = logging.getLogger(__name__)

#: ``settings`` key holding the payment details shown by ``/pay``.
BANK_DETAILS_KEY = "bank_details"


@dataclass
class ImportReport:
    """What one import run did (or, with ``dry_run=True``, would have done)."""

    panel_clients: int = 0
    created: int = 0
    refreshed: int = 0
    #: Legacy placeholders (``expiry_time == 0`` with ``enable=False``) pinned to now.
    normalized: list[int] = field(default_factory=list)
    banned_imported: int = 0
    banned_disabled: int = 0
    bank_details_seeded: bool = False

    @property
    def summary(self) -> str:
        """One-line human-readable summary (printed by the CLI)."""
        return (
            f"panel clients: {self.panel_clients}, users created: {self.created}, "
            f"users refreshed: {self.refreshed}, "
            f"bans imported: {self.banned_imported}, "
            f"clients disabled by ban: {self.banned_disabled}, "
            f"unset expiries pinned to now: {len(self.normalized)}, "
            f"bank details seeded: {self.bank_details_seeded}"
        )


def parse_username(comment: str | None) -> str | None:
    """Extract the username from a legacy panel client ``comment`` (``@name``)."""
    text = (comment or "").strip()
    if not text:
        return None
    return text.lstrip("@") or None


class LegacyImporter:
    """Import panel clients and the legacy ban list into the ``users`` table."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        *,
        panel: PanelGateway | None = None,
        bank_details: str | None = None,
        banned_users_file: str | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._panel = panel
        self._bank_details = bank_details
        self._banned_users_file = banned_users_file

    # --- inspection (used by the startup guard) ----------------------------

    async def count_users(self) -> int:
        """Return how many ``users`` rows exist."""
        sessionmaker = self._require_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(select(func.count()).select_from(User))
            return int(result.scalar_one())

    async def panel_customers(self) -> list[int]:
        """Return the Telegram ids of the panel's numeric-email clients."""
        return [tg_id for tg_id, _ in await self._numeric_clients()]

    # --- the import --------------------------------------------------------

    async def run(self, *, dry_run: bool = False) -> ImportReport:
        """Import everything that is still missing; safe to run repeatedly."""
        report = ImportReport()
        banned = self._banned_ids()
        clients = await self._numeric_clients()
        report.panel_clients = len(clients)
        report.banned_imported = len(banned & {tg_id for tg_id, _ in clients})

        for tg_id, client in clients:
            blocked = tg_id in banned
            created = await self._upsert_user(
                tg_id,
                username=parse_username(getattr(client, "comment", None)),
                client_uuid=str(getattr(client, "id", "") or "") or None,
                blocked=blocked,
                dry_run=dry_run,
            )
            if created:
                report.created += 1
            else:
                report.refreshed += 1
            await self._normalize_client(report, tg_id, client, dry_run=dry_run)
            if blocked and bool(getattr(client, "enable", False)):
                report.banned_disabled += 1
                if not dry_run:
                    await self._set_enabled(tg_id, enabled=False)

        report.bank_details_seeded = await self._seed_bank_details(dry_run=dry_run)
        logger.info(
            "legacy import%s: %s", " (dry run)" if dry_run else "", report.summary
        )
        return report

    # --- internals ---------------------------------------------------------

    async def _normalize_client(
        self, report: ImportReport, tg_id: int, client: Any, *, dry_run: bool
    ) -> None:
        """Pin a never-activated legacy client (``0`` expiry, disabled) to ``now``."""
        expiry_ms = int(getattr(client, "expiry_time", 0) or 0)
        enabled = bool(getattr(client, "enable", False))
        if expiry_ms != 0 or is_unlimited(expiry_ms, enabled):
            return
        report.normalized.append(tg_id)
        if dry_run:
            return
        panel = self._require_panel()
        try:
            await panel.set_expiry_ms(tg_id, now_ms())
        except Exception:  # one broken client must not abort the whole import
            logger.warning(
                "failed to pin the expiry of client %s", tg_id, exc_info=True
            )

    async def _set_enabled(self, tg_id: int, *, enabled: bool) -> None:
        """Best-effort panel enable/disable for an imported ban."""
        panel = self._require_panel()
        try:
            await panel.set_enabled(tg_id, enabled)
        except Exception:  # the ``users`` row is already written, so just report
            logger.warning(
                "failed to set enable=%s for client %s", enabled, tg_id, exc_info=True
            )

    async def _numeric_clients(self) -> list[tuple[int, Any]]:
        """Return ``(tg_id, client)`` for every panel client with a numeric email."""
        panel = self._require_panel()
        found: list[tuple[int, Any]] = []
        for client in await panel.list_clients():
            email = str(getattr(client, "email", "") or "")
            if not email.isdigit():
                logger.debug("skipping non-numeric panel client %r", email)
                continue
            found.append((int(email), client))
        return found

    def _banned_ids(self) -> set[int]:
        """Read the legacy ban list; a missing file simply means "no bans"."""
        path = self._banned_users_file
        if not path:
            return set()
        file = Path(path)
        if not file.exists():
            logger.info("legacy ban list %s not found; importing no bans", file)
            return set()
        try:
            data = json.loads(file.read_text(encoding="utf-8") or "[]")
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("cannot read the legacy ban list %s: %s", file, exc)
            return set()
        if not isinstance(data, list):
            logger.warning("legacy ban list %s is not a JSON list", file)
            return set()
        ids: set[int] = set()
        for item in data:
            try:
                ids.add(int(item))
            except (TypeError, ValueError):
                logger.warning("skipping non-numeric legacy ban entry %r", item)
        return ids

    async def _upsert_user(
        self,
        tg_id: int,
        *,
        username: str | None,
        client_uuid: str | None,
        blocked: bool,
        dry_run: bool,
    ) -> bool:
        """Create/refresh one ``users`` row; returns ``True`` when it was created."""
        sessionmaker = self._require_sessionmaker()
        created = False
        async with sessionmaker() as session:
            user = await users_repo.get(session, int(tg_id))
            if user is None:
                created = True
                moment = utcnow()
                session.add(
                    User(
                        tg_id=int(tg_id),
                        username=username,
                        status=str(
                            UserStatus.BLOCKED if blocked else UserStatus.APPROVED
                        ),
                        status_note="legacy import",
                        status_changed_at=moment,
                        panel_client_uuid=client_uuid,
                        created_at=moment,
                        last_seen_at=moment,
                    )
                )
            else:
                if username is not None:
                    user.username = username
                if client_uuid is not None:
                    user.panel_client_uuid = client_uuid
                if blocked and user.status != UserStatus.BLOCKED:
                    user.status = str(UserStatus.BLOCKED)
                    user.status_note = "legacy import"
                    user.status_changed_at = utcnow()
            await session.flush()
            if dry_run:
                await session.rollback()
            else:
                await session.commit()
        return created

    async def _seed_bank_details(self, *, dry_run: bool) -> bool:
        """Seed ``settings.bank_details`` from ``BANK_ACCOUNT_DETAILS`` if unset."""
        if not self._bank_details:
            return False
        sessionmaker = self._require_sessionmaker()
        async with sessionmaker() as session:
            row = await session.get(Setting, BANK_DETAILS_KEY)
            if row is not None and row.value:
                return False
            if dry_run:
                return True
            if row is None:
                session.add(
                    Setting(
                        key=BANK_DETAILS_KEY,
                        value=self._bank_details,
                        updated_at=utcnow(),
                    )
                )
            else:
                row.value = self._bank_details
                row.updated_at = utcnow()
            await session.flush()
            await session.commit()
        return True

    def _require_sessionmaker(self) -> async_sessionmaker[AsyncSession]:
        if self._sessionmaker is None:
            raise RuntimeError("LegacyImporter has no sessionmaker configured")
        return self._sessionmaker

    def _require_panel(self) -> PanelGateway:
        if self._panel is None:
            raise RuntimeError("LegacyImporter has no panel configured")
        return self._panel
