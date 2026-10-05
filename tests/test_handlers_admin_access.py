"""Access request card tests (``TASK_PLAN.md`` §S3-2).

Covers the S3-2 acceptance criteria:

* the card fans out to every ``access.review`` reviewer and each delivered copy is
  stored in ``admin_cards`` (§S3-2.4);
* two simultaneous accepts → exactly one winner **and** exactly one panel client
  (§S3-2.3/.10);
* a repeated ``/start`` while ``pending`` produces a single card (§S3-2.9);
* reject/block notify the requesting user and leave them out of the gate
  (§S3-2.10);
* a caller without ``access.review`` is refused before ``decide_access`` runs, and
  the losing reviewer is told «уже обработано» (§S3-2.5);
* the ``adm:access`` queue lists only ``pending`` rows and its page nav parses
  (§S3-2.7).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import texts
from app.callbacks import Access, AdminNav, ProfileNav, unpack
from app.container import Container
from app.db.models import Admin, UserStatus
from app.db.repositories import admin_cards as admin_cards_repo
from app.db.repositories import users as users_repo
from app.handlers.admin import access as screen
from app.handlers.admin.nav import admin_callback
from app.handlers.start import start_command
from app.permissions import Role
from tests.fakes import FakeBot, FakePanel

OWNER = 1
ADMIN = 7
SUPPORT = 8
USER = 555
CHAT = 99

#: Marker of the §S3-2.4 card title, used to count delivered cards.
CARD_MARK = "Новая заявка на доступ"


def message(
    tg_id: int, *, chat_id: int = CHAT, username: str | None = "neo"
) -> SimpleNamespace:
    """Minimal Telegram message: only the fields the handlers read."""
    return SimpleNamespace(
        from_user=SimpleNamespace(id=tg_id, username=username, first_name="Neo"),
        chat=SimpleNamespace(id=chat_id),
        text="/start",
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


def acc(action: str, *, tg_id: int = USER, actor: int = OWNER) -> SimpleNamespace:
    """Minimal ``acc:`` review callback (``actor`` = who clicked)."""
    return callback(Access(action, int(tg_id)).pack(), tg_id=actor)


def labels_of(markup: Any) -> list[str]:
    """Return the button labels of an inline keyboard, row by row."""
    return [button.text for row in markup.keyboard for button in row]


def payloads_of(markup: Any) -> list[str]:
    """Return the callback payloads of an inline keyboard, row by row."""
    return [button.callback_data for row in markup.keyboard for button in row]


def cards_for(fake_bot: FakeBot, marker: str = CARD_MARK) -> list[tuple[int, str]]:
    """Return every message whose text carries ``marker``."""
    return [(chat_id, text) for chat_id, text, _ in fake_bot.messages if marker in text]


async def seed_pending(
    factory: async_sessionmaker[AsyncSession],
    tg_id: int = USER,
    *,
    username: str = "neo",
) -> None:
    """Insert a ``pending`` access request directly (bypassing ``/start``)."""
    async with factory() as session:
        await users_repo.upsert_from_telegram(session, tg_id, username=username)
        await users_repo.set_status(session, tg_id, UserStatus.PENDING)
        await session.commit()


async def seed_admin(
    factory: async_sessionmaker[AsyncSession], tg_id: int, role: Role
) -> None:
    """Grant ``tg_id`` a non-owner role from the ``admins`` table."""
    async with factory() as session:
        session.add(Admin(tg_id=tg_id, role=str(role), added_by=OWNER))
        await session.commit()


async def get_status(
    factory: async_sessionmaker[AsyncSession], tg_id: int
) -> str | None:
    async with factory() as session:
        user = await users_repo.get(session, tg_id)
    return None if user is None else str(user.status)


async def edited_texts(fake_bot: FakeBot) -> list[str]:
    """Return the text of every ``edit_message_text`` call."""
    return [text for _, _, text, _ in fake_bot.edits]


# --- the card itself (§S3-2.4) ----------------------------------------------


async def test_send_access_card_fans_out_and_stores_every_copy(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (§S3-2.4): one card per reviewer, each copy remembered."""
    await seed_admin(session_factory, ADMIN, Role.ADMIN)
    notifier = handler_container.notifier
    assert notifier is not None

    delivered = await notifier.send_access_card(USER, username="neo", first_name="Neo")

    assert sorted(delivered) == [OWNER, ADMIN]
    assert sorted(chat_id for chat_id, _ in cards_for(fake_bot)) == [OWNER, ADMIN]
    async with session_factory() as session:
        copies = await admin_cards_repo.list_for(session, "access", USER)
    assert sorted(card.chat_id for card in copies) == [OWNER, ADMIN]


