"""Admin user card + one-tap actions tests (§S2-3).

Covers the S2-3 acceptance criteria: the card renders header/status/traffic (and
the payments line only with ``payments.view``), the keyboard is filtered by role,
every action mutates ``FakePanel`` (or the user row) and writes its audit row, a
forged mutation from a role without the permission is refused, the two
confirmation flows ask first, and the ``admin_grant`` prompt round-trips.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import permissions, texts
from app.callbacks import Confirm, unpack
from app.container import Container
from app.db.models import Admin, AuditLog, Payment, PaymentStatus, UserStatus
from app.db.repositories import payments as payments_repo
from app.db.repositories import users as users_repo
from app.handlers.admin import user_card
from app.handlers.admin.nav import admin_callback
from app.handlers.confirm import confirm_callback
from app.handlers.support import support_relay
from app.permissions import Permission, Role
from app.services.panel import gb_to_bytes
from app.states import UserStates
from tests.fakes import FakeBot, FakePanel

OWNER = 1
SUPPORT = 42
EDITOR = 43
USER = 500
CHAT = 99
GB = 1024**3
DAY_MS = 24 * 60 * 60 * 1000
#: ``seed``/``render`` read the wall clock, so offsets are anchored to "now" with
#: a minute of slack so integer day counting cannot round down.
NOW = int(time.time() * 1000)
SLACK_MS = 60 * 1000


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


def message(tg_id: int, text: str, *, message_id: int = 700) -> SimpleNamespace:
    """Minimal Telegram message (the day count typed after ``grc``)."""
    return SimpleNamespace(
        from_user=SimpleNamespace(id=tg_id, username="neo", first_name="Neo"),
        chat=SimpleNamespace(id=CHAT),
        message_id=message_id,
        text=text,
    )


def payloads_of(markup: Any) -> list[str]:
    """Return the callback payloads of an inline keyboard, row by row."""
    return [button.callback_data for row in markup.keyboard for button in row]


def labels_of(markup: Any) -> list[str]:
    """Return the button labels of an inline keyboard, row by row."""
    return [button.text for row in markup.keyboard for button in row]


def toast_of(bot: FakeBot) -> tuple[str, str | None, bool]:
    """Return the meaningful toast of the last callback.

    Every ``adm:`` callback is answered twice — the screen sends the real toast,
    then the shared dispatcher answers a no-op ``None`` — and Telegram keeps only
    the first, so the tests read the latest non-``None`` answer.
    """
    for entry in reversed(bot.callback_answers):
        if entry[1] is not None:
            return entry
    return bot.callback_answers[-1]


def panel_of(container: Container) -> FakePanel:
    """Return the container's panel double."""
    panel = container.panel
    assert isinstance(panel, FakePanel)
    return panel


async def seed_user(
    factory: async_sessionmaker[AsyncSession],
    tg_id: int = USER,
    *,
    username: str = "neo",
    status: UserStatus = UserStatus.APPROVED,
) -> None:
    """Insert a ``users`` row directly."""
    async with factory() as session:
        await users_repo.upsert_from_telegram(session, tg_id, username=username)
        await users_repo.set_status(session, tg_id, status)
        await session.commit()


async def seed_staff(
    factory: async_sessionmaker[AsyncSession], tg_id: int, role: Role
) -> None:
    """Grant ``tg_id`` a non-owner role from the ``admins`` table."""
    async with factory() as session:
        session.add(Admin(tg_id=tg_id, role=str(role), added_by=OWNER))
        await session.commit()


async def seed_payment(
    factory: async_sessionmaker[AsyncSession],
    tg_id: int,
    price: int,
    status: PaymentStatus = PaymentStatus.APPROVED,
) -> None:
    """Insert one payment for ``tg_id``."""
    async with factory() as session:
        await payments_repo.add(
            session,
            Payment(
                user_tg_id=tg_id,
                tariff_id=None,
                tariff_name="Месяц",
                days=30,
                price=price,
                status=str(status),
            ),
        )
        await session.commit()


async def audit_rows(
    factory: async_sessionmaker[AsyncSession],
) -> list[tuple[str, str | None]]:
    """Return ``(action, target_id)`` for every audit row."""
    async with factory() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
    return [(row.action, row.target_id) for row in rows]


