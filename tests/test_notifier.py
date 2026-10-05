"""Notifier delivery tests (``TASK_PLAN.md`` §2.9 / M0-07.10).

Covers ``safe_send`` (429 retry, 403 → ``bot_blocked``, other errors), the
escaped-HTML delivery path (fixes B9), fan-out routing, all-copies card sync
and the error-signature limiter (fixes B2).
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)
from telebot.apihelper import ApiTelegramException

from app.db.models import Admin, User
from app.db.repositories import admin_cards as admin_cards_repo
from app.permissions import Permission
from app.services.admins import AdminService
from app.services.notifier import Notifier
from app.settings import Settings
from app.utils.text import esc

OWNER, ADMIN, SUPPORT = 1, 2, 3


def _api_error(
    code: int, description: str, **parameters: object
) -> ApiTelegramException:
    payload: dict[str, object] = {"error_code": code, "description": description}
    if parameters:
        payload["parameters"] = parameters
    return ApiTelegramException("send_message", b"", payload)


class _Message:
    """Minimal stand-in for a telebot ``Message`` (only ``message_id`` used)."""

    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


class FlakyBot:
    """Raises queued errors, then records sends; counts every attempt."""

    def __init__(self, errors: list[Exception] | None = None) -> None:
        self.errors = list(errors or [])
        self.attempts = 0
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: object) -> _Message:
        self.attempts += 1
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append((int(chat_id), text))
        return _Message(self.attempts)


class StrictHtmlBot:
    """Rejects raw ``<`` under ``parse_mode="HTML"`` the way Telegram does."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(
        self, chat_id: int, text: str, parse_mode: str | None = None, **kw: object
    ) -> _Message:
        if parse_mode == "HTML" and "<" in text:
            raise _api_error(
                400, "Bad Request: can't parse entities: unsupported start tag"
            )
        self.sent.append(text)
        return _Message(len(self.sent))


class EditBot:
    """Supports ``send_message`` + ``edit_message_text`` with a failure switch."""

    def __init__(self, fail_edit_chats: set[int] | None = None) -> None:
        self.fail = set(fail_edit_chats or set())
        self.sent: list[tuple[int, str]] = []
        self.edits: list[tuple[int, int, str]] = []
        self._next_id = 100

    async def send_message(self, chat_id: int, text: str, **kwargs: object) -> _Message:
        self._next_id += 1
        self.sent.append((int(chat_id), text))
        return _Message(self._next_id)

    async def edit_message_text(
        self, text: str, chat_id: int, message_id: int, **kwargs: object
    ) -> _Message:
        if int(chat_id) in self.fail:
            raise _api_error(400, "Bad Request: message to edit not found")
        self.edits.append((int(chat_id), int(message_id), text))
        return _Message(int(message_id))


@pytest_asyncio.fixture
async def db_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Sessionmaker over the shared in-memory engine."""
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _service(
    settings: Settings, factory: async_sessionmaker[AsyncSession]
) -> AdminService:
    return AdminService(settings, factory)


# --- safe_send -------------------------------------------------------------


async def test_safe_send_retries_once_on_429(
    settings: Settings, db_factory: async_sessionmaker[AsyncSession]
) -> None:
    bot = FlakyBot([_api_error(429, "Too Many Requests", retry_after=0)])
    notifier = Notifier(_service(settings, db_factory), bot=bot)

    assert await notifier.safe_send(OWNER, "hi") is True
    assert bot.attempts == 2  # original + one retry
    assert bot.sent == [(OWNER, "hi")]


async def test_safe_send_marks_bot_blocked_on_403(
    settings: Settings, db_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with db_factory() as session:
        session.add(User(tg_id=SUPPORT, username="s"))
        await session.commit()

    bot = FlakyBot([_api_error(403, "Forbidden: bot was blocked by the user")])
    notifier = Notifier(
        _service(settings, db_factory), bot=bot, sessionmaker=db_factory
    )

    assert await notifier.safe_send(SUPPORT, "hi") is False
    async with db_factory() as session:
        user = await session.get(User, SUPPORT)
        assert user is not None and user.bot_blocked is True


async def test_safe_send_returns_false_on_other_error(
    settings: Settings, db_factory: async_sessionmaker[AsyncSession]
) -> None:
    bot = FlakyBot([_api_error(400, "Bad Request: message is too long")])
    notifier = Notifier(
        _service(settings, db_factory), bot=bot, sessionmaker=db_factory
    )

    assert await notifier.safe_send(OWNER, "hi") is False
    assert bot.attempts == 1  # only 429 is retried


async def test_safe_send_without_bot_returns_false(
    settings: Settings, db_factory: async_sessionmaker[AsyncSession]
) -> None:
    notifier = Notifier(_service(settings, db_factory))
    assert await notifier.safe_send(OWNER, "hi") is False


async def test_safe_send_gives_up_when_retry_after_exceeds_cap(
    settings: Settings, db_factory: async_sessionmaker[AsyncSession]
) -> None:
    # Telegram asking for a long pause must not stall the send loop.
    bot = FlakyBot([_api_error(429, "Too Many Requests", retry_after=60)])
    notifier = Notifier(_service(settings, db_factory), bot=bot)

    assert await notifier.safe_send(OWNER, "hi") is False
    assert bot.attempts == 1  # no retry, no 60s sleep
    assert bot.sent == []


async def test_safe_send_patient_honours_a_long_retry_after(
    settings: Settings,
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``patient=True`` (§S2-6.5) sleeps the full delay and still delivers."""
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("app.services.notifier.asyncio.sleep", fake_sleep)
    bot = FlakyBot([_api_error(429, "Too Many Requests", retry_after=60)])
    notifier = Notifier(_service(settings, db_factory), bot=bot)

    assert await notifier.safe_send(OWNER, "hi", patient=True) is True
    assert slept == [60.0]  # the capped path would have given up instead
    assert bot.attempts == 2
    assert bot.sent == [(OWNER, "hi")]


