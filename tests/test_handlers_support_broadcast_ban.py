"""Support / broadcast / ban handler tests (``TASK_PLAN.md`` §M0-09.7).

Acceptance criteria covered:

* HTML in support text is delivered safely (escaped, B9) and fanned out with
  ``staff_with(support.reply)``;
* a broadcast sends **exactly one** summary (B3) and escapes its text;
* a ban disables the panel client, unban re-enables it only while the
  subscription is still in the future, and staff can never be banned;
* every admin action leaves an audit row.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import texts
from app.container import Container
from app.db.models import Admin, AuditLog, UserStatus
from app.db.repositories import users as users_repo
from app.handlers import broadcast
from app.handlers.admin.nav import admin_callback
from app.handlers.ban import ban_command, banned_list_command, unban_command
from app.handlers.support import (
    enter_support,
    support_command,
    support_message,
    support_relay,
    support_user_command,
)
from app.states import UserStates
from tests.fakes import FakeBot, FakePanel

OWNER = 1
ADMIN = 2
USER = 100
CHAT = 99
DAY_MS = 86_400_000


def message(tg_id: int, text: str, *, chat_id: int = CHAT) -> SimpleNamespace:
    """Minimal Telegram message (only ``from_user``/``chat``/``text`` are read)."""
    return SimpleNamespace(
        from_user=SimpleNamespace(id=tg_id, username="neo", first_name="Neo"),
        chat=SimpleNamespace(id=chat_id),
        text=text,
    )


async def add_admin(
    factory: async_sessionmaker[AsyncSession], tg_id: int, role: str, **flags: object
) -> None:
    """Insert an ``admins`` row (role + notification flags)."""
    async with factory() as session:
        session.add(Admin(tg_id=tg_id, role=role, added_by=OWNER, **flags))
        await session.commit()


async def add_user(
    factory: async_sessionmaker[AsyncSession],
    tg_id: int,
    status: UserStatus = UserStatus.APPROVED,
    *,
    bot_blocked: bool = False,
) -> None:
    """Insert a ``users`` row with the given status."""
    async with factory() as session:
        await users_repo.upsert_from_telegram(session, tg_id, username="neo")
        if bot_blocked:
            await users_repo.set_bot_blocked(session, tg_id, True)
        await users_repo.set_status(session, tg_id, status)
        await session.commit()


async def status_of(
    factory: async_sessionmaker[AsyncSession], tg_id: int
) -> str | None:
    async with factory() as session:
        user = await users_repo.get(session, tg_id)
    return None if user is None else str(user.status)


async def audit_actions(
    factory: async_sessionmaker[AsyncSession],
) -> list[tuple[str, str | None]]:
    async with factory() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
    return [(row.action, row.target_id) for row in rows]


async def client_enabled(tg_id: int, container: Container) -> bool | None:
    panel = container.panel
    assert isinstance(panel, FakePanel)
    client = await panel.get_client(tg_id)
    return None if client is None else bool(client.enable)


class SessionWatcher:
    """Sessionmaker proxy recording how many sessions are open at once.

    Used to prove §M0-09.4's requirement that the broadcast reads its recipients
    in one short session and then iterates in memory: the app never sees a DB
    transaction open during the pacing sleeps.
    """

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory
        self.active = 0
        self.peak = 0

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[AsyncSession]:
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            async with self._factory() as session:
                yield session
        finally:
            self.active -= 1


# --- support ---------------------------------------------------------------


async def test_support_toggles_the_waiting_state(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """``/support`` enters the state; a second call leaves it (behavior kept)."""
    await support_command(message(USER, "/support"), fake_bot, handler_container)
    assert await fake_bot.get_state(USER, CHAT) == UserStates.waiting_for_help.name
    assert fake_bot.texts_to(CHAT) == [texts.SUPPORT_ENTERED]

    await support_command(message(USER, "/support"), fake_bot, handler_container)
    assert await fake_bot.get_state(USER, CHAT) is None
    assert fake_bot.texts_to(CHAT)[-1] == texts.SUPPORT_EXITED


async def test_enter_support_is_the_single_entry_point(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """AC (S1-4.2): ``enter_support`` sets the state and confirms, as /support did."""
    await enter_support(fake_bot, CHAT, handler_container, USER)

    assert await fake_bot.get_state(USER, CHAT) == UserStates.waiting_for_help.name
    assert fake_bot.sent == [(CHAT, texts.SUPPORT_ENTERED)]
    assert fake_bot.edits == []


async def test_enter_support_edits_the_card_that_asked(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """The ``prf:support`` button path edits the dashboard instead of adding one."""
    await enter_support(fake_bot, CHAT, handler_container, USER, message_id=501)

    (chat_id, message_id, text, kwargs) = fake_bot.edits[-1]
    assert (chat_id, message_id, text) == (CHAT, 501, texts.SUPPORT_ENTERED)
    assert kwargs["parse_mode"] == "HTML"
    assert fake_bot.sent == []


async def test_support_message_is_escaped_and_fanned_out_to_staff(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """B9/AC: HTML in the text is escaped before it reaches the staff card."""
    await add_admin(session_factory, ADMIN, "admin", notify_support=True)

    await support_message(message(USER, "help <b>& me"), fake_bot, handler_container)

    # Owner + the admin with notify_support.
    assert sorted(chat for chat, _ in fake_bot.sent) == [OWNER, ADMIN]
    for _, text in fake_bot.sent:
        assert "help &lt;b&gt;&amp; me" in text
        assert "<b>&" not in text


async def test_support_message_reaches_nobody_when_staff_opted_out(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The ``notify_support`` flag filters non-owner staff (owner still gets it)."""
    await add_admin(session_factory, ADMIN, "admin", notify_support=False)

    await support_message(message(USER, "ping"), fake_bot, handler_container)

    assert [chat for chat, _ in fake_bot.sent] == [OWNER]


