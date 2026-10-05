"""``/start`` and ``/profile`` handler tests (``TASK_PLAN.md`` §M0-09.7).

Covers the two acceptance criteria from §M0-09 plus the defects they fix:

* a panel outage during ``/start`` or ``/profile`` yields a friendly reply, no
  exception, and a staff alert (B7);
* ``/start`` is what creates the ``users`` row, with ``status='approved'``;
* ``expiry == 0`` renders as "Бессрочно" (B4), and a user without a panel client
  is pointed at ``/start``.
"""

from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import texts
from app.callbacks import Confirm, ProfileNav, unpack
from app.container import Container
from app.db.models import AuditLog, Tariff, UserStatus
from app.db.repositories import users as users_repo
from app.handlers.confirm import confirm_callback
from app.handlers.profile import (
    ACTION_NEWLINK,
    STATE_ACTIVE,
    STATE_EXPIRED,
    STATE_EXPIRING,
    STATE_NOT_ACTIVATED,
    STATE_SUSPENDED,
    STATE_UNLIMITED,
    _last_regen,
    _show_instructions,
    load_profile_info,
    profile_callback,
    profile_command,
    profile_keyboard,
    profile_state,
    render_profile,
)
from app.handlers.start import help_command, start_command
from app.services.panel import ClientTraffic
from app.states import UserStates
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


async def open_access(container: Container) -> None:
    """Put the container in ``open`` mode so a new ``/start`` auto-approves."""
    settings = container.settings_service
    assert settings is not None
    await settings.set_access_mode("open")


# --- /start ----------------------------------------------------------------


