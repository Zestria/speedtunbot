"""``/start`` and ``/profile`` handler tests (``TASK_PLAN.md`` §M0-09.7).

Covers the two acceptance criteria from §M0-09 plus the defects they fix:

* a panel outage during ``/start`` or ``/profile`` yields a friendly reply, no
  exception, and a staff alert (B7);
* ``/start`` is what creates the ``users`` row, with ``status='approved'``;
* ``expiry == 0`` renders as "Бессрочно" (B4), and a user without a panel client
  is pointed at ``/start``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import texts
from app.container import Container
from app.db.models import UserStatus
from app.db.repositories import users as users_repo
from app.handlers.profile import profile_command, profile_text
from app.handlers.start import start_command
from tests.fakes import FakeBot, FakePanel

USER = 555
OWNER = 1


def message(
    tg_id: int, *, chat_id: int = 99, username: str | None = "neo"
) -> SimpleNamespace:
    """Minimal Telegram message: only the fields the handlers read."""
    return SimpleNamespace(
        from_user=SimpleNamespace(id=tg_id, username=username, first_name="Neo"),
        chat=SimpleNamespace(id=chat_id),
        text="/start",
    )


async def seed_user(
    factory: async_sessionmaker[AsyncSession],
    tg_id: int,
    status: UserStatus = UserStatus.APPROVED,
) -> None:
    """Insert a ``users`` row directly (bypassing ``/start``)."""
    async with factory() as session:
        await users_repo.upsert_from_telegram(session, tg_id, username="neo")
        await users_repo.set_status(session, tg_id, status)
        await session.commit()


async def get_status(
    factory: async_sessionmaker[AsyncSession], tg_id: int
) -> str | None:
    async with factory() as session:
        user = await users_repo.get(session, tg_id)
    return None if user is None else str(user.status)


# --- /start ----------------------------------------------------------------


async def test_start_creates_approved_row_and_panel_client(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC: the row is created here, with ``approved``, plus a panel client."""
    panel = handler_container.panel
    assert isinstance(panel, FakePanel)

    await start_command(message(USER), fake_bot, handler_container)

    assert await get_status(session_factory, USER) == UserStatus.APPROVED
    assert await panel.get_client(USER) is not None
    assert fake_bot.texts_to(99) == [texts.START_WELCOME]


async def test_start_welcomes_back_an_existing_client(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A user with a panel client gets "welcome back", not a second client."""
    panel = handler_container.panel
    assert isinstance(panel, FakePanel)
    panel.seed(USER, enable=False)
    await seed_user(session_factory, USER)

    await start_command(message(USER), fake_bot, handler_container)

    assert fake_bot.texts_to(99) == [texts.START_RETURNING]
    assert "ensure_client" not in panel.calls


async def test_start_panel_outage_replies_and_alerts_owners(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (B7): outage → friendly reply + staff alert, no exception."""
    panel = handler_container.panel
    assert isinstance(panel, FakePanel)
    panel.unavailable = True

    await start_command(message(USER), fake_bot, handler_container)

    assert fake_bot.texts_to(99) == [texts.ERROR_PANEL]
    assert await get_status(session_factory, USER) == UserStatus.APPROVED
    # The owner (id 1, from the settings fixture) was alerted.
    alerts = fake_bot.texts_to(OWNER)
    assert len(alerts) == 1
    assert "start" in alerts[0]


@pytest.mark.parametrize(
    "status", [UserStatus.PENDING, UserStatus.REJECTED, UserStatus.BLOCKED]
)
async def test_start_preserves_an_existing_status(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
    status: UserStatus,
) -> None:
    """``/start`` promotes only **new** rows; an existing status is untouched."""
    await seed_user(session_factory, USER, status)

    await start_command(message(USER), fake_bot, handler_container)

    assert await get_status(session_factory, USER) == status


async def test_start_promotes_a_new_row_to_approved(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The ``users`` row is inserted as ``new`` and promoted once, on creation."""
    await start_command(message(USER), fake_bot, handler_container)

    assert await get_status(session_factory, USER) == UserStatus.APPROVED


# --- /profile --------------------------------------------------------------


async def test_profile_reports_unlimited_expiry(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """``expiry == 0`` is the panel's unlimited marker → "Бессрочно"."""
    panel = handler_container.panel
    assert isinstance(panel, FakePanel)
    panel.seed(USER, expiry_ms=0, enable=True)

    await profile_command(message(USER), fake_bot, handler_container)

    (text,) = fake_bot.texts_to(99)
    assert texts.PROFILE_UNLIMITED in text
    assert texts.PROFILE_STATUS_ACTIVE in text
    assert fake_bot.messages[-1][2].get("parse_mode") == "HTML"


async def test_profile_outage_is_friendly_and_alerts(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """AC (B4): a panel outage never raises and never touches an undefined var."""
    panel = handler_container.panel
    assert isinstance(panel, FakePanel)
    panel.unavailable = True

    await profile_command(message(USER), fake_bot, handler_container)

    assert fake_bot.texts_to(99) == [texts.ERROR_PANEL]
    assert len(fake_bot.texts_to(OWNER)) == 1


async def test_profile_without_client_points_at_start(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """A user who never ran ``/start`` is told to run it."""
    await profile_command(message(USER), fake_bot, handler_container)

    assert fake_bot.texts_to(99) == [texts.PROFILE_NO_ACCOUNT]


def test_profile_text_formats_the_expiry_in_the_configured_timezone() -> None:
    """The card renders the panel's ms timestamp in ``settings.timezone``."""
    client = SimpleNamespace(enable=True, expiry_time=1_700_000_000_000, sub_id="abc")
    rendered = profile_text(
        client, sub_url_base="https://panel.example.com/sub/", timezone="Europe/Moscow"
    )

    assert "15.11.2023 01:13" in rendered
    assert "https://panel.example.com/sub/abc" in rendered


def test_profile_text_marks_a_zero_expiry_as_unlimited() -> None:
    """``expiry_time == 0`` is the panel's "unlimited" marker, not 1970 (B4)."""
    client = SimpleNamespace(enable=True, expiry_time=0, sub_id="abc")

    rendered = profile_text(client, sub_url_base="https://x/", timezone="UTC")

    assert texts.PROFILE_UNLIMITED in rendered
    assert "1970" not in rendered


@pytest.mark.parametrize("enable", [True, False])
async def test_profile_status_follows_the_enable_flag(
    handler_container: Container, fake_bot: FakeBot, enable: bool
) -> None:
    """The rendered status mirrors the panel's ``enable`` flag."""
    panel = handler_container.panel
    assert isinstance(panel, FakePanel)
    panel.seed(USER, expiry_ms=0, enable=enable)

    await profile_command(message(USER), fake_bot, handler_container)

    (text,) = fake_bot.texts_to(99)
    expected = texts.PROFILE_STATUS_ACTIVE if enable else texts.PROFILE_STATUS_INACTIVE
    assert expected in text