async def status_of(
    factory: async_sessionmaker[AsyncSession], tg_id: int = USER
) -> tuple[str | None, str | None]:
    """Return ``(status, status_note)`` of a user row."""
    async with factory() as session:
        row = await users_repo.get(session, tg_id)
    assert row is not None
    return row.status, row.status_note


async def open_card(
    container: Container,
    bot: FakeBot,
    *,
    tg_id: int = OWNER,
    message_id: int = 500,
) -> None:
    """Dispatch ``adm:users:card:<USER>`` like the list button does."""
    await admin_callback(
        callback(f"adm:users:card:{USER}", tg_id=tg_id, message_id=message_id),
        bot,
        container,
    )


def confirm_payload(bot: FakeBot, *, cancel: bool) -> str:
    """Return the confirm / cancel ``cf:`` payload of the last shown card."""
    for payload in payloads_of(bot.edits[-1][3]["reply_markup"]):
        parsed = unpack(payload)
        assert isinstance(parsed, Confirm)
        if parsed.cancel == cancel:
            return payload
    raise AssertionError("expected button missing from the confirmation card")


def expected_status_line(container: Container, expiry_ms: int, left: str) -> str:
    """Build the card's status line the way the renderer does."""
    stamp = user_card._date(expiry_ms, container.settings.timezone)
    return f"{user_card.STATUS_ACTIVE} · до {stamp} ({left})"


# --- load_card / render_card (§S2-3.1, §S2-3.2) ------------------------------


