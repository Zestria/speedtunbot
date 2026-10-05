"""Settings service + middleware tests (``TASK_PLAN.md`` §M0-05.9).

Covers the two acceptance criteria from §M0-05 (maintenance applies immediately,
a handler exception does not lock a user out) plus the settings cache and the
fixed registration order.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)
from telebot.asyncio_handler_backends import CancelUpdate

from app.container import Container
from app.db.models import Admin, User, UserStatus
from app.db.repositories import settings as settings_repo
from app.db.repositories import users as users_repo
from app.db.repositories.settings import DEFAULT_SETTINGS
from app.middlewares import (
    AccessMiddleware,
    ContextMiddleware,
    MaintenanceMiddleware,
    ThrottleMiddleware,
    build_middlewares,
)
from app.middlewares.base import chat_id, is_callback, user_id
from app.middlewares.throttle import IN_FLIGHT_TTL, RATE_LIMIT
from app.permissions import Role
from app.services import settings_service as settings_service_module
from app.services.settings_service import SettingsService
from app.settings import Settings
from tests.fakes import FakeBot

OWNER, STAFF, STRANGER = 1, 2, 3


@pytest_asyncio.fixture
async def factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def container(
    settings: Settings, factory: async_sessionmaker[AsyncSession]
) -> Container:
    c = Container(settings=settings, sessionmaker=factory)
    c.init_rbac()
    c.init_settings()
    return c


def _message(tg_id: int, *, username: str | None = "neo", chat: int = 99) -> object:
    return SimpleNamespace(
        from_user=SimpleNamespace(id=tg_id, username=username, first_name="Neo"),
        chat=SimpleNamespace(id=chat),
    )


def _callback(tg_id: int, *, data: str = "pay:sel:1", cid: str = "cb1") -> object:
    return SimpleNamespace(
        id=cid,
        data=data,
        from_user=SimpleNamespace(id=tg_id, username="neo", first_name="Neo"),
        message=SimpleNamespace(chat=SimpleNamespace(id=99)),
    )


# --- settings service ------------------------------------------------------


async def test_settings_defaults_when_keys_missing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = SettingsService(factory)
    assert await service.access_mode() == "approval"
    assert await service.maintenance_mode() is False
    assert await service.bank_details() is None
    assert await service.reminders_enabled() is True


async def test_settings_read_after_write(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = SettingsService(factory)
    assert await service.maintenance_mode() is False
    await service.set_maintenance_mode(True)
    # Cache updated on write: no reload needed.
    assert await service.maintenance_mode() is True
    # And the value is persisted for a fresh process.
    assert await SettingsService(factory).maintenance_mode() is True


async def test_settings_cache_expires_and_picks_up_external_writes(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"t": 1000.0}
    monkeypatch.setattr(settings_service_module, "monotonic", lambda: clock["t"])
    service = settings_service_module.SettingsService(factory, ttl=20.0)

    # Cache the default (the DB has no row yet).
    assert await service.maintenance_mode() is False

    # An out-of-band writer (the M0-11 CLI) flips the flag directly in the DB.
    async with factory() as session:
        await settings_repo.set_value(session, "maintenance_mode", True)
        await session.commit()

    # Served from cache inside the TTL window ...
    clock["t"] += 10
    assert await service.maintenance_mode() is False

    # ... and refreshed from the DB once the TTL has elapsed.
    clock["t"] += 20
    assert await service.maintenance_mode() is True


async def test_settings_bank_details_env_seed(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded = {**DEFAULT_SETTINGS, "bank_details": "CARD 1234"}
    assert await SettingsService(factory, defaults=seeded).bank_details() == "CARD 1234"


async def test_settings_rejects_bad_access_mode(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = SettingsService(factory)
    with pytest.raises(ValueError):
        await service.set_access_mode("nonsense")
    assert await service.access_mode() == "approval"


async def test_container_seeds_bank_details_from_env(
    settings: Settings, factory: async_sessionmaker[AsyncSession]
) -> None:
    seeded = settings.model_copy(update={"bank_account_details": "PAY 42"})
    c = Container(settings=seeded, sessionmaker=factory)
    service = c.init_settings()
    assert await service.bank_details() == "PAY 42"


# --- context middleware ----------------------------------------------------


async def test_context_stranger_creates_no_row(
    container: Container, factory: async_sessionmaker[AsyncSession]
) -> None:
    middleware = ContextMiddleware(container)
    data: dict = {}

    await middleware.pre_process(_message(STRANGER), data)

    assert data["role"] is None
    assert data["user"] is None
    async with factory() as session:
        assert await users_repo.get(session, STRANGER) is None


async def test_context_updates_existing_row_and_role(
    container: Container, factory: async_sessionmaker[AsyncSession]
) -> None:
    async with factory() as session:
        user = await users_repo.upsert_from_telegram(session, STRANGER, username="old")
        user.bot_blocked = True
        session.add(Admin(tg_id=STAFF, role="support", added_by=OWNER))
        await session.commit()

    middleware = ContextMiddleware(container)
    data: dict = {}
    await middleware.pre_process(_message(STRANGER, username="new"), data)
    assert data["user"] is not None
    assert data["user"].username == "new"
    assert data["user"].bot_blocked is False

    await middleware.pre_process(_message(STAFF), data)
    assert data["role"] is Role.SUPPORT

    await middleware.pre_process(_message(OWNER), data)
    assert data["role"] is Role.OWNER


# --- throttle middleware ---------------------------------------------------


async def test_throttle_releases_lock_after_exception() -> None:
    middleware = ThrottleMiddleware(bot=FakeBot())
    update = _message(STRANGER)
    data = {"role": None}

    assert await middleware.pre_process(update, data) is None
    # A raising handler still reaches post_process → the lock is released.
    await middleware.post_process(update, data, RuntimeError("boom"))
    assert await middleware.pre_process(update, data) is None


async def test_throttle_recovers_from_stale_lock() -> None:
    middleware = ThrottleMiddleware(bot=FakeBot())
    update = _message(STRANGER)
    data = {"role": None}

    middleware._in_flight[STRANGER] = time.monotonic() - (IN_FLIGHT_TTL + 1)
    assert await middleware.pre_process(update, data) is None


async def test_throttle_duplicate_callback_answers_wait() -> None:
    bot = FakeBot()
    middleware = ThrottleMiddleware(bot=bot)
    update = _callback(STRANGER)
    data = {"role": None}

    assert await middleware.pre_process(update, data) is None
    assert isinstance(await middleware.pre_process(update, data), CancelUpdate)
    assert bot.callback_answers == [("cb1", "Подождите…", False)]


async def test_throttle_duplicate_message_dropped_silently() -> None:
    bot = FakeBot()
    middleware = ThrottleMiddleware(bot=bot)
    update = _message(STRANGER)
    data = {"role": None}

    await middleware.pre_process(update, data)
    assert isinstance(await middleware.pre_process(update, data), CancelUpdate)
    assert bot.sent == []
    assert bot.callback_answers == []


async def test_throttle_rate_cap_and_staff_exemption() -> None:
    middleware = ThrottleMiddleware(bot=FakeBot())
    update = _message(STRANGER)
    non_staff = {"role": None}

    for _ in range(RATE_LIMIT):
        assert await middleware.pre_process(update, non_staff) is None
        await middleware.post_process(update, non_staff, None)
    assert isinstance(await middleware.pre_process(update, non_staff), CancelUpdate)

    staff = {"role": Role.ADMIN}
    for _ in range(RATE_LIMIT * 2):
        assert await middleware.pre_process(update, staff) is None
        await middleware.post_process(update, staff, None)


# --- maintenance middleware ------------------------------------------------


async def test_maintenance_applies_immediately_and_limits_notice(
    container: Container,
) -> None:
    bot = FakeBot()
    service = container.settings_service
    assert service is not None
    middleware = MaintenanceMiddleware(service, bot=bot)
    update = _message(STRANGER)
    data = {"role": None}

    assert await middleware.pre_process(update, data) is None

    await service.set_maintenance_mode(True)
    assert isinstance(await middleware.pre_process(update, data), CancelUpdate)
    assert len(bot.sent) == 1 and bot.sent[0][0] == STRANGER
    # The notice is rate-limited to once per 10 minutes per user.
    assert isinstance(await middleware.pre_process(update, data), CancelUpdate)
    assert len(bot.sent) == 1

    # Toggling back takes effect on the very next update.
    await service.set_maintenance_mode(False)
    assert await middleware.pre_process(update, data) is None


async def test_maintenance_staff_bypass(container: Container) -> None:
    service = container.settings_service
    assert service is not None
    await service.set_maintenance_mode(True)
    middleware = MaintenanceMiddleware(service, bot=FakeBot())

    assert await middleware.pre_process(_message(OWNER), {"role": Role.OWNER}) is None


# --- access middleware -----------------------------------------------------


async def test_access_drops_blocked_user() -> None:
    middleware = AccessMiddleware()

    blocked = SimpleNamespace(status=UserStatus.BLOCKED)
    approved = SimpleNamespace(status=UserStatus.APPROVED)

    assert isinstance(
        await middleware.pre_process(_message(STRANGER), {"user": blocked}),
        CancelUpdate,
    )
    assert await middleware.pre_process(_message(STRANGER), {"user": approved}) is None
    assert await middleware.pre_process(_message(STRANGER), {"user": None}) is None


# --- registration order / helpers ------------------------------------------


async def test_cancel_by_maintenance_leaves_no_throttle_lock(
    container: Container,
) -> None:
    """A `CancelUpdate` from an earlier middleware must not strand a lock.

    Telebot stops at the first `CancelUpdate` and skips `post_process`, so with
    the fixed order (… → Throttle last) Throttle never acquired one.
    """
    service = container.settings_service
    assert service is not None
    await service.set_maintenance_mode(True)

    middlewares = build_middlewares(container, FakeBot())
    update = _message(STRANGER)
    data: dict = {}
    for middleware in middlewares:
        if isinstance(await middleware.pre_process(update, data), CancelUpdate):
            break

    throttle = middlewares[-1]
    assert isinstance(throttle, ThrottleMiddleware)
    assert throttle._in_flight == {}


async def test_build_middlewares_order(container: Container) -> None:
    middlewares = build_middlewares(container, FakeBot())
    assert [type(m) for m in middlewares] == [
        ContextMiddleware,
        MaintenanceMiddleware,
        AccessMiddleware,
        ThrottleMiddleware,
    ]


def test_base_helpers() -> None:
    message = _message(STRANGER)
    callback = _callback(STRANGER)
    assert user_id(message) == STRANGER
    assert chat_id(message) == 99
    assert chat_id(callback) == 99
    assert is_callback(message) is False
    assert is_callback(callback) is True


async def test_users_touch_never_inserts(session: AsyncSession) -> None:
    assert await users_repo.touch(session, 42, username="ghost") is None
    await session.commit()
    assert await users_repo.get(session, 42) is None

    session.add(User(tg_id=7, status=UserStatus.APPROVED, bot_blocked=True))
    await session.commit()
    touched = await users_repo.touch(session, 7, username="neo")
    assert touched is not None and touched.username == "neo"