async def test_access_card_buttons_are_review_payloads(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """The card's three buttons unpack to the matching ``acc:`` actions."""
    notifier = handler_container.notifier
    assert notifier is not None
    await notifier.send_access_card(USER, username="neo", first_name="Neo")

    _, _, kwargs = fake_bot.messages[0]
    markup = kwargs["reply_markup"]
    assert labels_of(markup) == [
        texts.BUTTON_ACCESS_ACCEPT,
        texts.BUTTON_ACCESS_REJECT,
        texts.BUTTON_ACCESS_BLOCK,
    ]
    assert [unpack(data) for data in payloads_of(markup)] == [
        Access("accept", USER),
        Access("reject", USER),
        Access("block", USER),
    ]


# --- the race (§S3-2.3/.10) -------------------------------------------------


async def test_concurrent_accept_has_one_winner_and_one_client(
    handler_container: Container, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """AC (§S3-2.10): two simultaneous accepts → one winner, one client."""
    await seed_admin(session_factory, ADMIN, Role.ADMIN)
    await seed_pending(session_factory)
    users = handler_container.users
    panel = handler_container.panel
    assert users is not None and isinstance(panel, FakePanel)

    results = await asyncio.gather(
        users.decide_access(USER, OWNER, "accept"),
        users.decide_access(USER, ADMIN, "accept"),
        return_exceptions=True,
    )

    won = [r for r in results if getattr(r, "already", True) is False]
    lost = [r for r in results if getattr(r, "already", False) is True]
    assert not [r for r in results if isinstance(r, BaseException)]
    assert len(won) == 1
    assert len(lost) == 1
    assert won[0].accepted is True
    assert lost[0].status is None

    assert panel.calls.count("ensure_client") == 1
    assert await panel.get_client(USER) is not None
    assert await get_status(session_factory, USER) == UserStatus.APPROVED


async def test_lost_race_is_answered_with_already(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (§S3-2.3): the second click gets «уже обработано», no side effect."""
    await seed_pending(session_factory)
    await screen.access_callback(acc("accept"), fake_bot, handler_container)

    await screen.access_callback(acc("reject"), fake_bot, handler_container)

    assert fake_bot.callback_answers[-1] == ("cb-500", texts.ACCESS_ALREADY, True)
    # The first decision stands: the loser never downgraded the row.
    assert await get_status(session_factory, USER) == UserStatus.APPROVED


async def test_decide_access_rejects_an_unknown_decision(
    handler_container: Container, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The service refuses a decision that is not in the callback vocabulary."""
    await seed_pending(session_factory)
    users = handler_container.users
    assert users is not None

    with pytest.raises(ValueError):
        await users.decide_access(USER, OWNER, "nope")


# --- accepting / rejecting / blocking (§S3-2.5/.10) ------------------------


async def test_accept_syncs_every_card_and_notifies_the_user(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (§S3-2.5): the card is edited with «обработал» and the user is told."""
    await seed_pending(session_factory)
    notifier = handler_container.notifier
    assert notifier is not None
    await notifier.send_access_card(USER, username="neo", first_name="Neo")

    await screen.access_callback(acc("accept"), fake_bot, handler_container)

    synced = [text for text in await edited_texts(fake_bot) if CARD_MARK in text]
    assert len(synced) == 1
    assert texts.ACCESS_CARD_ACCEPTED.format(actor="@neo") in synced[0]
    assert (USER, texts.ACCESS_USER_ACCEPTED) in fake_bot.sent

    _, text, kwargs = fake_bot.messages[-1]
    assert text == texts.ACCESS_USER_ACCEPTED
    assert payloads_of(kwargs["reply_markup"]) == [
        ProfileNav("profile").pack(),
        ProfileNav("pay").pack(),
    ]


@pytest.mark.parametrize(
    ("decision", "status", "notice"),
    [
        ("reject", UserStatus.REJECTED, texts.ACCESS_USER_REJECTED),
        ("block", UserStatus.BLOCKED, texts.ACCESS_USER_BLOCKED),
    ],
)
async def test_reject_and_block_notify_the_user(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
    decision: str,
    status: UserStatus,
    notice: str,
) -> None:
    """AC (§S3-2.10): the refusal reaches the user and the row leaves ``pending``."""
    await seed_pending(session_factory)

    await screen.access_callback(acc(decision), fake_bot, handler_container)

    assert await get_status(session_factory, USER) == status
    assert (USER, notice) in fake_bot.sent
    assert fake_bot.callback_answers[-1] == ("cb-500", texts.CALLBACK_DONE, False)


async def test_access_callback_refuses_a_caller_without_permission(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (§S3-2.5): a forged ``acc:`` never reaches ``decide_access``."""
    await seed_admin(session_factory, SUPPORT, Role.SUPPORT)
    await seed_pending(session_factory)

    await screen.access_callback(
        acc("accept", actor=SUPPORT), fake_bot, handler_container
    )

    assert await get_status(session_factory, USER) == UserStatus.PENDING
    assert fake_bot.callback_answers[-1][2] is True  # show_alert denial


async def test_access_callback_answers_a_stale_button(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """A malformed payload is a stale toast, never a crash (B5)."""
    await screen.access_callback(callback("acc:accept"), fake_bot, handler_container)

    assert fake_bot.callback_answers[-1] == (
        "cb-500",
        texts.ERROR_STALE_BUTTON,
        False,
    )


# --- /start fan-out (§S3-2.9) ----------------------------------------------


async def test_first_start_creates_exactly_one_card(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (§S3-2.9): ``approval`` mode parks the request and cards it once."""
    await start_command(message(USER), fake_bot, handler_container)

    assert await get_status(session_factory, USER) == UserStatus.PENDING
    assert fake_bot.texts_to(CHAT) == [texts.ACCESS_PENDING]
    assert len(cards_for(fake_bot)) == 1
    async with session_factory() as session:
        copies = await admin_cards_repo.list_for(session, "access", USER)
    assert [card.chat_id for card in copies] == [OWNER]


async def test_repeated_start_does_not_spawn_a_second_card(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (§S3-2.9/.10): one card per user across repeated ``/start``."""
    for _ in range(3):
        await start_command(message(USER), fake_bot, handler_container)

    assert await get_status(session_factory, USER) == UserStatus.PENDING
    assert fake_bot.texts_to(CHAT) == [texts.ACCESS_PENDING] * 3
    assert len(cards_for(fake_bot)) == 1


async def test_invite_only_mode_sends_no_card(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``invite_only`` refuses the stranger without waking a reviewer (§S3-1)."""
    settings = handler_container.settings_service
    assert settings is not None
    await settings.set_access_mode("invite_only")

    await start_command(message(USER), fake_bot, handler_container)

    assert cards_for(fake_bot) == []
    assert await get_status(session_factory, USER) is None


# --- the adm:access queue (§S3-2.7) ------------------------------------------


async def test_access_screen_lists_only_pending_and_navigates_pages(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (§S3-2.7): the queue shows ``pending`` rows only; page nav parses."""
    for tg_id in range(501, 510):  # 9 requests → 2 pages of 8
        await seed_pending(session_factory, tg_id, username=f"u{tg_id}")
    async with session_factory() as session:
        await users_repo.upsert_from_telegram(session, 999, username="approved")
        await users_repo.set_status(session, 999, UserStatus.APPROVED)
        await session.commit()

    await admin_callback(
        callback(AdminNav("access").pack()), fake_bot, handler_container
    )

    text = fake_bot.edits[-1][2]
    assert "Заявки на доступ" in text and "стр. 1/2" in text
    markup = fake_bot.edits[-1][3]["reply_markup"]
    labels = labels_of(markup)
    assert any("u501" in label for label in labels)
    assert any("u508" in label for label in labels)
    assert not any("u509" in label for label in labels)  # page 2
    assert not any("approved" in label for label in labels)  # decided rows excluded
    assert AdminNav("access", "c", "501").pack() in payloads_of(markup)
    assert AdminNav("access", "p", "1").pack() in payloads_of(markup)

    await admin_callback(
        callback(AdminNav("access", "p", "1").pack()), fake_bot, handler_container
    )

    second = fake_bot.edits[-1][2]
    second_labels = labels_of(fake_bot.edits[-1][3]["reply_markup"])
    assert "стр. 2/2" in second
    assert any("u509" in label for label in second_labels)


async def test_access_screen_opens_the_standard_review_card(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (§S3-2.7): a queue row opens the very card ``/start`` would send."""
    await seed_pending(session_factory, 501, username="first")

    await admin_callback(
        callback(AdminNav("access", "c", "501").pack()), fake_bot, handler_container
    )

    cards = cards_for(fake_bot)
    assert len(cards) == 1
    assert cards[0][0] == CHAT  # the acting admin, not a fan-out
    assert ("cb-500", texts.CALLBACK_DONE, False) in fake_bot.callback_answers


async def test_open_card_refuses_an_already_decided_request(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A row decided between render and click opens no card (§S3-2.7)."""
    await seed_pending(session_factory, 501)
    users = handler_container.users
    assert users is not None
    await users.decide_access(501, OWNER, "reject")

    await admin_callback(
        callback(AdminNav("access", "c", "501").pack()), fake_bot, handler_container
    )

    assert cards_for(fake_bot) == []
    assert ("cb-500", texts.ACCESS_ALREADY, True) in fake_bot.callback_answers
