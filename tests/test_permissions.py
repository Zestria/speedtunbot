"""RBAC tests (``TASK_PLAN.md`` §2.5 / M0-06.8).

The expected role × permission matrix is written out **independently** here
(not derived from ``ROLE_PERMISSIONS``) so a mistake in ``app/permissions.py``
fails the test.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from app.db.models import Admin
from app.permissions import (
    Permission,
    Role,
    check_callback,
    configure,
    get_role,
    has,
    require,
    role_has,
    staff_with,
)
from app.services.admins import AdminService
from app.services.notifier import Notifier
from app.settings import Settings
from tests.fakes import FakeBot

OWNER, ADMIN, SUPPORT, STRANGER = 1, 2, 3, 4

# Independent transcription of the §2.5 table.
EXPECTED: dict[Role, set[Permission]] = {
    Role.OWNER: set(Permission),
    Role.ADMIN: {
        Permission.USERS_VIEW,
        Permission.SUPPORT_REPLY,
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
    },
    Role.SUPPORT: {Permission.USERS_VIEW, Permission.SUPPORT_REPLY},
}


@pytest_asyncio.fixture
async def db_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Sessionmaker over the shared in-memory engine (for ``AdminService``)."""
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _add_admin(
    factory: async_sessionmaker[AsyncSession],
    tg_id: int,
    role: str,
    **flags: object,
) -> None:
    async with factory() as session:
        session.add(Admin(tg_id=tg_id, role=role, added_by=OWNER, **flags))
        await session.commit()


def _service(
    settings: Settings, factory: async_sessionmaker[AsyncSession]
) -> AdminService:
    return AdminService(settings, factory)


def _message(tg_id: int, chat_id: int = 99) -> SimpleNamespace:
    return SimpleNamespace(
        from_user=SimpleNamespace(id=tg_id), chat=SimpleNamespace(id=chat_id)
    )


# --- pure matrix -----------------------------------------------------------


def test_role_permission_matrix() -> None:
    for role, expected in EXPECTED.items():
        for permission in Permission:
            assert role_has(role, permission) is (permission in expected), (
                f"{role} x {permission}"
            )


def test_none_role_has_nothing() -> None:
    assert all(not role_has(None, p) for p in Permission)


# --- role resolution -------------------------------------------------------


async def test_owners_come_from_env_only(
    settings: Settings, db_factory: async_sessionmaker[AsyncSession]
) -> None:
    service = _service(settings, db_factory)
    assert await service.get_role(OWNER) is Role.OWNER
    # A stranger with no admins row is not staff.
    assert await service.get_role(STRANGER) is None