async def test_bot_blocked_bookkeeping_failure_does_not_abort_fan_out(
    settings: Settings, db_factory: async_sessionmaker[AsyncSession]
) -> None:
    class BrokenSessionmaker:
        """A sessionmaker whose every call raises (simulated DB outage)."""

        def __call__(self) -> object:
            raise RuntimeError("database is down")

    class BlockedThenOkBot:
        def __init__(self) -> None:
            self.sent: list[tuple[int, str]] = []

        async def send_message(
            self, chat_id: int, text: str, **kwargs: object
        ) -> _Message:
            if int(chat_id) == SUPPORT:
                raise _api_error(403, "Forbidden: bot was blocked by the user")
            self.sent.append((int(chat_id), text))
            return _Message(1)

    bot = BlockedThenOkBot()
    notifier = Notifier(
        _service(settings, db_factory),
        bot=bot,
        sessionmaker=BrokenSessionmaker(),  # type: ignore[arg-type]
    )

    # The blocked user's failed bookkeeping write must not stop the others.
    delivered = await notifier.send_to([SUPPORT, OWNER], "hi")
    assert delivered == [OWNER]
    assert bot.sent == [(OWNER, "hi")]


# --- escaped HTML delivery (fixes B9) --------------------------------------


async def test_escaped_text_is_delivered_without_parse_errors(
    settings: Settings, db_factory: async_sessionmaker[AsyncSession]
) -> None:
    bot = StrictHtmlBot()
    notifier = Notifier(_service(settings, db_factory), bot=bot)
    raw = "user <b>& text"

    # Raw user text is rejected by HTML parse mode ...
    assert await notifier.safe_send(OWNER, raw, parse_mode="HTML") is False
    # ... the escaped form goes through.
    assert await notifier.safe_send(OWNER, esc(raw), parse_mode="HTML") is True
    assert bot.sent == ["user &lt;b&gt;&amp; text"]


# --- fan-out / routing -----------------------------------------------------


async def _add_admin(
    factory: async_sessionmaker[AsyncSession], tg_id: int, role: str, **flags: object
) -> None:
    async with factory() as session:
        session.add(Admin(tg_id=tg_id, role=role, added_by=OWNER, **flags))
        await session.commit()


async def test_fan_out_and_alert_staff_routing(
    settings: Settings, db_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _add_admin(db_factory, ADMIN, "admin", notify_support=True)
    await _add_admin(db_factory, SUPPORT, "support")
    bot = FlakyBot()
    notifier = Notifier(_service(settings, db_factory), bot=bot)

    assert await notifier.fan_out("support", "ping") == [OWNER, ADMIN, SUPPORT]

    # Explicit permission + flag overrides the kind route.
    delivered = await notifier.alert_staff(
        "ping", permission=Permission.PAYMENTS_REVIEW, flag="notify_payments"
    )
    assert delivered == [OWNER, ADMIN]


# --- admin cards -----------------------------------------------------------


async def test_send_card_then_sync_edits_all_copies(
    settings: Settings, db_factory: async_sessionmaker[AsyncSession]
) -> None:
    bot = EditBot(fail_edit_chats={ADMIN})
    notifier = Notifier(
        _service(settings, db_factory), bot=bot, sessionmaker=db_factory
    )

    delivered = await notifier.send_card("payment", 7, [OWNER, ADMIN], "card")
    assert delivered == [OWNER, ADMIN]

    async with db_factory() as session:
        copies = await admin_cards_repo.list_for(session, "payment", 7)
    assert sorted(c.chat_id for c in copies) == [OWNER, ADMIN]

    updated = await notifier.sync_card("payment", 7, "done")
    assert sorted(updated) == [OWNER, ADMIN]
    # OWNER's copy was edited in place ...
    assert [e[0] for e in bot.edits] == [OWNER]
    # ... ADMIN's edit failed, so a fresh message was sent instead.
    assert bot.sent[-1] == (ADMIN, "done")


# --- error-signature limiter (M0-07.4) -------------------------------------


def test_allow_error_alert_suppresses_duplicate_signatures(
    settings: Settings,
) -> None:
    now = [1000.0]
    notifier = Notifier(AdminService(settings, None), clock=lambda: now[0])

    assert notifier.allow_error_alert("boom") is True
    assert notifier.allow_error_alert("boom") is False
    assert notifier.allow_error_alert("other") is True
    now[0] += 601.0
    assert notifier.allow_error_alert("boom") is True