async def test_start_creates_approved_row_and_panel_client(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC: the row is created here, with ``approved``, plus a panel client."""
    await open_access(handler_container)
    panel = handler_container.panel
    assert isinstance(panel, FakePanel)

    await start_command(message(USER), fake_bot, handler_container)

    assert await get_status(session_factory, USER) == UserStatus.APPROVED
    assert await panel.get_client(USER) is not None
    assert fake_bot.texts_to(99) == [f"{texts.START_WELCOME}\n\n{texts.START_MENU}"]


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

    assert fake_bot.texts_to(99) == [f"{texts.START_RETURNING}\n\n{texts.START_MENU}"]
    assert "ensure_client" not in panel.calls


async def test_start_panel_outage_replies_and_alerts_owners(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (B7): outage → friendly reply + staff alert, no exception."""
    await open_access(handler_container)
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
    await open_access(handler_container)
    await start_command(message(USER), fake_bot, handler_container)

    assert await get_status(session_factory, USER) == UserStatus.APPROVED


# --- /start access modes (§S3-1) --------------------------------------------


async def test_start_approval_mode_parks_a_request_without_a_client(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (§S3-1): ``approval`` keeps the user ``pending`` and builds no client."""
    panel = handler_container.panel
    assert isinstance(panel, FakePanel)

    await start_command(message(USER), fake_bot, handler_container)

    assert await get_status(session_factory, USER) == UserStatus.PENDING
    assert "ensure_client" not in panel.calls
    assert fake_bot.texts_to(99) == [texts.ACCESS_PENDING]


async def test_start_approval_mode_repeat_start_keeps_the_status(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A repeat ``/start`` while ``pending`` must not reset the row."""
    await start_command(message(USER), fake_bot, handler_container)
    await start_command(message(USER), fake_bot, handler_container)

    assert await get_status(session_factory, USER) == UserStatus.PENDING
    assert fake_bot.texts_to(99) == [texts.ACCESS_PENDING, texts.ACCESS_PENDING]


async def test_start_invite_only_mode_writes_no_row(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (§S3-1): ``invite_only`` refuses a stranger and writes no row."""
    settings = handler_container.settings_service
    assert settings is not None
    await settings.set_access_mode("invite_only")
    panel = handler_container.panel
    assert isinstance(panel, FakePanel)

    await start_command(message(USER), fake_bot, handler_container)

    assert await get_status(session_factory, USER) is None
    assert fake_bot.texts_to(99) == [texts.ACCESS_INVITE_ONLY]
    assert "ensure_client" not in panel.calls


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


@pytest.mark.parametrize(
    "data", ["prf:nope", "prf:instr_bsd", "prf:vpn", "pay:sel:1", "bogus"]
)
async def test_profile_callback_answers_a_crafted_payload(
    handler_container: Container, fake_bot: FakeBot, data: str
) -> None:
    """AC: a crafted payload never raises and is always answered (B5).

    ``prf:instr_bsd`` is the unknown-platform case of §S1-3.3: the section is not
    in :class:`~app.callbacks.ProfileNav`, so the same stale toast comes back.
    """
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


# --- QR, instruction and link screens (S1-2, S1-3) --------------------------

SUB_URL = "https://panel.example.com/sub/abc"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def seeded(container: Container) -> None:
    """Seed ``USER`` with the fixed ``sub_id`` these screens render."""
    panel = container.panel
    assert isinstance(panel, FakePanel)
    panel.seed(USER, expiry_ms=0, enable=True, sub_id="abc")


def payloads_of(markup: Any) -> list[str]:
    """Return the callback payloads of an inline keyboard, row by row."""
    return [button.callback_data for row in markup.keyboard for button in row]


async def test_qr_screen_uploads_an_in_memory_png(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """AC (S1-2.2): ``prf:qr`` sends a ``BytesIO``; nothing is written to disk."""
    seeded(handler_container)

    await profile_callback(
        callback("prf:qr", message_id=501), fake_bot, handler_container
    )

    (chat_id, kind, photo, kwargs) = fake_bot.media[-1]
    assert (chat_id, kind) == (99, "photo")
    assert isinstance(photo, BytesIO)
    assert photo.read(8) == PNG_SIGNATURE
    assert SUB_URL in str(kwargs["caption"])
    assert kwargs["parse_mode"] == "HTML"
    assert payloads_of(kwargs["reply_markup"]) == ["prf:instr", "prf:profile"]
    assert fake_bot.callback_answers[-1][0] == "cb-501"


async def test_qr_screen_replaces_a_previous_qr_photo(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """Re-pressing 📱 from a QR photo deletes it instead of stacking photos."""
    seeded(handler_container)

    await profile_callback(
        callback("prf:qr", message_id=602, photo=[{"file_id": "x"}]),
        fake_bot,
        handler_container,
    )

    assert fake_bot.deleted == [(99, 602)]
    assert fake_bot.edits == []


async def test_instr_picker_lists_every_platform_without_a_url(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """AC (S1-3.2, S1-3.5): the picker edits in place and shows no link."""
    await profile_callback(
        callback("prf:instr", message_id=501), fake_bot, handler_container
    )

    (chat_id, message_id, text, kwargs) = fake_bot.edits[-1]
    assert (chat_id, message_id) == (99, 501)
    assert text == texts.INSTR_PICKER
    assert "http" not in text
    assert payloads_of(kwargs["reply_markup"]) == [
        "prf:instr_android",
        "prf:instr_ios",
        "prf:instr_windows",
        "prf:instr_macos",
        "prf:profile",
    ]
    assert fake_bot.sent == []


@pytest.mark.parametrize("platform", texts.INSTRUCTION_PLATFORMS)
async def test_instruction_screen_uses_the_callers_own_link(
    handler_container: Container, fake_bot: FakeBot, platform: str
) -> None:
    """AC (S1-3.3): every platform renders the caller's URL, never a literal one."""
    seeded(handler_container)

    await profile_callback(
        callback(f"prf:instr_{platform}", message_id=501), fake_bot, handler_container
    )

    (chat_id, message_id, text, kwargs) = fake_bot.edits[-1]
    assert (chat_id, message_id) == (99, 501)
    assert f"<code>{SUB_URL}</code>" in text
    assert "{link}" not in text
    assert "<b>Hiddify</b>" in text
    # ⬅️ Назад returns to the picker, not to the dashboard (S1-3.3).
    assert payloads_of(kwargs["reply_markup"]) == ["prf:qr", "prf:link", "prf:instr"]
    assert fake_bot.sent == []


async def test_instruction_navigation_round_trip(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """AC (S1-3.5): dashboard → picker → Android → ⬅️ Назад → picker again."""
    seeded(handler_container)

    await profile_callback(
        callback("prf:instr", message_id=501), fake_bot, handler_container
    )
    await profile_callback(
        callback("prf:instr_android", message_id=501), fake_bot, handler_container
    )
    back = payloads_of(fake_bot.edits[-1][3]["reply_markup"])[-1]
    await profile_callback(callback(back, message_id=501), fake_bot, handler_container)

    assert back == "prf:instr"
    assert fake_bot.edits[-1][2] == texts.INSTR_PICKER


async def test_link_screen_shows_the_callers_own_url(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """AC (S1-3.4): ``prf:link`` edits the same message; back goes to the card."""
    seeded(handler_container)

    await profile_callback(
        callback("prf:link", message_id=501), fake_bot, handler_container
    )

    (chat_id, message_id, text, kwargs) = fake_bot.edits[-1]
    assert (chat_id, message_id) == (99, 501)
    assert f"<code>{SUB_URL}</code>" in text
    assert payloads_of(kwargs["reply_markup"]) == ["prf:profile"]
    assert fake_bot.sent == []


async def test_instruction_screen_without_a_client_points_at_start(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """A screen reached by a user without a client edits in the ``/start`` hint."""
    await profile_callback(
        callback("prf:instr_ios", message_id=501), fake_bot, handler_container
    )

    assert fake_bot.edits[-1][2] == texts.PROFILE_NO_ACCOUNT
    assert fake_bot.media == []


async def test_show_instructions_rejects_an_unknown_platform(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """AC (S1-3.3): a platform outside the catalogue is a stale toast, not a crash."""
    await _show_instructions(
        callback("prf:instr_bsd"), fake_bot, handler_container, platform="bsd"
    )

    assert fake_bot.callback_answers == [("cb-500", texts.ERROR_STALE_BUTTON, False)]
    assert fake_bot.edits == []


# --- main menu + /help (S1-4) -----------------------------------------------

#: The main menu rows: profile/pay then instr/support (§S1-4.5).
MENU_PAYLOADS = ["prf:profile", "prf:pay", "prf:instr", "prf:support"]


async def seed_tariff(factory: async_sessionmaker[AsyncSession]) -> Tariff:
    """Insert one active tariff (the 💳 flow needs a list to render)."""
    async with factory() as session:
        tariff = Tariff(
            name="30 дней — 150 ₽", days=30, price=150, is_active=True, sort_order=1
        )
        session.add(tariff)
        await session.commit()
        await session.refresh(tariff)
        return tariff


def menu_of(fake_bot: FakeBot) -> list[str]:
    """Return the payloads of the keyboard on the last message the bot sent."""
    (_, _, kwargs) = fake_bot.messages[-1]
    return payloads_of(kwargs["reply_markup"])


async def test_start_shows_the_main_menu(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """AC (S1-4.5): the welcome keeps its copy and gains the inline menu."""
    await open_access(handler_container)
    await start_command(message(USER), fake_bot, handler_container)

    (_, text, kwargs) = fake_bot.messages[-1]
    assert text.startswith(texts.START_WELCOME)
    assert text.endswith(texts.START_MENU)
    assert kwargs["parse_mode"] == "HTML"
    assert payloads_of(kwargs["reply_markup"]) == MENU_PAYLOADS


async def test_start_keeps_the_returning_copy_and_the_menu(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (S1-4.5): "с возвращением" is preserved, the menu is added to it."""
    seeded(handler_container)
    await seed_user(session_factory, USER)

    await start_command(message(USER), fake_bot, handler_container)

    assert fake_bot.texts_to(99) == [f"{texts.START_RETURNING}\n\n{texts.START_MENU}"]
    assert menu_of(fake_bot) == MENU_PAYLOADS


async def test_help_shows_the_same_menu_as_start(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """AC (S1-4.6): ``/help`` shows the reference and the identical keyboard."""
    await open_access(handler_container)
    await start_command(message(USER), fake_bot, handler_container)
    start_menu = payloads_of(fake_bot.messages[-1][2]["reply_markup"])

    await help_command(message(USER), fake_bot, handler_container)

    (_, text, kwargs) = fake_bot.messages[-1]
    assert text == texts.HELP_TEXT
    assert "/support" in text
    assert payloads_of(kwargs["reply_markup"]) == start_menu == MENU_PAYLOADS


async def test_menu_buttons_reach_their_flows(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (S1-4.7): every menu button opens the same flow as its command."""
    seeded(handler_container)
    await seed_user(session_factory, USER)
    tariff = await seed_tariff(session_factory)

    await start_command(message(USER), fake_bot, handler_container)
    (profile, pay, instr, support) = menu_of(fake_bot)

    # 👤 Профиль → the dashboard card (S1-1).
    await profile_callback(
        callback(profile, message_id=501), fake_bot, handler_container
    )
    assert fake_bot.edits[-1][:2] == (99, 501)
    assert texts.PROFILE_TITLE in fake_bot.edits[-1][2]

    # 💳 Оплата → the tariff list ``/pay`` renders, in the dashboard's message.
    await profile_callback(callback(pay, message_id=501), fake_bot, handler_container)
    (chat_id, message_id, text, kwargs) = fake_bot.edits[-1]
    assert (chat_id, message_id, text) == (99, 501, texts.PAYMENT_CHOOSE_TARIFF)
    assert payloads_of(kwargs["reply_markup"]) == [f"pay:sel:{tariff.id}"]

    # 📖 Инструкция → the platform picker (S1-3).
    await profile_callback(callback(instr, message_id=501), fake_bot, handler_container)
    assert fake_bot.edits[-1][2] == texts.INSTR_PICKER

    # 🆘 Поддержка → the ``/support`` state, so the next message reaches staff.
    await profile_callback(
        callback(support, message_id=501), fake_bot, handler_container
    )
    assert fake_bot.edits[-1][2] == texts.SUPPORT_ENTERED
    assert await fake_bot.get_state(USER, 99) == UserStates.waiting_for_help.name

    # Every button answered its own callback (no spinner left running).
    assert [answer[0] for answer in fake_bot.callback_answers] == [
        "cb-501",
        "cb-501",
        "cb-501",
        "cb-501",
    ]


async def test_menu_pay_button_refuses_a_pending_user(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The button keeps the ``/pay`` guard: a pending account is refused."""
    seeded(handler_container)
    await seed_user(session_factory, USER, status=UserStatus.PENDING)

    await profile_callback(
        callback("prf:pay", message_id=501), fake_bot, handler_container
    )

    assert fake_bot.edits[-1][2] == texts.PAYMENT_NOT_APPROVED
    assert fake_bot.callback_answers[-1] == ("cb-501", None, False)


# --- link regeneration (S1-5) ----------------------------------------------

OTHER = 777


@pytest.fixture(autouse=True)
def _reset_regen_cooldown() -> Any:
    """Isolate the module-level regeneration cooldown between tests (§S1-5.4)."""
    _last_regen.clear()
    yield
    _last_regen.clear()


async def audit_rows(
    factory: async_sessionmaker[AsyncSession],
) -> list[tuple[str, str | None]]:
    """Return ``(action, target_id)`` for every audit row."""
    async with factory() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
    return [(row.action, row.target_id) for row in rows]


def _payload_of(fake_bot: FakeBot, *, cancel: bool) -> str:
    """Return the confirm / cancel ``cf:`` payload of the card last shown."""
    for payload in payloads_of(fake_bot.edits[-1][3]["reply_markup"]):
        parsed = unpack(payload)
        assert isinstance(parsed, Confirm)
        if parsed.cancel == cancel:
            return payload
    raise AssertionError("expected button missing from the confirmation card")


def _token_of(fake_bot: FakeBot, *, cancel: bool) -> str:
    """Return the token behind the confirm / cancel button of the last card."""
    parsed = unpack(_payload_of(fake_bot, cancel=cancel))
    assert isinstance(parsed, Confirm)
    return parsed.token


async def test_newlink_shows_a_confirmation_card(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """AC (S1-5.2): «🔄 Новая ссылка» asks first — nothing is written yet."""
    seeded(handler_container)
    panel = handler_container.panel
    assert isinstance(panel, FakePanel)

    await profile_callback(
        callback("prf:newlink", message_id=501), fake_bot, handler_container
    )

    (chat_id, message_id, text, kwargs) = fake_bot.edits[-1]
    assert (chat_id, message_id, text) == (99, 501, texts.NEWLINK_CONFIRM)
    confirm, cancel = (
        _token_of(fake_bot, cancel=False),
        _token_of(fake_bot, cancel=True),
    )
    assert confirm == cancel  # one token, two buttons
    assert "regenerate" not in " ".join(panel.calls)  # no panel write yet
    assert fake_bot.callback_answers[-1] == ("cb-501", None, False)


async def test_newlink_cancel_keeps_the_old_link(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """AC (S1-5.2): «❌ Отмена» drops the token and touches nothing."""
    seeded(handler_container)
    panel = handler_container.panel
    assert isinstance(panel, FakePanel)

    await profile_callback(
        callback("prf:newlink", message_id=501), fake_bot, handler_container
    )
    cancel = _payload_of(fake_bot, cancel=True)
    await confirm_callback(
        callback(cancel, message_id=501), fake_bot, handler_container
    )

    assert fake_bot.edits[-1][2] == texts.CONFIRM_CANCELLED
    assert fake_bot.callback_answers[-1][1] == texts.CALLBACK_CANCELLED
    client = await panel.get_client(USER)
    assert client is not None and client.sub_id == "abc"


async def test_newlink_confirm_regenerates_and_rewrites_the_card(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (S1-5.0/5.3): the old ``sub_id`` dies and the card is re-rendered."""
    seeded(handler_container)
    panel = handler_container.panel
    assert isinstance(panel, FakePanel)
    before = await panel.get_client(USER)
    assert before is not None and before.sub_id == "abc"

    await profile_callback(
        callback("prf:newlink", message_id=501), fake_bot, handler_container
    )
    confirm = _payload_of(fake_bot, cancel=False)
    await confirm_callback(
        callback(confirm, message_id=501), fake_bot, handler_container
    )

    fresh = await panel.get_client(USER)
    assert fresh is not None and fresh.sub_id and fresh.sub_id != "abc"
    (chat_id, message_id, text, kwargs) = fake_bot.edits[-1]
    assert (chat_id, message_id) == (99, 501)
    assert texts.PROFILE_TITLE in text
    assert fresh.sub_id in text  # the dashboard carries the *new* link
    assert texts.BUTTON_PROFILE_QR in [
        button.text for row in kwargs["reply_markup"].keyboard for button in row
    ]
    assert ("user.sub_regenerate", str(USER)) in await audit_rows(session_factory)


async def test_newlink_is_rate_limited_for_ten_minutes(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """AC (S1-5.4): a second request < 10 min is refused, gateway untouched."""
    seeded(handler_container)
    panel = handler_container.panel
    assert isinstance(panel, FakePanel)

    await profile_callback(
        callback("prf:newlink", message_id=501), fake_bot, handler_container
    )
    confirm = _payload_of(fake_bot, cancel=False)
    await confirm_callback(
        callback(confirm, message_id=501), fake_bot, handler_container
    )

    edits, mutates = len(fake_bot.edits), panel.calls.count("mutate")
    await profile_callback(
        callback("prf:newlink", message_id=502), fake_bot, handler_container
    )

    assert len(fake_bot.edits) == edits  # no second card
    assert panel.calls.count("mutate") == mutates  # no gateway write
    assert fake_bot.callback_answers[-1][1] == texts.NEWLINK_RATE_LIMITED
    assert fake_bot.callback_answers[-1][2] is True
    assert USER in _last_regen


async def test_newlink_failure_keeps_the_old_link(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (S1-5.3/4): a failure keeps the old link and does not lock the user."""
    seeded(handler_container)
    panel = handler_container.panel
    assert isinstance(panel, FakePanel)
    panel.drop_writes = True  # the write is dropped -> PanelError

    await profile_callback(
        callback("prf:newlink", message_id=501), fake_bot, handler_container
    )
    confirm = _payload_of(fake_bot, cancel=False)
    await confirm_callback(
        callback(confirm, message_id=501), fake_bot, handler_container
    )

    assert fake_bot.edits[-1][2] == texts.NEWLINK_FAILED
    client = await panel.get_client(USER)
    assert client is not None and client.sub_id == "abc"
    assert USER not in _last_regen  # a failed attempt starts no cooldown
    assert ("user.sub_regenerate", str(USER)) not in await audit_rows(session_factory)


async def test_newlink_token_is_bound_to_its_owner(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """AC (S1-5.2): a wrong user cannot consume the owner's token."""
    seeded(handler_container)
    panel = handler_container.panel
    assert isinstance(panel, FakePanel)

    await profile_callback(
        callback("prf:newlink", message_id=501), fake_bot, handler_container
    )
    confirm = _payload_of(fake_bot, cancel=False)
    token = _token_of(fake_bot, cancel=False)

    intruder = callback(confirm, message_id=501)
    intruder.from_user = SimpleNamespace(
        id=OTHER, username="trinity", first_name="Trinity"
    )
    await confirm_callback(intruder, fake_bot, handler_container)

    assert fake_bot.callback_answers[-1][1] == texts.ERROR_CONFIRM_EXPIRED
    client = await panel.get_client(USER)
    assert client is not None and client.sub_id == "abc"
    # The owner's token survived, and asking again mints a *fresh* card.
    assert handler_container.confirmations.peek(token, USER) == ACTION_NEWLINK
    await profile_callback(
        callback("prf:newlink", message_id=503), fake_bot, handler_container
    )
    assert _token_of(fake_bot, cancel=False) != token
