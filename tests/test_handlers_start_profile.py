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
from app.callbacks import ProfileNav, unpack
from app.container import Container
from app.db.models import UserStatus
from app.db.repositories import users as users_repo
from app.handlers.profile import (
    STATE_ACTIVE,
    STATE_EXPIRED,
    STATE_EXPIRING,
    STATE_NOT_ACTIVATED,
    STATE_SUSPENDED,
    STATE_UNLIMITED,
    load_profile_info,
    profile_callback,
    profile_command,
    profile_keyboard,
    profile_state,
    render_profile,
)
from app.handlers.start import start_command
from app.services.panel import ClientTraffic
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


# --- /profile dashboard (S1-1) ---------------------------------------------

NOW = 1_700_000_000_000  # 2023-11-14 22:13:20 UTC
HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
GIB = 1024**3


def info(
    *,
    expiry_ms: int = 0,
    enable: bool = True,
    sub_id: str = "abc",
    total: int = 0,
    up: int = 0,
    down: int = 0,
    last_online_ms: int | None = None,
) -> ClientTraffic:
    """Build a ``ClientTraffic`` snapshot for the render tests."""
    return ClientTraffic(
        email=str(USER),
        up=up,
        down=down,
        total=total,
        expiry_ms=expiry_ms,
        enable=enable,
        sub_id=sub_id,
        last_online_ms=last_online_ms,
    )


def render(snapshot: ClientTraffic) -> str:
    """Render a snapshot at :data:`NOW` and return just the card text."""
    text, _ = render_profile(
        snapshot,
        NOW,
        link="https://panel.example.com/sub/abc",
        timezone="UTC",
    )
    return text


def callback(
    data: str, *, message_id: int = 500, photo: object = None
) -> SimpleNamespace:
    """Minimal Telegram ``CallbackQuery``: only the fields the handler reads."""
    return SimpleNamespace(
        id=f"cb-{message_id}",
        data=data,
        from_user=SimpleNamespace(id=USER, username="neo", first_name="Neo"),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=99),
            message_id=message_id,
            photo=photo,
        ),
    )


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        (info(expiry_ms=NOW + 30 * DAY_MS), STATE_ACTIVE),
        (info(expiry_ms=NOW + 2 * DAY_MS), STATE_EXPIRING),
        # A disabled client with a future expiry is a suspension, not an expiry.
        (info(expiry_ms=NOW + 30 * DAY_MS, enable=False), STATE_SUSPENDED),
        (info(expiry_ms=NOW - 1), STATE_EXPIRED),
        (info(expiry_ms=NOW - 1, enable=False), STATE_EXPIRED),
        (info(expiry_ms=0), STATE_UNLIMITED),
        (info(expiry_ms=0, enable=False), STATE_NOT_ACTIVATED),
    ],
)
def test_profile_state_classification(snapshot: ClientTraffic, expected: str) -> None:
    """Every dashboard state is reachable from the snapshot alone (S1-1.9)."""
    assert profile_state(snapshot, NOW) == expected


def test_render_profile_active_card() -> None:
    """AC (active): status, expiry+timeleft, traffic bar and the link."""
    rendered = render(info(expiry_ms=NOW + 37 * DAY_MS, total=2 * GIB, up=GIB))

    assert texts.PROFILE_STATE_ACTIVE in rendered
    assert f"{texts.PROFILE_EXPIRES_LABEL} 21.12.2023 · ещё 37 дн." in rendered
    assert "▰▰▰▰▰▱▱▱▱▱" in rendered
    assert "1.0 ГБ / 2.0 ГБ" in rendered
    assert "<code>https://panel.example.com/sub/abc</code>" in rendered


def test_render_profile_expiring_card() -> None:
    """AC (expiring): an enabled client inside the 3-day window is 🟡."""
    rendered = render(info(expiry_ms=NOW + 2 * DAY_MS, total=2 * GIB, up=GIB))

    assert texts.PROFILE_STATE_EXPIRING in rendered
    assert texts.PROFILE_STATE_HINT not in rendered


def test_render_profile_expired_card() -> None:
    """AC (expired): 🔴 plus the «pay a tariff» hint and «истёк»."""
    rendered = render(info(expiry_ms=NOW - DAY_MS))

    assert texts.PROFILE_STATE_EXPIRED in rendered
    assert texts.PROFILE_STATE_HINT in rendered
    assert "истёк" in rendered


def test_render_profile_unlimited_card() -> None:
    """AC (unlimited): ``expiry == 0`` while enabled renders ∞, not 1970."""
    rendered = render(info(expiry_ms=0))

    assert texts.PROFILE_STATE_ACTIVE in rendered
    assert f"{texts.PROFILE_EXPIRES_LABEL} {texts.PROFILE_UNLIMITED}" in rendered
    assert "1970" not in rendered


def test_render_profile_without_a_traffic_limit_omits_the_bar() -> None:
    """AC (no traffic limit): ``total == 0`` drops the bar and shows ∞."""
    rendered = render(info(total=0, up=5 * GIB))

    assert "▰" not in rendered
    assert texts.PROFILE_UNLIMITED in rendered
    assert "5.0 ГБ" in rendered


def test_render_profile_suspended_card() -> None:
    """AC (suspended): a disabled client with a future expiry."""
    rendered = render(info(expiry_ms=NOW + 10 * DAY_MS, enable=False))

    assert texts.PROFILE_STATE_SUSPENDED in rendered
    assert texts.PROFILE_STATE_HINT in rendered


def test_render_profile_not_activated_card() -> None:
    """AC (never activated): disabled ``0`` is neither ∞ nor unlimited."""
    rendered = render(info(expiry_ms=0, enable=False))

    assert texts.PROFILE_STATE_NOT_ACTIVATED in rendered
    # The traffic line may still read "0 Б / ∞" (no quota); the *expiry* must not.
    assert f"{texts.PROFILE_EXPIRES_LABEL} {texts.PROFILE_UNLIMITED}" not in rendered
    assert "1970" not in rendered
    assert texts.PROFILE_STATE_HINT in rendered


