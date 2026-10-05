"""Admin «Пользователи» list/filters/search tests (§S2-2).

Covers the S2-2 acceptance criteria: the pure ``status_icon``/``filter_rows``
helpers select the right subsets (including the exact 3-day boundary), the page
renderer produces the expected row buttons and navigation, and the search prompt
finds a user by id and by ``@username``, replies when there is no hit, and the
«✖️ Отмена» button returns to the list with the FSM state cleared.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import texts
from app.callbacks import unpack
from app.container import Container
from app.db.models import UserStatus
from app.db.repositories import users as users_repo
from app.handlers.admin import users as users_screen
from app.handlers.admin.nav import admin_callback
from app.states import UserStates
from app.utils.pagination import paginate
from tests.fakes import FakeBot

OWNER = 1
CHAT = 99
DAY_MS = 24 * 60 * 60 * 1000
#: The screen reads the wall clock, so relative offsets are anchored to "now".
NOW = int(time.time() * 1000)


def message(tg_id: int, text: str) -> SimpleNamespace:
    """Minimal Telegram message for the search prompt reply."""
    return SimpleNamespace(
        from_user=SimpleNamespace(id=tg_id, username="neo", first_name="Neo"),
        chat=SimpleNamespace(id=CHAT),
        text=text,
    )


def callback(
    data: str, *, tg_id: int = OWNER, message_id: int = 500
) -> SimpleNamespace:
    """Minimal Telegram ``CallbackQuery``: only the fields the handlers read."""
    return SimpleNamespace(
        id=f"cb-{message_id}",
        data=data,
        from_user=SimpleNamespace(id=tg_id, username="neo", first_name="Neo"),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=CHAT), message_id=message_id, photo=None
        ),
    )


def payloads_of(markup: Any) -> list[str]:
    """Return the callback payloads of an inline keyboard, row by row."""
    return [button.callback_data for row in markup.keyboard for button in row]


def labels_of(markup: Any) -> list[str]:
    """Return the button labels of an inline keyboard, row by row."""
    return [button.text for row in markup.keyboard for button in row]


def row(
    tg_id: int,
    *,
    status: UserStatus = UserStatus.APPROVED,
    expiry_ms: int | None = None,
    enable: bool | None = None,
    username: str | None = None,
) -> users_screen.UserRow:
    """Build a :class:`UserRow` directly (no DB/panel needed)."""
    return users_screen.UserRow(
        tg_id=tg_id,
        username=username,
        first_name=None,
        status=str(status),
        expiry_ms=expiry_ms,
        enable=enable,
    )


async def seed_user(
    factory: async_sessionmaker[AsyncSession],
    tg_id: int,
    *,
    username: str = "neo",
    status: UserStatus = UserStatus.APPROVED,
) -> None:
    """Insert a ``users`` row directly."""
    async with factory() as session:
        await users_repo.upsert_from_telegram(session, tg_id, username=username)
        await users_repo.set_status(session, tg_id, status)
        await session.commit()


# --- admin_search state (§S2-2.7) --------------------------------------------


async def test_admin_search_state_round_trips(fake_bot: FakeBot) -> None:
    await fake_bot.set_state(OWNER, UserStates.admin_search, CHAT)
    assert await fake_bot.get_state(OWNER, CHAT) == UserStates.admin_search.name

    await fake_bot.delete_state(OWNER, CHAT)
    assert await fake_bot.get_state(OWNER, CHAT) is None


# --- status_icon (§S2-2.3) ---------------------------------------------------


def test_status_icon_blocked_and_pending() -> None:
    assert users_screen.status_icon(row(1, status=UserStatus.BLOCKED), NOW) == "⛔"
    assert users_screen.status_icon(row(1, status=UserStatus.PENDING), NOW) == "⏳"
    # ``new``/``rejected`` are neither blocked nor pending → inactive.
    assert users_screen.status_icon(row(1, status=UserStatus.NEW), NOW) == "🔴"
    assert users_screen.status_icon(row(1, status=UserStatus.REJECTED), NOW) == "🔴"


def test_status_icon_approved_branches_and_boundary() -> None:
    # No panel client, a disabled client and an expired client are all 🔴.
    assert users_screen.status_icon(row(1), NOW) == "🔴"
    assert (
        users_screen.status_icon(row(1, expiry_ms=NOW + 10 * DAY_MS, enable=False), NOW)
        == "🔴"
    )
    assert users_screen.status_icon(row(1, expiry_ms=NOW, enable=True), NOW) == "🔴"
    assert (
        users_screen.status_icon(row(1, expiry_ms=NOW - DAY_MS, enable=True), NOW)
        == "🔴"
    )
    # Unlimited enabled and a far expiry are 🟢.
    assert users_screen.status_icon(row(1, expiry_ms=0, enable=True), NOW) == "🟢"
    assert (
        users_screen.status_icon(row(1, expiry_ms=NOW + 10 * DAY_MS, enable=True), NOW)
        == "🟢"
    )
    # Exactly three days left is already 🟡 (the boundary is strict ``>``).
    assert (
        users_screen.status_icon(row(1, expiry_ms=NOW + 3 * DAY_MS, enable=True), NOW)
        == "🟡"
    )
    assert (
        users_screen.status_icon(row(1, expiry_ms=NOW + DAY_MS, enable=True), NOW)
        == "🟡"
    )


# --- filter_rows (§S2-2.4) ---------------------------------------------------


def test_filter_rows_subsets() -> None:
    rows = [
        row(1, status=UserStatus.BLOCKED),
        row(2, status=UserStatus.PENDING),
        row(3, expiry_ms=NOW + 10 * DAY_MS, enable=True),  # active
        row(4, expiry_ms=NOW + 2 * DAY_MS, enable=True),  # soon
        row(5, expiry_ms=NOW - DAY_MS, enable=True),  # inactive
        row(6),  # no client → inactive
    ]
    ids = lambda subset: [item.tg_id for item in subset]  # noqa: E731

    assert ids(users_screen.filter_rows(rows, "all", NOW)) == [1, 2, 3, 4, 5, 6]
    assert ids(users_screen.filter_rows(rows, "active", NOW)) == [3]
    assert ids(users_screen.filter_rows(rows, "soon", NOW)) == [4]
    # ``blocked`` is the 🔴 group (expired/disabled), not the ⛔ bans.
    assert ids(users_screen.filter_rows(rows, "blocked", NOW)) == [5, 6]
    # An unknown key falls back to ``all``.
    assert ids(users_screen.filter_rows(rows, "bogus", NOW)) == [1, 2, 3, 4, 5, 6]


# --- render_users_page (§S2-2.5) ---------------------------------------------


def test_render_users_page_rows_and_buttons_parse() -> None:
    rows = [
        row(101, expiry_ms=NOW + 12 * DAY_MS, enable=True, username="neo"),
        row(102, status=UserStatus.BLOCKED, username="trinity"),
    ]
    text, markup = users_screen.render_users_page(rows, paginate(rows, 0), now_ms=NOW)

    labels = labels_of(markup)
    assert "🟢 @neo · ещё 12 дн." in labels
    assert "⛔ @trinity · —" in labels

    payloads = payloads_of(markup)
    assert "adm:users:card:101" in payloads
    assert "adm:users:card:102" in payloads
    assert "adm:users:f:all" in payloads
    assert "adm:users:f:soon" in payloads
    assert "adm:users:s" in payloads
    assert "Пользователи" in text

    # Every button payload is a well-formed ``AdminNav``.
    for payload in payloads:
        unpack(payload)


def test_render_users_page_paginates_and_marks_the_filter() -> None:
    rows = [
        row(i, expiry_ms=NOW + 10 * DAY_MS, enable=True, username=f"u{i}")
        for i in range(1, 11)
    ]
    page = paginate(rows, 1)
    assert page.count == 2

    text, markup = users_screen.render_users_page(
        rows, page, filter_key="soon", now_ms=NOW
    )

    assert "стр. 2/2" in text
    assert "• 🟡 скоро" in labels_of(markup)
    assert "• Все" not in labels_of(markup)
    payloads = payloads_of(markup)
    assert [p for p in payloads if p.startswith("adm:users:card:")] == [
        "adm:users:card:9",
        "adm:users:card:10",
    ]
    # Last page: a «prev» button but no «next».
    assert "adm:users:p:0" in payloads
    assert "adm:users:p:2" not in payloads


def test_render_users_page_empty_list_has_filters_only() -> None:
    text, markup = users_screen.render_users_page([], paginate([], 0), now_ms=NOW)

    assert "Никого не найдено." in text
    assert [p for p in payloads_of(markup) if p.startswith("adm:users:card:")] == []
    assert "adm:users:f:all" in payloads_of(markup)
    assert "adm:users:s" in payloads_of(markup)


# --- list screen dispatch (§S2-2.6) ------------------------------------------


async def test_admin_users_screen_renders_and_filters_in_place(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    handler_container.panel.seed(  # type: ignore[union-attr]
        101, expiry_ms=NOW + 10 * DAY_MS, enable=True
    )
    await seed_user(session_factory, 101, username="neo")
    await seed_user(session_factory, 202, username="trinity")  # no client → 🔴

    await admin_callback(callback("adm:users"), fake_bot, handler_container)
    assert "Пользователи" in fake_bot.edits[-1][2]
    assert "adm:users:f:blocked" in payloads_of(fake_bot.edits[-1][3]["reply_markup"])

    await admin_callback(
        callback("adm:users:f:blocked", message_id=502), fake_bot, handler_container
    )
    payloads = payloads_of(fake_bot.edits[-1][3]["reply_markup"])
    assert "adm:users:card:202" in payloads
    assert "adm:users:card:101" not in payloads
    assert fake_bot.callback_answers[-1] == ("cb-502", None, False)


# --- search (§S2-2.7/§S2-2.8) ------------------------------------------------


async def test_admin_search_by_id_opens_the_matching_row(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await seed_user(session_factory, 101, username="neo")
    await seed_user(session_factory, 202, username="trinity")

    await users_screen.admin_search_message(
        message(OWNER, "101"), fake_bot, handler_container
    )

    assert len(fake_bot.messages) == 1
    payloads = payloads_of(fake_bot.messages[-1][2]["reply_markup"])
    assert "adm:users:card:101" in payloads
    assert "adm:users:card:202" not in payloads


async def test_admin_search_by_username_is_case_insensitive(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await seed_user(session_factory, 101, username="neo")
    await seed_user(session_factory, 202, username="trinity")

    await users_screen.admin_search_message(
        message(OWNER, "@TRINITY"), fake_bot, handler_container
    )

    payloads = payloads_of(fake_bot.messages[-1][2]["reply_markup"])
    assert "adm:users:card:202" in payloads
    assert "adm:users:card:101" not in payloads


async def test_admin_search_miss_replies_not_found_and_clears_state(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await seed_user(session_factory, 101, username="neo")
    await fake_bot.set_state(OWNER, "admin_search", CHAT)

    await users_screen.admin_search_message(
        message(OWNER, "nobody"), fake_bot, handler_container
    )

    assert fake_bot.messages[-1][1] == texts.ERROR_UNKNOWN_USER
    assert await fake_bot.get_state(OWNER, CHAT) is None


async def test_admin_search_run_clears_state(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await seed_user(session_factory, 101, username="neo")
    await fake_bot.set_state(OWNER, "admin_search", CHAT)

    await users_screen.admin_search_message(
        message(OWNER, "101"), fake_bot, handler_container
    )

    assert await fake_bot.get_state(OWNER, CHAT) is None


async def test_admin_search_prompt_carries_a_cancel_that_returns_to_the_list(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await seed_user(session_factory, 101, username="neo")

    await admin_callback(callback("adm:users:s"), fake_bot, handler_container)
    assert await fake_bot.get_state(OWNER, CHAT) == UserStates.admin_search.name
    assert "adm:users" in payloads_of(fake_bot.edits[-1][3]["reply_markup"])

    # The cancel button (``adm:users``) restores the list and clears the state.
    await admin_callback(
        callback("adm:users", message_id=501), fake_bot, handler_container
    )
    assert await fake_bot.get_state(OWNER, CHAT) is None
    assert "Пользователи" in fake_bot.edits[-1][2]
    assert "adm:users:card:101" in payloads_of(fake_bot.edits[-1][3]["reply_markup"])


async def test_admin_search_ignores_non_staff(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A non-staff reply to the prompt is dropped silently (``@require``)."""
    await seed_user(session_factory, 101, username="neo")

    await users_screen.admin_search_message(
        message(777, "101"), fake_bot, handler_container
    )

    assert fake_bot.messages == []
