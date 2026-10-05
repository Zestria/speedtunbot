"""Admin broadcast screen tests (``TASK_PLAN.md`` §S2-6).

Covers the S2-6 acceptance criteria: the three audiences resolve on a fixture
(``all`` skips blocked/unreachable users, ``active`` needs an enabled live client
and ``soon`` is the ≤ 7-day window), the prompt → audience → preview → send flow
edits exactly one summary, a failing send is counted not raised, ``bot_blocked``
users never receive, the progress line lands every ``PROGRESS_EVERY`` sends and
«✖ Отмена» clears both the FSM state and the pending text.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import texts
from app.callbacks import AdminNav, unpack
from app.container import Container
from app.db.models import Admin, AuditLog, UserStatus
from app.db.repositories import users as users_repo
from app.handlers import broadcast
from app.handlers.admin.nav import admin_callback
from app.states import UserStates
from tests.fakes import FakeBot, FakePanel

OWNER = 1
ADMIN = 2
CHAT = 99
DAY_MS = 86_400_000


def message(tg_id: int, text: str, *, chat_id: int = CHAT) -> SimpleNamespace:
    """Minimal Telegram message (only ``from_user``/``chat``/``text`` are read)."""
    return SimpleNamespace(
        from_user=SimpleNamespace(id=tg_id, username="neo", first_name="Neo"),
        chat=SimpleNamespace(id=chat_id),
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


async def audit_actions(factory: async_sessionmaker[AsyncSession]) -> list[str]:
    """Return every recorded audit action, newest first."""
    async with factory() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
    return [row.action for row in rows]


def seed_client(
    container: Container,
    tg_id: int,
    *,
    enable: bool = True,
    expiry_ms: int | None = None,
) -> None:
    """Insert a panel client for ``tg_id`` on the container's ``FakePanel``."""
    panel = container.panel
    assert isinstance(panel, FakePanel)
    panel.seed(tg_id, enable=enable, expiry_ms=expiry_ms)


@pytest.fixture(autouse=True)
def _no_pacing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the 0.1 s broadcast pacing so the tests stay fast."""
    monkeypatch.setattr(broadcast, "PACING", 0.0)


def _future_ms(days: int) -> int:
    """Return epoch ms ``days`` from now."""
    return int(time.time() * 1000) + days * DAY_MS


# --- audience resolution (§S2-6.2) ------------------------------------------


async def test_audience_all_is_reachable_approved_users(
    handler_container: Container, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """``all`` = approved and not ``bot_blocked``; blocked/unreachable excluded."""
    await add_user(session_factory, 100)
    await add_user(session_factory, 101)
    await add_user(session_factory, 102, UserStatus.BLOCKED)
    await add_user(session_factory, 103, bot_blocked=True)

    assert await broadcast.audience_ids(handler_container, "all") == [100, 101]


async def test_audience_active_needs_an_enabled_live_client(
    handler_container: Container, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """``active`` keeps only enabled clients that are not yet expired."""
    await add_user(session_factory, 100)
    await add_user(session_factory, 101)
    await add_user(session_factory, 102)
    seed_client(handler_container, 100, enable=True, expiry_ms=_future_ms(30))
    seed_client(handler_container, 101, enable=False, expiry_ms=_future_ms(30))
    seed_client(handler_container, 102, enable=True, expiry_ms=_future_ms(-1))

    assert await broadcast.audience_ids(handler_container, "active") == [100]


async def test_audience_soon_is_the_seven_day_window(
    handler_container: Container, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """``soon`` = clients expiring within 7 days (a far expiry drops out)."""
    await add_user(session_factory, 100)
    await add_user(session_factory, 101)
    seed_client(handler_container, 100, enable=True, expiry_ms=_future_ms(3))
    seed_client(handler_container, 101, enable=True, expiry_ms=_future_ms(30))

    assert await broadcast.audience_ids(handler_container, "soon") == [100]


async def test_unknown_audience_falls_back_to_all(
    handler_container: Container, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await add_user(session_factory, 100)
    assert await broadcast.audience_ids(handler_container, "bogus") == [100]


# --- prompt → audience → preview → send (§S2-6.3/.4/.5) ----------------------


async def test_prompt_flow_renders_preview_and_one_summary(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC: audience selection, preview count, single summary, audit row."""
    await add_user(session_factory, 100)
    await add_user(session_factory, 101)

    # ``adm:broadcast`` prompts for the text and enters the FSM state.
    await admin_callback(callback("adm:broadcast"), fake_bot, handler_container)
    assert fake_bot.edits[-1][2] == texts.BROADCAST_PROMPT
    assert await fake_bot.get_state(OWNER, CHAT) == UserStates.admin_broadcast.name

    # The typed text opens the audience step (escaped, buttons parse).
    await broadcast.admin_broadcast_message(
        message(OWNER, "5 <b> off"), fake_bot, handler_container
    )
    card_markup = fake_bot.messages[-1][2]["reply_markup"]
    assert "5 &lt;b&gt; off" in fake_bot.messages[-1][1]
    assert set(payloads_of(card_markup)) == {
        AdminNav("broadcast", "a", "all").pack(),
        AdminNav("broadcast", "a", "active").pack(),
        AdminNav("broadcast", "a", "soon").pack(),
        AdminNav("broadcast", "x").pack(),
    }

    # Choosing an audience shows the preview with the recipient count.
    await admin_callback(
        callback("adm:broadcast:a:all", message_id=501), fake_bot, handler_container
    )
    assert "Получателей: <b>2</b>" in fake_bot.edits[-1][2]
    preview_markup = fake_bot.edits[-1][3]["reply_markup"]
    assert unpack(payloads_of(preview_markup)[0]) == AdminNav("broadcast", "go")

    # Confirming sends to both, then edits the single summary into place.
    await admin_callback(
        callback("adm:broadcast:go", message_id=502), fake_bot, handler_container
    )
    delivered = sorted(chat for chat, _ in fake_bot.sent if chat != CHAT)
    assert delivered == [100, 101]
    for chat in (100, 101):
        assert fake_bot.texts_to(chat) == [
            f"{texts.BROADCAST_HEADER}\n\n5 &lt;b&gt; off"
        ]
    assert fake_bot.edits[-1][2] == texts.BROADCAST_SUMMARY.format(sent=2, failed=0)
    assert await audit_actions(session_factory) == [broadcast.AUDIT_BROADCAST]
    assert await fake_bot.get_state(OWNER, CHAT) is None