async def test_support_user_sets_the_target_and_relays_the_reply(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """``/support_user <id>`` then a message delivers the escaped reply to the user."""
    await support_user_command(
        message(OWNER, f"/support_user {USER}"), fake_bot, handler_container
    )
    assert await fake_bot.get_state(OWNER, CHAT) == UserStates.writing_to_user.name
    assert fake_bot.texts_to(CHAT)[-1] == texts.SUPPORT_USER_ENTERED.format(tg_id=USER)

    await support_relay(message(OWNER, "use a < b"), fake_bot, handler_container)

    assert fake_bot.texts_to(USER) == [
        texts.SUPPORT_REPLY_CARD.format(text="use a &lt; b")
    ]


async def test_support_user_usage_and_exit(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """A bad id prints the usage hint; a bare second call exits the mode."""
    await support_user_command(
        message(OWNER, "/support_user nope"), fake_bot, handler_container
    )
    assert fake_bot.texts_to(CHAT) == [texts.SUPPORT_USER_USAGE]
    assert await fake_bot.get_state(OWNER, CHAT) is None

    await support_user_command(
        message(OWNER, f"/support_user {USER}"), fake_bot, handler_container
    )
    await support_user_command(
        message(OWNER, "/support_user"), fake_bot, handler_container
    )
    assert await fake_bot.get_state(OWNER, CHAT) is None
    assert fake_bot.texts_to(CHAT)[-1] == texts.SUPPORT_USER_EXITED


async def test_support_user_is_refused_for_non_staff(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """``@require(support.reply)`` drops strangers silently."""
    await support_user_command(
        message(USER, f"/support_user {USER}"), fake_bot, handler_container
    )
    assert fake_bot.sent == []
    assert await fake_bot.get_state(USER, CHAT) is None


# --- broadcast -------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_pacing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the 0.1 s broadcast pacing so the tests stay fast."""
    monkeypatch.setattr(broadcast, "PACING", 0.0)


def _cb(data: str, *, message_id: int = 500) -> SimpleNamespace:
    """Minimal ``CallbackQuery``: only the fields the handlers read."""
    return SimpleNamespace(
        id=f"cb-{message_id}",
        data=data,
        from_user=SimpleNamespace(id=OWNER, username="neo", first_name="Neo"),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=CHAT), message_id=message_id, photo=None
        ),
    )


async def test_broadcast_command_reaches_the_audience_step(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """§S2-6.6: ``/broadcast <text>`` opens the audience step with the text."""
    await add_user(session_factory, 100)

    await broadcast.broadcast_command(
        message(OWNER, "/broadcast 5 <b> off"), fake_bot, handler_container
    )

    sent = [text for chat, text in fake_bot.sent if chat == CHAT]
    assert sent and "5 &lt;b&gt; off" in sent[-1]
    markup = fake_bot.messages[-1][2]["reply_markup"]
    payloads = [b.callback_data for row in markup.keyboard for b in row]
    assert "adm:broadcast:a:all" in payloads
    assert await fake_bot.get_state(OWNER, CHAT) == UserStates.admin_broadcast.name


async def test_broadcast_holds_no_db_session_while_pacing(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§M0-09.4: one short read session, then in-memory sends with no session open."""
    watcher = SessionWatcher(session_factory)
    handler_container.sessionmaker = watcher  # type: ignore[assignment]
    handler_container.init_users()  # rebuild UserService on the watched sessionmaker
    assert handler_container.users is not None

    await add_user(session_factory, 100)
    await add_user(session_factory, 101)

    notifier = handler_container.notifier
    assert notifier is not None
    sent_during: list[int] = []
    slept_during: list[int] = []
    real_sleep = asyncio.sleep

    async def watched_send(chat_id: int, text: str, **kwargs: object) -> bool:
        sent_during.append(watcher.active)
        return True

    async def watched_sleep(seconds: float) -> None:
        slept_during.append(watcher.active)
        await real_sleep(0)

    notifier.safe_send = watched_send  # type: ignore[method-assign]
    monkeypatch.setattr(broadcast, "PACING", 0.01)
    monkeypatch.setattr(broadcast.asyncio, "sleep", watched_sleep)

    await broadcast.broadcast_command(
        message(OWNER, "/broadcast hi"), fake_bot, handler_container
    )
    await admin_callback(_cb("adm:broadcast:a:all"), fake_bot, handler_container)
    await admin_callback(_cb("adm:broadcast:go"), fake_bot, handler_container)

    # The recipients were read in exactly one session, closed before the loop...
    assert watcher.peak == 1
    assert watcher.active == 0
    # ...so no session (let alone a transaction) is open while sending or sleeping.
    assert sent_during == [0, 0]
    assert slept_during == [0, 0]
    # The summary is a single edit of the live progress message (B3).
    assert fake_bot.edits[-1][2] == texts.BROADCAST_SUMMARY.format(sent=2, failed=0)


async def test_broadcast_without_text_shows_usage(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """An empty ``/broadcast`` argument prints the usage hint."""
    await broadcast.broadcast_command(
        message(OWNER, "/broadcast"), fake_bot, handler_container
    )
    assert fake_bot.texts_to(CHAT) == [texts.BROADCAST_USAGE]


async def test_broadcast_is_refused_for_staff_without_permission(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``broadcast.send`` is admin+, so a support account is refused."""
    await add_admin(session_factory, ADMIN, "support")

    await broadcast.broadcast_command(
        message(ADMIN, "/broadcast hi"), fake_bot, handler_container
    )

    assert fake_bot.texts_to(CHAT) == [texts.ERROR_ACCESS_DENIED]


# --- /ban, /unban, /banned_list -------------------------------------------


async def test_ban_disables_the_client_and_writes_audit(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC: ban blocks the row, disables the panel client, audits, and replies."""
    panel = handler_container.panel
    assert isinstance(panel, FakePanel)
    panel.seed(USER, enable=True, expiry_ms=int(time.time() * 1000) + DAY_MS)
    await add_user(session_factory, USER)

    await ban_command(message(OWNER, f"/ban {USER}"), fake_bot, handler_container)

    assert await status_of(session_factory, USER) == UserStatus.BLOCKED
    assert await client_enabled(USER, handler_container) is False
    assert ("user.ban", str(USER)) in await audit_actions(session_factory)
    assert fake_bot.texts_to(CHAT) == [texts.BAN_DONE.format(tg_id=USER)]
    assert fake_bot.texts_to(USER) == [texts.BAN_USER_NOTICE]


async def test_ban_refuses_staff(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Legacy rule kept (``main.py:88-93``): an admin cannot be banned."""
    await add_admin(session_factory, ADMIN, "support")
    await add_user(session_factory, ADMIN)

    await ban_command(message(OWNER, f"/ban {ADMIN}"), fake_bot, handler_container)

    assert fake_bot.texts_to(CHAT) == [texts.BAN_STAFF_REFUSED]
    assert await status_of(session_factory, ADMIN) == UserStatus.APPROVED
    assert await audit_actions(session_factory) == []


async def test_ban_without_text_shows_usage(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    await ban_command(message(OWNER, "/ban"), fake_bot, handler_container)
    assert fake_bot.texts_to(CHAT) == [texts.BAN_USAGE]


async def test_ban_unknown_user_is_refused(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """No ``users`` row → nothing is banned and the admin is told."""
    await ban_command(message(OWNER, f"/ban {USER}"), fake_bot, handler_container)

    assert fake_bot.texts_to(CHAT) == [texts.ERROR_UNKNOWN_USER]


async def test_ban_keeps_working_when_the_panel_is_down(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A panel outage blocks in the DB, warns the admin and alerts staff (B7 spirit)."""
    panel = handler_container.panel
    assert isinstance(panel, FakePanel)
    panel.seed(USER, enable=True)
    await add_user(session_factory, USER)
    panel.unavailable = True

    await ban_command(message(OWNER, f"/ban {USER}"), fake_bot, handler_container)

    assert await status_of(session_factory, USER) == UserStatus.BLOCKED
    (reply_text,) = fake_bot.texts_to(CHAT)
    assert reply_text.startswith(texts.BAN_DONE.format(tg_id=USER))
    assert "Не удалось обновить панель" in reply_text
    assert len(fake_bot.texts_to(OWNER)) == 1


async def test_unban_reenables_a_live_subscription(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC: unban re-enables the client when the expiry is still in the future."""
    panel = handler_container.panel
    assert isinstance(panel, FakePanel)
    panel.seed(USER, enable=False, expiry_ms=int(time.time() * 1000) + DAY_MS)
    await add_user(session_factory, USER, UserStatus.BLOCKED)

    await unban_command(message(OWNER, f"/unban {USER}"), fake_bot, handler_container)

    assert await status_of(session_factory, USER) == UserStatus.APPROVED
    assert await client_enabled(USER, handler_container) is True
    assert ("user.unban", str(USER)) in await audit_actions(session_factory)
    assert fake_bot.texts_to(CHAT) == [texts.UNBAN_DONE.format(tg_id=USER)]


async def test_unban_leaves_an_expired_client_disabled(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC: an expired subscription is not silently re-enabled by ``/unban``."""
    panel = handler_container.panel
    assert isinstance(panel, FakePanel)
    panel.seed(USER, enable=False, expiry_ms=int(time.time() * 1000) - DAY_MS)
    await add_user(session_factory, USER, UserStatus.BLOCKED)

    await unban_command(message(OWNER, f"/unban {USER}"), fake_bot, handler_container)

    assert await status_of(session_factory, USER) == UserStatus.APPROVED
    assert await client_enabled(USER, handler_container) is False
    assert fake_bot.texts_to(CHAT) == [texts.UNBAN_EXPIRED.format(tg_id=USER)]


async def test_unban_leaves_a_never_activated_legacy_client_disabled(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (S0-1.7): ``expiry==0`` + ``enable=False`` is never activated.

    Unban restores bot access only: the client stays disabled and its expiry is
    left untouched (writing ``now`` here would turn an unlimited client into a
    permanently expired one).
    """
    panel = handler_container.panel
    assert isinstance(panel, FakePanel)
    panel.seed(USER, enable=False, expiry_ms=0)
    await add_user(session_factory, USER, UserStatus.BLOCKED)

    await unban_command(message(OWNER, f"/unban {USER}"), fake_bot, handler_container)

    assert await status_of(session_factory, USER) == UserStatus.APPROVED
    assert await client_enabled(USER, handler_container) is False
    client = await panel.get_client(USER)
    assert client is not None
    assert int(client.expiry_time) == 0  # expiry never rewritten
    assert fake_bot.texts_to(CHAT) == [texts.UNBAN_EXPIRED.format(tg_id=USER)]


async def test_unban_retries_the_panel_for_an_approved_user(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A previous panel failure leaves DB ``approved`` + client disabled.

    Re-running ``/unban`` must retry ``set_enabled(True)`` instead of answering
    "not banned", so an admin can clear the out-of-sync state.
    """
    panel = handler_container.panel
    assert isinstance(panel, FakePanel)
    panel.seed(USER, enable=False, expiry_ms=int(time.time() * 1000) + DAY_MS)
    await add_user(session_factory, USER, UserStatus.APPROVED)

    await unban_command(message(OWNER, f"/unban {USER}"), fake_bot, handler_container)

    assert await client_enabled(USER, handler_container) is True
    assert ("user.unban", str(USER)) in await audit_actions(session_factory)
    assert fake_bot.texts_to(CHAT) == [texts.UNBAN_DONE.format(tg_id=USER)]
    assert fake_bot.texts_to(USER) == [texts.UNBAN_USER_NOTICE]


async def test_unban_of_a_non_banned_user_is_a_no_op(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Nothing to retry (no client) → still reported as "not banned"."""
    await add_user(session_factory, USER)

    await unban_command(message(OWNER, f"/unban {USER}"), fake_bot, handler_container)

    assert fake_bot.texts_to(CHAT) == [texts.UNBAN_NOT_BANNED]
    assert await audit_actions(session_factory) == []


async def test_banned_list_reads_the_database(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The list comes from ``users.status``, not from a JSON file."""
    await add_user(session_factory, 100, UserStatus.BLOCKED)
    await add_user(session_factory, 101, UserStatus.BLOCKED)
    await add_user(session_factory, 102)

    await banned_list_command(
        message(OWNER, "/banned_list"), fake_bot, handler_container
    )

    (text,) = fake_bot.texts_to(CHAT)
    assert "<code>100</code>" in text
    assert "<code>101</code>" in text
    assert "102" not in text


async def test_banned_list_empty(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    await banned_list_command(
        message(OWNER, "/banned_list"), fake_bot, handler_container
    )
    assert fake_bot.texts_to(CHAT) == [texts.BANNED_LIST_EMPTY]