async def test_card_renders_header_status_traffic_and_payments(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (S2-3.2): the exact card body, payments included for an owner."""
    panel = panel_of(handler_container)
    expiry = NOW + 10 * DAY_MS + SLACK_MS
    panel.seed(USER, expiry_ms=expiry, enable=True, up=GB, down=GB)
    await panel.set_limits(USER, gb_to_bytes(10))
    await seed_user(session_factory)
    await seed_payment(session_factory, USER, 100)
    await seed_payment(session_factory, USER, 200)
    await seed_payment(session_factory, USER, 999, status=PaymentStatus.DECLINED)

    await open_card(handler_container, fake_bot)

    chat_id, message_id, text, _kwargs = fake_bot.edits[-1]
    assert (chat_id, message_id) == (CHAT, 500)
    assert text.splitlines() == [
        f"👤 <b>@neo</b> · <code>{USER}</code>",
        expected_status_line(handler_container, expiry, "ещё 10 дн."),
        "📊 ▰▰▱▱▱▱▱▱▱▱ 2.0 ГБ / 10.0 ГБ",
        "💳 Платежей: 2 · 300 ₽",
    ]
    assert toast_of(fake_bot) == ("cb-500", None, False)


async def test_card_hides_the_payments_line_without_the_permission(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (S2-3.2): a support viewer never sees the money figures."""
    await seed_staff(session_factory, SUPPORT, Role.SUPPORT)
    panel_of(handler_container).seed(USER, expiry_ms=NOW + 10 * DAY_MS, enable=True)
    await seed_user(session_factory)
    await seed_payment(session_factory, USER, 300)

    await open_card(handler_container, fake_bot, tg_id=SUPPORT)

    text = fake_bot.edits[-1][2]
    assert "💳" not in text
    assert "300" not in text


async def test_card_renders_unlimited_traffic_as_infinity(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A client with no quota shows the used bytes against ``∞`` (§S2-3.2)."""
    panel = panel_of(handler_container)
    panel.seed(USER, expiry_ms=NOW + 10 * DAY_MS, enable=True, up=GB // 2)
    await seed_user(session_factory)

    await open_card(handler_container, fake_bot)

    assert "📊 512.0 МБ / ∞" in fake_bot.edits[-1][2]


async def test_card_renders_the_perpetual_client_status(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``expiry == 0`` + enabled is «🟢 Бессрочный» (§S2-1.5 / S0-1.6)."""
    panel_of(handler_container).seed(USER, expiry_ms=0, enable=True)
    await seed_user(session_factory)

    await open_card(handler_container, fake_bot)

    assert user_card.STATUS_UNLIMITED in fake_bot.edits[-1][2]


async def test_load_card_returns_none_for_an_unknown_user(
    handler_container: Container,
) -> None:
    """AC (S2-3.1): a missing ``users`` row yields ``None`` (→ ❓ toast)."""
    assert await user_card.load_card(handler_container, USER) is None


async def test_card_of_an_unknown_user_shows_not_found(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """AC (S2-3.1): the card replies «Пользователь не найден»."""
    await open_card(handler_container, fake_bot)

    assert fake_bot.edits[-1][2] == user_card.NOT_FOUND
    assert toast_of(fake_bot)[1] is None


async def test_card_renders_with_the_panel_down(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (S2-3.1): a panel outage degrades to a warning, never an error."""
    panel = panel_of(handler_container)
    panel.seed(USER, expiry_ms=NOW + 10 * DAY_MS, enable=True)
    panel.unavailable = True
    await seed_user(session_factory)

    await open_card(handler_container, fake_bot)

    text = fake_bot.edits[-1][2]
    assert user_card.PANEL_WARNING in text
    assert user_card.NO_CLIENT in text
    assert "💳 Платежей: 0 · 0 ₽" in text


# --- card_keyboard (§S2-3.3) ------------------------------------------------


async def test_card_keyboard_lists_every_action_for_the_owner(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (S2-3.3): the full action set, every payload a valid ``AdminNav``."""
    panel = panel_of(handler_container)
    panel.seed(USER, expiry_ms=NOW + 10 * DAY_MS, enable=True)
    await seed_user(session_factory)

    await open_card(handler_container, fake_bot)

    markup = fake_bot.edits[-1][3]["reply_markup"]
    labels = labels_of(markup)
    for expected in (
        user_card.BUTTON_GRANT_7,
        user_card.BUTTON_GRANT_30,
        user_card.BUTTON_FREEZE,
        user_card.BUTTON_RESET_TRAFFIC,
        user_card.BUTTON_NEWLINK,
        user_card.BUTTON_WRITE,
        user_card.BUTTON_BAN,
        user_card.BUTTON_DELETE,
        texts.BUTTON_BACK,
    ):
        assert expected in labels

    payloads = payloads_of(markup)
    for payload in payloads:
        unpack(payload)
    assert f"adm:users:lim50:{USER}" in payloads
    assert "adm:users" in payloads  # the «⬅️ Назад» button returns to the list


async def test_card_keyboard_is_view_only_for_support(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (S2-3.3): ``support`` sees no mutation button at all."""
    await seed_staff(session_factory, SUPPORT, Role.SUPPORT)
    await seed_user(session_factory)

    await open_card(handler_container, fake_bot, tg_id=SUPPORT)

    labels = labels_of(fake_bot.edits[-1][3]["reply_markup"])
    assert labels == [texts.BUTTON_BACK]


def test_card_keyboard_marks_a_perpetual_client() -> None:
    """A grant on an unlimited client is a no-op, so the button says so."""
    markup = user_card.card_keyboard(Role.OWNER, USER, enable=True, unlimited=True)
    labels = labels_of(markup)
    assert user_card.BUTTON_GRANT_CUSTOM_UNLIMITED in labels
    assert user_card.BUTTON_BAN in labels


def test_card_keyboard_offers_the_opposite_transitions_for_a_disabled_client() -> None:
    """A disabled client shows 🔥/✅ instead of ❄️/⛔ (§S2-3.3)."""
    labels = labels_of(user_card.card_keyboard(Role.OWNER, USER, enable=False))
    assert user_card.BUTTON_UNFREEZE in labels
    assert user_card.BUTTON_UNBAN in labels
    assert user_card.BUTTON_FREEZE not in labels
    assert user_card.BUTTON_BAN not in labels


def test_card_keyboard_hides_delete_from_a_non_owner() -> None:
    """``users.delete`` is owner-only, so an ``admin`` sees no 🗑 (§S2-3.3)."""
    labels = labels_of(user_card.card_keyboard(Role.ADMIN, USER, enable=True))
    assert user_card.BUTTON_DELETE not in labels
    assert user_card.BUTTON_BAN in labels
    assert user_card.BUTTON_GRANT_7 in labels


# --- grant (§S2-3.5, §S2-3.6) -----------------------------------------------


async def test_grant_preset_extends_expiry_and_audits(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (S2-3.5): ➕ 7 д grows the expiry by a week and audits the grant."""
    panel = panel_of(handler_container)
    expiry = NOW + DAY_MS
    panel.seed(USER, expiry_ms=expiry, enable=True)
    await seed_user(session_factory)

    await admin_callback(
        callback(f"adm:users:gr7:{USER}", message_id=501), fake_bot, handler_container
    )

    fresh = await panel.get_client(USER)
    assert fresh is not None
    assert fresh.expiry_time >= expiry + 7 * DAY_MS - SLACK_MS
    assert ("user.grant_days", str(USER)) in await audit_rows(session_factory)
    assert toast_of(fake_bot) == (
        "cb-501",
        user_card.GRANT_DONE.format(days=7),
        False,
    )
    # The card is re-rendered in place, so the new expiry is already visible.
    assert fake_bot.edits[-1][1] == 501


async def test_grant_on_a_perpetual_client_reports_no_change(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (S2-3.5): ``UNLIMITED_REASON`` is handled, the client is untouched."""
    panel = panel_of(handler_container)
    panel.seed(USER, expiry_ms=0, enable=True)
    await seed_user(session_factory)

    await admin_callback(
        callback(f"adm:users:gr30:{USER}", message_id=501),
        fake_bot,
        handler_container,
    )

    fresh = await panel.get_client(USER)
    assert fresh is not None and fresh.expiry_time == 0
    assert toast_of(fake_bot)[1] == user_card.GRANT_UNLIMITED
    assert toast_of(fake_bot)[2] is True  # shown as an alert


async def test_grant_is_denied_for_support(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (S2-3.5): a forged grant by ``support`` is refused, panel untouched."""
    panel = panel_of(handler_container)
    panel.seed(USER, expiry_ms=NOW + DAY_MS, enable=True)
    await seed_staff(session_factory, SUPPORT, Role.SUPPORT)
    await seed_user(session_factory)

    await admin_callback(
        callback(f"adm:users:gr30:{USER}", tg_id=SUPPORT),
        fake_bot,
        handler_container,
    )

    fresh = await panel.get_client(USER)
    assert fresh is not None and fresh.expiry_time == NOW + DAY_MS
    assert "mutate" not in panel.calls
    assert fake_bot.edits == []  # nothing re-rendered either
    assert toast_of(fake_bot) == ("cb-500", "Недостаточно прав", True)
    assert await audit_rows(session_factory) == []


async def test_custom_grant_prompts_then_applies_the_typed_number(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (S2-3.6): the prompt carries «✖️ Отмена» and the reply grants."""
    panel = panel_of(handler_container)
    panel.seed(USER, expiry_ms=NOW + DAY_MS, enable=True)
    await seed_user(session_factory)

    await admin_callback(
        callback(f"adm:users:grc:{USER}", message_id=501),
        fake_bot,
        handler_container,
    )
    (chat_id, message_id, text, kwargs) = fake_bot.edits[-1]
    assert (chat_id, message_id, text) == (CHAT, 501, user_card.GRANT_PROMPT)
    assert payloads_of(kwargs["reply_markup"]) == [f"adm:users:card:{USER}"]
    assert await fake_bot.get_state(OWNER, CHAT) == UserStates.admin_grant.name

    await user_card.admin_grant_message(
        message(OWNER, "5"), fake_bot, handler_container
    )

    fresh = await panel.get_client(USER)
    assert fresh is not None
    assert fresh.expiry_time >= NOW + 6 * DAY_MS - SLACK_MS
    assert await fake_bot.get_state(OWNER, CHAT) is None
    assert ("user.grant_days", str(USER)) in await audit_rows(session_factory)
    # The prompt message turned into the refreshed card.
    assert fake_bot.edits[-1][1] == 700
    assert fake_bot.messages[-1][1] == user_card.GRANT_DONE.format(days=5)


async def test_custom_grant_rejects_a_non_numeric_reply(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (S2-3.6): «abc» is rejected, nothing is granted, state cleared."""
    panel = panel_of(handler_container)
    panel.seed(USER, expiry_ms=NOW + DAY_MS, enable=True)
    await seed_user(session_factory)
    await admin_callback(callback(f"adm:users:grc:{USER}"), fake_bot, handler_container)

    await user_card.admin_grant_message(
        message(OWNER, "abc"), fake_bot, handler_container
    )

    assert fake_bot.messages[-1][1] == user_card.GRANT_INVALID
    assert await fake_bot.get_state(OWNER, CHAT) is None
    assert "mutate" not in panel.calls


async def test_custom_grant_cancel_reopens_the_card_and_clears_the_state(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (S2-3.6): the prompt's escape button restores the card, state gone."""
    panel = panel_of(handler_container)
    panel.seed(USER, expiry_ms=NOW + 10 * DAY_MS, enable=True)
    await seed_user(session_factory)
    await admin_callback(
        callback(f"adm:users:grc:{USER}", message_id=501),
        fake_bot,
        handler_container,
    )
    assert await fake_bot.get_state(OWNER, CHAT) == UserStates.admin_grant.name

    await admin_callback(
        callback(f"adm:users:card:{USER}", message_id=501),
        fake_bot,
        handler_container,
    )

    assert await fake_bot.get_state(OWNER, CHAT) is None
    assert f"👤 <b>@neo</b> · <code>{USER}</code>" in fake_bot.edits[-1][2]
    assert toast_of(fake_bot) == ("cb-501", None, False)


# --- freeze / limits / reset (§S2-3.7, §S2-3.8, §S2-3.9) --------------------


async def test_freeze_disables_the_client_and_audits(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (S2-3.7): ❄️ flips ``enable`` to ``False`` and writes an audit row."""
    panel = panel_of(handler_container)
    panel.seed(USER, expiry_ms=NOW + 10 * DAY_MS, enable=True)
    await seed_user(session_factory)

    await admin_callback(
        callback(f"adm:users:fz:{USER}", message_id=501),
        fake_bot,
        handler_container,
    )

    fresh = await panel.get_client(USER)
    assert fresh is not None and fresh.enable is False
    assert ("user.freeze", str(USER)) in await audit_rows(session_factory)
    assert toast_of(fake_bot)[1] == user_card.FROZEN_DONE


async def test_unfreeze_enables_the_client_and_audits(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (S2-3.7): 🔥 re-enables the client and writes its own audit row."""
    panel = panel_of(handler_container)
    panel.seed(USER, expiry_ms=NOW + 10 * DAY_MS, enable=False)
    await seed_user(session_factory)

    await admin_callback(
        callback(f"adm:users:uf:{USER}", message_id=501),
        fake_bot,
        handler_container,
    )

    fresh = await panel.get_client(USER)
    assert fresh is not None and fresh.enable is True
    assert ("user.unfreeze", str(USER)) in await audit_rows(session_factory)


async def test_limit_preset_writes_bytes_and_audits(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (S2-3.8): 📊 50 ГБ stores ``gb_to_bytes(50)`` on the panel."""
    panel = panel_of(handler_container)
    panel.seed(USER, expiry_ms=NOW + 10 * DAY_MS, enable=True)
    await seed_user(session_factory)

    await admin_callback(
        callback(f"adm:users:lim50:{USER}", message_id=501),
        fake_bot,
        handler_container,
    )

    traffic = await panel.get_traffic(USER)
    assert traffic is not None and traffic.total == gb_to_bytes(50)
    assert ("user.limits", str(USER)) in await audit_rows(session_factory)
    assert toast_of(fake_bot)[1] == user_card.LIMIT_DONE
    assert "50.0 ГБ" in fake_bot.edits[-1][2]  # the re-rendered bar


async def test_limit_infinity_clears_the_quota(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (S2-3.8): ``∞`` becomes ``0`` on the panel (no quota)."""
    panel = panel_of(handler_container)
    panel.seed(USER, expiry_ms=NOW + 10 * DAY_MS, enable=True)
    await panel.set_limits(USER, gb_to_bytes(50))
    await seed_user(session_factory)

    await admin_callback(
        callback(f"adm:users:liminf:{USER}", message_id=501),
        fake_bot,
        handler_container,
    )

    traffic = await panel.get_traffic(USER)
    assert traffic is not None and traffic.total == 0


async def test_reset_traffic_zeroes_the_counters(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (S2-3.9): ♻️ zeroes up/down and audits the reset."""
    panel = panel_of(handler_container)
    panel.seed(USER, expiry_ms=NOW + 10 * DAY_MS, enable=True, up=GB, down=GB)
    await seed_user(session_factory)

    await admin_callback(
        callback(f"adm:users:rt:{USER}", message_id=501),
        fake_bot,
        handler_container,
    )

    traffic = await panel.get_traffic(USER)
    assert traffic is not None and traffic.used == 0
    assert ("user.reset_traffic", str(USER)) in await audit_rows(session_factory)
    assert toast_of(fake_bot)[1] == user_card.TRAFFIC_RESET_DONE


async def test_limit_is_denied_for_support(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (S2-3.8): a forged quota change by ``support`` never reaches the panel."""
    panel = panel_of(handler_container)
    panel.seed(USER, expiry_ms=NOW + 10 * DAY_MS, enable=True)
    await seed_staff(session_factory, SUPPORT, Role.SUPPORT)
    await seed_user(session_factory)

    await admin_callback(
        callback(f"adm:users:lim50:{USER}", tg_id=SUPPORT),
        fake_bot,
        handler_container,
    )

    assert "set_limits" not in panel.calls
    assert toast_of(fake_bot) == ("cb-500", "Недостаточно прав", True)


# --- new link (§S2-3.10) ----------------------------------------------------


async def test_newlink_asks_first_and_nothing_is_written_yet(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (S2-3.10): the old ``sub_id`` survives until «✅ Подтвердить»."""
    panel = panel_of(handler_container)
    panel.seed(USER, expiry_ms=NOW + 10 * DAY_MS, enable=True, sub_id="abc")
    await seed_user(session_factory)

    await admin_callback(
        callback(f"adm:users:nl:{USER}", message_id=501),
        fake_bot,
        handler_container,
    )

    (chat_id, message_id, text, kwargs) = fake_bot.edits[-1]
    assert (chat_id, message_id, text) == (CHAT, 501, user_card.NEWLINK_CONFIRM)
    confirm, cancel = (
        confirm_payload(fake_bot, cancel=False),
        confirm_payload(fake_bot, cancel=True),
    )
    confirmed, cancelled = unpack(confirm), unpack(cancel)
    assert isinstance(confirmed, Confirm) and isinstance(cancelled, Confirm)
    assert confirmed.token == cancelled.token  # one token, two buttons

    assert "regenerate" not in panel.calls
    assert (await panel.get_client(USER)).sub_id == "abc"


async def test_newlink_confirm_regenerates_and_audits(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (S2-3.10): «✅ Подтвердить» mints a new ``sub_id`` + audit row."""
    panel = panel_of(handler_container)
    panel.seed(USER, expiry_ms=NOW + 10 * DAY_MS, enable=True, sub_id="abc")
    await seed_user(session_factory)
    await admin_callback(
        callback(f"adm:users:nl:{USER}", message_id=501),
        fake_bot,
        handler_container,
    )

    await confirm_callback(
        callback(confirm_payload(fake_bot, cancel=False), message_id=501),
        fake_bot,
        handler_container,
    )

    fresh = await panel.get_client(USER)
    assert fresh is not None and fresh.sub_id not in (None, "", "abc")
    assert ("user.sub_regenerate", str(USER)) in await audit_rows(session_factory)
    assert toast_of(fake_bot)[1] == user_card.NEWLINK_DONE


async def test_newlink_cancel_keeps_the_old_link(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (S2-3.10): «❌ Отмена» burns the token and touches nothing."""
    panel = panel_of(handler_container)
    panel.seed(USER, expiry_ms=NOW + 10 * DAY_MS, enable=True, sub_id="abc")
    await seed_user(session_factory)
    await admin_callback(
        callback(f"adm:users:nl:{USER}", message_id=501),
        fake_bot,
        handler_container,
    )

    await confirm_callback(
        callback(confirm_payload(fake_bot, cancel=True), message_id=501),
        fake_bot,
        handler_container,
    )

    assert (await panel.get_client(USER)).sub_id == "abc"
    assert "regenerate" not in panel.calls
    assert await audit_rows(session_factory) == []


async def test_newlink_confirm_is_denied_after_the_role_is_revoked(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (S2-3.10): the confirmation re-checks ``users.edit`` (forged by support)."""
    panel = panel_of(handler_container)
    panel.seed(USER, expiry_ms=NOW + 10 * DAY_MS, enable=True)
    await seed_staff(session_factory, SUPPORT, Role.SUPPORT)
    await seed_user(session_factory)
    token = handler_container.confirmations.create(  # pyright: ignore[union-attr]
        SUPPORT, user_card.ACTION_SUB_REGENERATE, USER
    )

    await confirm_callback(
        callback(Confirm(token=token).pack(), tg_id=SUPPORT),
        fake_bot,
        handler_container,
    )

    assert "regenerate" not in panel.calls
    assert toast_of(fake_bot) == ("cb-500", "Недостаточно прав", True)


# --- write to user (§S2-3.11) -----------------------------------------------


async def test_write_to_user_reaches_the_target_via_safe_send(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (S2-3.11): «✉️ Написать» reuses the support relay's FSM state."""
    await seed_user(session_factory)

    await admin_callback(
        callback(f"adm:users:wr:{USER}", message_id=501),
        fake_bot,
        handler_container,
    )

    assert await fake_bot.get_state(OWNER, CHAT) == UserStates.writing_to_user.name
    async with fake_bot.retrieve_data(OWNER, CHAT) as data:
        assert data["target_user_id"] == USER
    assert fake_bot.edits[-1][1] == 501
    assert ("user.write", str(USER)) in await audit_rows(session_factory)

    await support_relay(message(OWNER, "привет"), fake_bot, handler_container)

    assert fake_bot.texts_to(USER)  # delivered by Notifier.safe_send
    assert "привет" in fake_bot.texts_to(USER)[-1]


# --- ban / unban (§S2-3.12) -------------------------------------------------


async def test_ban_disables_the_client_and_notices_the_user(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (S2-3.12): ⛔ blocks the row, disables the client, audits, notifies."""
    panel = panel_of(handler_container)
    panel.seed(USER, expiry_ms=NOW + 10 * DAY_MS, enable=True)
    await seed_user(session_factory)

    await admin_callback(
        callback(f"adm:users:ban:{USER}", message_id=501),
        fake_bot,
        handler_container,
    )

    fresh = await panel.get_client(USER)
    assert fresh is not None and fresh.enable is False
    assert (await status_of(session_factory))[0] == UserStatus.BLOCKED
    assert ("user.ban", str(USER)) in await audit_rows(session_factory)
    assert texts.BAN_USER_NOTICE in fake_bot.texts_to(USER)
    assert toast_of(fake_bot)[1] == user_card.BAN_DONE_TOAST


async def test_unban_restores_the_user(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (S2-3.12): ✅ clears the block and re-enables a live subscription."""
    panel = panel_of(handler_container)
    panel.seed(USER, expiry_ms=NOW + 10 * DAY_MS, enable=False)
    await seed_user(session_factory, status=UserStatus.BLOCKED)

    await admin_callback(
        callback(f"adm:users:unban:{USER}", message_id=501),
        fake_bot,
        handler_container,
    )

    fresh = await panel.get_client(USER)
    assert fresh is not None and fresh.enable is True
    assert (await status_of(session_factory))[0] == UserStatus.APPROVED
    assert ("user.unban", str(USER)) in await audit_rows(session_factory)
    assert texts.UNBAN_USER_NOTICE in fake_bot.texts_to(USER)


async def test_ban_is_denied_for_an_edit_only_role(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC (S2-3.12): ⛔ re-checks ``users.ban``, not ``users.edit``."""
    panel = panel_of(handler_container)
    panel.seed(USER, expiry_ms=NOW + 10 * DAY_MS, enable=True)
    await seed_staff(session_factory, EDITOR, Role.ADMIN)
    await seed_user(session_factory)
    monkeypatch.setitem(
        permissions.ROLE_PERMISSIONS,
        Role.ADMIN,
        frozenset({Permission.USERS_VIEW, Permission.USERS_EDIT}),
    )

    await admin_callback(
        callback(f"adm:users:ban:{USER}", tg_id=EDITOR),
        fake_bot,
        handler_container,
    )

    fresh = await panel.get_client(USER)
    assert fresh is not None and fresh.enable is True
    assert (await status_of(session_factory))[0] == UserStatus.APPROVED
    assert toast_of(fake_bot) == ("cb-500", "Недостаточно прав", True)


# --- delete (§S2-3.13) ------------------------------------------------------


async def test_delete_confirmation_removes_the_client_and_keeps_history(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (S2-3.13): 🗑 deletes the client, flips the row, keeps the payments."""
    panel = panel_of(handler_container)
    panel.seed(USER, expiry_ms=NOW + 10 * DAY_MS, enable=True)
    await seed_user(session_factory)
    await seed_payment(session_factory, USER, 500)

    await admin_callback(
        callback(f"adm:users:del:{USER}", message_id=501),
        fake_bot,
        handler_container,
    )
    assert fake_bot.edits[-1][2] == user_card.DELETE_CONFIRM.format(tg_id=USER)
    assert "delete_client" not in panel.calls  # nothing written yet

    await confirm_callback(
        callback(confirm_payload(fake_bot, cancel=False), message_id=501),
        fake_bot,
        handler_container,
    )

    assert await panel.get_client(USER) is None
    status, note = await status_of(session_factory)
    assert status == UserStatus.REJECTED and note == user_card.DELETED_NOTE
    assert ("user.delete", str(USER)) in await audit_rows(session_factory)
    # The payments survive, and the card's message falls back to the list.
    async with session_factory() as session:
        assert await payments_repo.user_totals(session, USER) == (1, 500)
    assert "Пользователи" in fake_bot.edits[-1][2]
    assert toast_of(fake_bot)[1] == user_card.DELETE_DONE


async def test_delete_cancel_keeps_the_client(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (S2-3.13): «❌ Отмена» leaves the client and the row untouched."""
    panel = panel_of(handler_container)
    panel.seed(USER, expiry_ms=NOW + 10 * DAY_MS, enable=True)
    await seed_user(session_factory)
    await admin_callback(
        callback(f"adm:users:del:{USER}", message_id=501),
        fake_bot,
        handler_container,
    )

    await confirm_callback(
        callback(confirm_payload(fake_bot, cancel=True), message_id=501),
        fake_bot,
        handler_container,
    )

    assert await panel.get_client(USER) is not None
    assert (await status_of(session_factory))[0] == UserStatus.APPROVED
    assert await audit_rows(session_factory) == []


async def test_delete_is_denied_for_a_non_owner(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (S2-3.13): only ``users.delete`` (owner) may delete a client."""
    panel = panel_of(handler_container)
    panel.seed(USER, expiry_ms=NOW + 10 * DAY_MS, enable=True)
    await seed_staff(session_factory, EDITOR, Role.ADMIN)
    await seed_user(session_factory)
    token = handler_container.confirmations.create(  # pyright: ignore[union-attr]
        EDITOR, user_card.ACTION_DELETE, USER
    )

    await confirm_callback(
        callback(Confirm(token=token).pack(), tg_id=EDITOR),
        fake_bot,
        handler_container,
    )

    assert "delete_client" not in panel.calls
    assert await panel.get_client(USER) is not None
    assert (await status_of(session_factory))[0] == UserStatus.APPROVED
    assert toast_of(fake_bot) == ("cb-500", "Недостаточно прав", True)


async def test_delete_is_denied_for_a_support_user(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A forged 🗑 from ``support`` is refused before the confirm card."""
    panel = panel_of(handler_container)
    panel.seed(USER, expiry_ms=NOW + 10 * DAY_MS, enable=True)
    await seed_staff(session_factory, SUPPORT, Role.SUPPORT)
    await seed_user(session_factory)

    await admin_callback(
        callback(f"adm:users:del:{USER}", tg_id=SUPPORT),
        fake_bot,
        handler_container,
    )

    assert fake_bot.edits == []
    assert await panel.get_client(USER) is not None


# --- malformed payloads (§S2-3.4) -------------------------------------------


async def test_forged_card_payload_is_answered_with_a_toast(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """AC (S2-3.4): a crafted ``adm:users:card`` argument renders nothing."""
    for data in ("adm:users:card", "adm:users:card:abc"):
        await admin_callback(
            callback(data, message_id=502), fake_bot, handler_container
        )

    assert fake_bot.edits == []
    assert toast_of(fake_bot) == (
        "cb-502",
        texts.ERROR_STALE_BUTTON,
        False,
    )
