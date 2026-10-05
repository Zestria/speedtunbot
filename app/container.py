"""Explicit dependency container.

Replaces the ``loads.py`` import-time singletons with an object that is built
once during startup and passed to the handler factories as ``c``
(``TASK_PLAN.md`` §2.1 / §2.8).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.repositories.settings import DEFAULT_SETTINGS
from app.services.admins import AdminService
from app.services.audit import AuditService
from app.services.notifier import Notifier
from app.services.panel import PanelGateway
from app.services.settings_service import SettingsService
from app.services.subscriptions import SubscriptionService
from app.settings import Settings


@dataclass
class Container:
    """Shared, mutable application state assembled at startup."""

    settings: Settings
    # Cached bot username used to build deep links, e.g.
    # ``https://t.me/<bot_username>?start=...`` (§2.8 step 3). Populated from
    # ``bot.get_me()`` exactly once during startup.
    bot_username: str | None = None
    engine: AsyncEngine | None = field(default=None)
    sessionmaker: async_sessionmaker[AsyncSession] | None = field(default=None)
    # Panel access (§2.7). Built during startup via :meth:`init_panel`.
    panel: PanelGateway | None = field(default=None)
    subscriptions: SubscriptionService | None = field(default=None)
    # RBAC (§2.5 / M0-06). Built during startup via :meth:`init_rbac`.
    admins: AdminService | None = field(default=None)
    notifier: Notifier | None = field(default=None)
    # Runtime settings (M0-05). Built during startup via :meth:`init_settings`.
    settings_service: SettingsService | None = field(default=None)
    # Audit trail (M0-08). Built during startup via :meth:`init_audit`.
    audit: AuditService | None = field(default=None)

    def init_panel(self) -> tuple[PanelGateway, SubscriptionService]:
        """Create the panel gateway + subscription service from settings."""
        gateway = PanelGateway(self.settings)
        self.panel = gateway
        self.subscriptions = SubscriptionService(gateway)
        return gateway, self.subscriptions

    def init_rbac(self) -> tuple[AdminService, Notifier]:
        """Create the admin service + notifier (needs the DB sessionmaker)."""
        admins = AdminService(self.settings, self.sessionmaker)
        notifier = Notifier(admins, sessionmaker=self.sessionmaker)
        self.admins = admins
        self.notifier = notifier
        return admins, notifier

    def init_settings(self) -> SettingsService:
        """Create the settings service (needs the DB sessionmaker).

        The service falls back to :data:`~app.db.repositories.settings.DEFAULT_SETTINGS`
        for missing keys; ``bank_details`` is additionally seeded from the
        legacy env variable when it is set (§M0-05.2).
        """
        defaults = dict(DEFAULT_SETTINGS)
        if self.settings.bank_account_details is not None:
            defaults["bank_details"] = self.settings.bank_account_details
        service = SettingsService(self.sessionmaker, defaults=defaults)
        self.settings_service = service
        return service

    def init_audit(self) -> AuditService:
        """Create the audit service (needs the DB sessionmaker, §M0-08.1)."""
        audit = AuditService(self.sessionmaker)
        self.audit = audit
        return audit

    @asynccontextmanager
    async def db(self) -> AsyncIterator[AsyncSession]:
        """Yield a session bound to one transaction (unit of work).

        Commits on clean exit, rolls back on any exception so each use case is
        atomic (``TASK_PLAN.md`` §M0-02.14).
        """
        if self.sessionmaker is None:
            raise RuntimeError("Container.sessionmaker is not configured")
        session = self.sessionmaker()
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def dispose(self) -> None:
        """Dispose the database engine (called on shutdown)."""
        if self.engine is not None:
            await self.engine.dispose()