async def test_get_role_reads_admins_table(
    settings: Settings, db_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _add_admin(db_factory, SUPPORT, "support")
    service = _service(settings, db_factory)
    assert await service.get_role(SUPPORT) is Role.SUPPORT


async def test_revoked_admin_is_not_staff(
    settings: Settings, db_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with db_factory() as session:
        session.add(
            Admin(
                tg_id=ADMIN,
                role="admin",
                added_by=OWNER,
                revoked_at=datetime(2020, 1, 1),
            )
        )
        await session.commit()
    service = _service(settings, db_factory)
    assert await service.get_role(ADMIN) is None


async def test_cache_invalidated_after_admins_write(
    settings: Settings, db_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _add_admin(db_factory, ADMIN, "admin")
    service = _service(settings, db_factory)
    assert await service.get_role(ADMIN) is Role.ADMIN  # cached

    # Change the row behind the service's back: the cache hides it.
    async with db_factory() as session:
        row = await session.get(Admin, ADMIN)
        assert row is not None
        row.role = "support"
        await session.commit()
    assert await service.get_role(ADMIN) is Role.ADMIN

    # After an explicit write-invalidation the new role is visible.
    service.invalidate(ADMIN)
    assert await service.get_role(ADMIN) is Role.SUPPORT


# --- support cannot broadcast ---------------------------------------------


async def test_support_cannot_broadcast(
    settings: Settings, db_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _add_admin(db_factory, ADMIN, "admin")
    await _add_admin(db_factory, SUPPORT, "support")
    service = _service(settings, db_factory)

    assert await service.get_role(SUPPORT) is Role.SUPPORT
    assert not role_has(await service.get_role(SUPPORT), Permission.BROADCAST_SEND)
    assert role_has(await service.get_role(ADMIN), Permission.BROADCAST_SEND)
    assert role_has(await service.get_role(OWNER), Permission.BROADCAST_SEND)


# --- staff_with ------------------------------------------------------------


async def test_staff_with_and_notify_flag(
    settings: Settings, db_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _add_admin(db_factory, ADMIN, "admin", notify_support=False)
    await _add_admin(db_factory, SUPPORT, "support", notify_support=True)
    service = _service(settings, db_factory)

    # Admin holds broadcast.send; support does not.
    assert await service.staff_with_all(Permission.BROADCAST_SEND) == [OWNER, ADMIN]
    # notify_support=True filters out the admin (owner is always included).
    assert await service.staff_with_all(Permission.SUPPORT_REPLY, "notify_support") == [
        OWNER,
        SUPPORT,
    ]
    with pytest.raises(ValueError):
        await service.staff_with_all(Permission.SUPPORT_REPLY, "nope")


# --- enforcement helpers ---------------------------------------------------


async def test_require_passthrough_and_denial(
    settings: Settings, db_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _add_admin(db_factory, SUPPORT, "support")
    bot = FakeBot()
    configure(bot=bot, admins=_service(settings, db_factory))

    ran: list[int] = []

    @require(Permission.BROADCAST_SEND)
    async def handler(message: SimpleNamespace) -> None:
        ran.append(message.from_user.id)

    await handler(_message(OWNER))  # owner: allowed
    assert ran == [OWNER]

    ran.clear()
    await handler(_message(SUPPORT))  # staff lacking permission: notice, not run
    assert ran == []
    assert bot.sent == [(99, "⛔ Недостаточно прав.")]

    bot.sent.clear()
    await handler(_message(STRANGER))  # non-staff: silent
    assert ran == []
    assert bot.sent == []


async def test_check_callback_allows_and_alerts(
    settings: Settings, db_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _add_admin(db_factory, SUPPORT, "support")
    bot = FakeBot()
    configure(bot=bot, admins=_service(settings, db_factory))

    allowed = SimpleNamespace(id="cb1", from_user=SimpleNamespace(id=OWNER))
    assert await check_callback(allowed, Permission.PAYMENTS_REVIEW) is True
    assert bot.callback_answers == []

    denied = SimpleNamespace(id="cb2", from_user=SimpleNamespace(id=SUPPORT))
    assert await check_callback(denied, Permission.PAYMENTS_REVIEW) is False
    assert bot.callback_answers == [("cb2", "Недостаточно прав", True)]


# --- wiring helpers --------------------------------------------------------


async def test_get_role_has_and_staff_with_helpers(
    settings: Settings, db_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _add_admin(db_factory, ADMIN, "admin")
    configure(admins=_service(settings, db_factory))

    assert await get_role(ADMIN) is Role.ADMIN
    assert await has(ADMIN, Permission.PAYMENTS_REVIEW) is True
    assert await has(ADMIN, Permission.ADMINS_MANAGE) is False
    assert await staff_with(Permission.PAYMENTS_REVIEW, "notify_payments") == [
        OWNER,
        ADMIN,
    ]


async def test_notifier_recipients_resolution(
    settings: Settings, db_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _add_admin(db_factory, ADMIN, "admin", notify_payments=False)
    await _add_admin(db_factory, SUPPORT, "support")
    notifier = Notifier(_service(settings, db_factory))

    assert await notifier.recipients_for(Permission.PAYMENTS_REVIEW) == [OWNER, ADMIN]
    # The admin disabled payment pings; support lacks the permission anyway.
    assert await notifier.recipients_for(
        Permission.PAYMENTS_REVIEW, "notify_payments"
    ) == [OWNER]
    assert await notifier.recipients_for_kind("payment") == [OWNER]
    assert await notifier.recipients_for_kind("support") == [OWNER, ADMIN, SUPPORT]