@pytest.mark.parametrize(
    ("seen_ago_ms", "expected"),
    [
        (HOUR_MS, "сегодня в 21:13"),
        (25 * HOUR_MS, "вчера в 21:13"),
        (5 * DAY_MS, "09.11 в 22:13"),
    ],
)
def test_render_profile_activity_line(seen_ago_ms: int, expected: str) -> None:
    """``🕒`` renders today / yesterday / an absolute date (S1-1.9)."""
    rendered = render(info(last_online_ms=NOW - seen_ago_ms))

    assert f"{texts.PROFILE_ACTIVITY_LABEL} {expected}" in rendered


def test_render_profile_omits_activity_when_the_panel_has_none() -> None:
    """No last-online value → no ``🕒`` line at all."""
    assert texts.PROFILE_ACTIVITY_LABEL not in render(info())


def test_profile_keyboard_buttons_round_trip() -> None:
    """AC (S1-1.10): every button's payload parses back to ``ProfileNav``."""
    rows = profile_keyboard().keyboard

    assert [len(row) for row in rows] == [2, 2, 1]
    payloads = [button.callback_data for row in rows for button in row]
    assert payloads == ["prf:qr", "prf:instr", "prf:pay", "prf:newlink", "prf:support"]
    for payload in payloads:
        assert isinstance(unpack(payload), ProfileNav)


# --- /profile command + callbacks ------------------------------------------


async def test_profile_command_renders_the_dashboard(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """The command renders the card with the dashboard keyboard (S1-1.11)."""
    panel = handler_container.panel
    assert isinstance(panel, FakePanel)
    panel.seed(USER, expiry_ms=0, enable=True)

    await profile_command(message(USER), fake_bot, handler_container)

    (text,) = fake_bot.texts_to(99)
    assert texts.PROFILE_STATE_ACTIVE in text
    assert fake_bot.messages[-1][2].get("parse_mode") == "HTML"
    assert fake_bot.messages[-1][2].get("reply_markup") is not None


async def test_profile_command_outage_is_friendly_and_alerts(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """AC (B4): a panel outage never raises and never touches an undefined var."""
    panel = handler_container.panel
    assert isinstance(panel, FakePanel)
    panel.unavailable = True

    await profile_command(message(USER), fake_bot, handler_container)

    assert fake_bot.texts_to(99) == [texts.ERROR_PANEL]
    assert len(fake_bot.texts_to(OWNER)) == 1


async def test_profile_command_without_client_points_at_start(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """A user who never ran ``/start`` is told to run it."""
    await profile_command(message(USER), fake_bot, handler_container)

    assert fake_bot.texts_to(99) == [texts.PROFILE_NO_ACCOUNT]


async def test_load_profile_info_distinguishes_outage_from_no_client(
    handler_container: Container,
) -> None:
    """AC (S1-1.8): "no client" is ``(None, None)``; an outage carries a text."""
    panel = handler_container.panel
    assert isinstance(panel, FakePanel)

    missing, error = await load_profile_info(handler_container, USER)
    assert missing is None
    assert error is None

    panel.seed(USER, expiry_ms=0, enable=True)
    found, error = await load_profile_info(handler_container, USER)
    assert found is not None
    assert error is None

    panel.unavailable = True
    broken, error = await load_profile_info(handler_container, USER)
    assert broken is None
    assert error == texts.ERROR_PANEL


async def test_profile_callback_edits_the_same_message(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """AC (S1-1.12): ``prf:profile`` edits in place and answers the query."""
    panel = handler_container.panel
    assert isinstance(panel, FakePanel)
    panel.seed(USER, expiry_ms=0, enable=True)

    await profile_callback(callback("prf:profile"), fake_bot, handler_container)

    (chat_id, message_id, text, _) = fake_bot.edits[-1]
    assert (chat_id, message_id) == (99, 500)
    assert texts.PROFILE_STATE_ACTIVE in text
    assert fake_bot.sent == []  # edited, never a duplicate message
    assert fake_bot.callback_answers[-1][0] == "cb-500"


async def test_profile_callback_removes_a_qr_photo_first(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """A photo (QR) card is deleted before the text card replaces it (S1-1.5)."""
    panel = handler_container.panel
    assert isinstance(panel, FakePanel)
    panel.seed(USER, expiry_ms=0, enable=True)

    await profile_callback(
        callback("prf:profile", photo=[{"file_id": "x"}]),
        fake_bot,
        handler_container,
    )

    assert fake_bot.deleted == [(99, 500)]
    assert fake_bot.edits or fake_bot.sent
    assert fake_bot.callback_answers[-1][0] == "cb-500"


@pytest.mark.parametrize("data", ["prf:nope", "prf:qr", "pay:sel:1", "bogus"])
async def test_profile_callback_answers_a_crafted_payload(
    handler_container: Container, fake_bot: FakeBot, data: str
) -> None:
    """AC: a crafted payload never raises and is always answered (B5)."""
    await profile_callback(callback(data), fake_bot, handler_container)

    assert fake_bot.callback_answers == [("cb-500", texts.ERROR_STALE_BUTTON, False)]
    assert fake_bot.edits == []


async def test_profile_callback_handles_a_missing_client(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """A callback from a user without a client edits in the /start hint."""
    await profile_callback(callback("prf:profile"), fake_bot, handler_container)

    assert fake_bot.edits[-1][2] == texts.PROFILE_NO_ACCOUNT
    assert fake_bot.callback_answers[-1][0] == "cb-500"