async def test_bot_blocked_users_are_skipped(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC: a user who blocked the bot is never targeted."""
    await add_user(session_factory, 100)
    await add_user(session_factory, 101, bot_blocked=True)

    await broadcast.broadcast_command(
        message(OWNER, "/broadcast hi"), fake_bot, handler_container
    )
    await admin_callback(
        callback("adm:broadcast:a:all", message_id=501), fake_bot, handler_container
    )
    await admin_callback(
        callback("adm:broadcast:go", message_id=502), fake_bot, handler_container
    )

    delivered = sorted(chat for chat, _ in fake_bot.sent if chat != CHAT)
    assert delivered == [100]
    assert fake_bot.edits[-1][2] == texts.BROADCAST_SUMMARY.format(sent=1, failed=0)


async def test_failing_send_is_counted_not_raised(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A failing send is counted, not raised, and the summary still lands."""
    await add_user(session_factory, 100)
    await add_user(session_factory, 101)

    notifier = handler_container.notifier
    assert notifier is not None
    seen: list[int] = []

    async def flaky(chat_id: int, text: str, **kwargs: object) -> bool:
        seen.append(int(chat_id))
        return len(seen) != 1  # the first recipient "fails"

    notifier.safe_send = flaky  # type: ignore[method-assign]

    await broadcast.broadcast_command(
        message(OWNER, "/broadcast hi"), fake_bot, handler_container
    )
    await admin_callback(
        callback("adm:broadcast:a:all", message_id=501), fake_bot, handler_container
    )
    await admin_callback(
        callback("adm:broadcast:go", message_id=502), fake_bot, handler_container
    )

    assert seen == [100, 101]
    assert fake_bot.edits[-1][2] == texts.BROADCAST_SUMMARY.format(sent=1, failed=1)


async def test_progress_line_lands_before_the_summary(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC: the live progress message is edited every ``PROGRESS_EVERY`` sends."""
    for tg_id in range(100, 125):  # 25 recipients → one progress tick at 20
        await add_user(session_factory, tg_id)

    await broadcast.broadcast_command(
        message(OWNER, "/broadcast hi"), fake_bot, handler_container
    )
    await admin_callback(
        callback("adm:broadcast:a:all", message_id=501), fake_bot, handler_container
    )
    await admin_callback(
        callback("adm:broadcast:go", message_id=502), fake_bot, handler_container
    )

    progress = broadcast.progress_text(20, 20, 0, 25)
    assert progress in [edit[2] for edit in fake_bot.edits]
    assert fake_bot.edits[-1][2] == texts.BROADCAST_SUMMARY.format(sent=25, failed=0)


async def test_cancel_clears_the_pending_text(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """«✖ Отмена» drops the FSM state and returns to the dashboard."""
    await add_user(session_factory, 100)

    await broadcast.broadcast_command(
        message(OWNER, "/broadcast hi"), fake_bot, handler_container
    )
    assert await fake_bot.get_state(OWNER, CHAT) == UserStates.admin_broadcast.name

    await admin_callback(
        callback("adm:broadcast:x", message_id=503), fake_bot, handler_container
    )

    assert await fake_bot.get_state(OWNER, CHAT) is None
    assert "Панель администратора" in fake_bot.edits[-1][2]


async def test_screen_rechecks_broadcast_permission(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A forged ``adm:broadcast`` from a support account is refused."""
    async with session_factory() as session:
        session.add(Admin(tg_id=ADMIN, role="support", added_by=OWNER))
        await session.commit()

    await admin_callback(
        callback("adm:broadcast", tg_id=ADMIN), fake_bot, handler_container
    )

    assert fake_bot.edits == []
    assert fake_bot.messages == []
    assert ("cb-500", "Недостаточно прав", True) in fake_bot.callback_answers
