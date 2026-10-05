"""Payments section tests (``TASK_PLAN.md`` §S2-4).

Covers the S2-4 acceptance criteria: the repository helpers list every
``PENDING_STATUSES`` row (``awaiting_proof`` included) newest-first and count
only *approved and applied* revenue, the stats card's four windows and its 7-day
chart match a fixture with declined/revoked rows excluded, the section home
counts the queue, a queue row opens the standard approve/decline card, and the
history page paginates newest-first.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.callbacks import unpack
from app.container import Container
from app.db.models import Admin, Payment, PaymentStatus, UserStatus
from app.db.repositories import payments as payments_repo
from app.db.repositories import users as users_repo
from app.db.repositories.payments import PENDING_STATUSES
from app.handlers.admin import payments as payments_screen
from app.handlers.admin.nav import admin_callback
from app.permissions import Role
from tests.fakes import FakeBot

OWNER = 1
SUPPORT = 42
CHAT = 99
#: Fixed wall clock so the calendar windows are deterministic (naive UTC).
NOW = datetime(2026, 10, 5, 12, 0, 0)
STAMP = datetime(2026, 10, 5, 8, 0, 0)


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


async def seed_staff(
    factory: async_sessionmaker[AsyncSession], tg_id: int, role: Role
) -> None:
    """Grant ``tg_id`` a non-owner role from the ``admins`` table."""
    async with factory() as session:
        session.add(Admin(tg_id=tg_id, role=str(role), added_by=OWNER))
        await session.commit()


async def seed_payment(
    factory: async_sessionmaker[AsyncSession],
    *,
    tg_id: int,
    price: int = 150,
    status: PaymentStatus = PaymentStatus.SUBMITTED,
    name: str = "Месяц",
    applied_at: datetime | None = None,
    decided_at: datetime | None = None,
) -> int:
    """Insert one payment snapshot and return its id."""
    async with factory() as session:
        payment = Payment(
            user_tg_id=tg_id,
            tariff_name=name,
            days=30,
            price=price,
            status=str(status),
        )
        await payments_repo.add(session, payment)
        payment.applied_at = applied_at
        payment.decided_at = decided_at
        await session.commit()
        return int(payment.id)


# --- repository helpers (§S2-4.1) -------------------------------------------


async def test_list_by_statuses_is_newest_first_and_includes_awaiting_proof(
    session: AsyncSession,
) -> None:
    """AC: the queue read covers every ``PENDING_STATUSES`` row (§S2-4.3)."""
    for tg_id in (7, 8, 9):
        await users_repo.upsert_from_telegram(session, tg_id)
    await session.commit()
    for tg_id, status, price in (
        (7, PaymentStatus.SUBMITTED, 100),
        (8, PaymentStatus.AWAITING_PROOF, 200),
        (9, PaymentStatus.APPROVED, 300),
    ):
        await payments_repo.add(
            session,
            Payment(
                user_tg_id=tg_id,
                tariff_name="t",
                days=30,
                price=price,
                status=str(status),
            ),
        )
    await session.commit()

    pending = await payments_repo.list_by_statuses(session, PENDING_STATUSES)
    assert [payment.price for payment in pending] == [200, 100]

    decided = await payments_repo.list_by_statuses(
        session, payments_screen.HISTORY_STATUSES
    )
    assert [payment.price for payment in decided] == [300]


async def test_stats_counts_only_approved_and_applied_since(
    session: AsyncSession,
) -> None:
    """AC: declined/revoked and never-applied rows stay out of the figures."""
    await users_repo.upsert_from_telegram(session, 7)
    await session.commit()
    since = datetime(2026, 10, 1, 0, 0, 0)
    for status, price, applied in (
        (PaymentStatus.APPROVED, 150, datetime(2026, 10, 4, 9, 0, 0)),
        (PaymentStatus.APPROVED, 250, datetime(2026, 9, 30, 9, 0, 0)),
        (PaymentStatus.DECLINED, 999, datetime(2026, 10, 4, 9, 0, 0)),
        (PaymentStatus.REVOKED, 999, datetime(2026, 10, 4, 9, 0, 0)),
        (PaymentStatus.APPROVED, 999, None),
    ):
        payment = Payment(
            user_tg_id=7,
            tariff_name="t",
            days=30,
            price=price,
            status=str(status),
        )
        await payments_repo.add(session, payment)
        payment.applied_at = applied
    await session.commit()

    assert await payments_repo.stats(session, since=since) == (1, 150)
    assert await payments_repo.stats(session, since=datetime(2026, 1, 1)) == (2, 400)


# --- stats (§S2-4.5) --------------------------------------------------------


async def test_collect_stats_windows_and_chart(
    handler_container: Container,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC: the windows and the chart match a fixture; revoked/declined excluded."""
    for tg_id in (101, 102, 103, 104, 105, 106):
        await seed_user(session_factory, tg_id)
    await seed_payment(
        session_factory,
        tg_id=101,
        price=100,
        status=PaymentStatus.APPROVED,
        applied_at=datetime(2026, 10, 5, 8, 0, 0),  # today
    )
    await seed_payment(
        session_factory,
        tg_id=102,
        price=200,
        status=PaymentStatus.APPROVED,
        applied_at=datetime(2026, 10, 2, 9, 0, 0),  # three days ago
    )
    await seed_payment(
        session_factory,
        tg_id=103,
        price=400,
        status=PaymentStatus.APPROVED,
        applied_at=datetime(2026, 9, 25, 9, 0, 0),  # inside 30 d, outside 7 d
    )
    await seed_payment(
        session_factory,
        tg_id=104,
        price=800,
        status=PaymentStatus.APPROVED,
        applied_at=datetime(2026, 8, 26, 9, 0, 0),  # only «Всего»
    )
    await seed_payment(
        session_factory,
        tg_id=105,
        price=999,
        status=PaymentStatus.DECLINED,
        applied_at=datetime(2026, 10, 1, 9, 0, 0),
    )
    await seed_payment(
        session_factory,
        tg_id=106,
        price=999,
        status=PaymentStatus.REVOKED,
        applied_at=datetime(2026, 10, 1, 9, 0, 0),
    )

    stats = await payments_screen.collect_stats(handler_container, now=NOW)

    assert [(w.label, w.count, w.revenue) for w in stats.windows] == [
        ("Сегодня", 1, 100),
        ("7 дней", 2, 300),
        ("30 дней", 3, 700),
        ("Всего", 4, 1500),
    ]
    # One line per day, oldest first: 29.09 … 05.10.
    assert stats.days == (
        ("29.09", 0),
        ("30.09", 0),
        ("01.10", 0),
        ("02.10", 200),
        ("03.10", 0),
        ("04.10", 0),
        ("05.10", 100),
    )
    # The bars are the «7 дней» window, so they add up to that figure.
    assert sum(revenue for _, revenue in stats.days) == 300


def test_render_stats_draws_one_bar_per_day() -> None:
    """The chart is ``bar()`` per day, scaled against the busiest day."""
    stats = payments_screen.Stats(
        windows=(
            payments_screen.WindowStats("Сегодня", 1, 100),
            payments_screen.WindowStats("7 дней", 2, 300),
            payments_screen.WindowStats("30 дней", 3, 700),
            payments_screen.WindowStats("Всего", 4, 1500),
        ),
        days=(
            ("30.09", 0),
            ("01.10", 0),
            ("02.10", 200),
            ("03.10", 0),
            ("04.10", 0),
            ("05.10", 100),
        ),
    )

    text = payments_screen.render_stats(stats)

    assert "Всего: 4 · 1500 ₽" in text
    assert "Сегодня: 1 · 100 ₽" in text
    assert "02.10 ▰▰▰▰▰▰▰▰▰▰ 200 ₽" in text  # the peak day fills the bar
    assert "05.10 ▰▰▰▰▰▱▱▱▱▱ 100 ₽" in text  # half the peak → half the bar
    assert "30.09 ▱▱▱▱▱▱▱▱▱▱ 0 ₽" in text  # a quiet day is empty
    # One line per day, oldest first, each carrying a bar.
    day_lines = text.splitlines()[-6:]
    assert [row[:5] for row in day_lines] == [
        "30.09",
        "01.10",
        "02.10",
        "03.10",
        "04.10",
        "05.10",
    ]
    assert all(("▰" in row or "▱" in row) for row in day_lines)


# --- section home (§S2-4.2) -------------------------------------------------


async def test_payments_home_counts_the_queue_and_parseable_buttons(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC: N counts ``PENDING_STATUSES``; every section button is a payload."""
    await seed_user(session_factory, 101)
    await seed_user(session_factory, 102)
    await seed_payment(session_factory, tg_id=101, status=PaymentStatus.AWAITING_PROOF)
    await seed_payment(session_factory, tg_id=102, status=PaymentStatus.APPROVED)

    await admin_callback(callback("adm:payments"), fake_bot, handler_container)

    text = fake_bot.edits[-1][2]
    markup = fake_bot.edits[-1][3]["reply_markup"]
    assert "В очереди: 1" in text
    assert "⏳ Ожидают (1)" in labels_of(markup)
    assert "📜 История" in labels_of(markup)
    assert "📈 Статистика" in labels_of(markup)
    assert payloads_of(markup) == [
        "adm:payments:p:0",
        "adm:payments:h:0",
        "adm:payments:st",
        "adm:menu",
    ]
    for payload in payloads_of(markup):
        unpack(payload)


# --- pending queue (§S2-4.3) ------------------------------------------------


async def test_pending_row_opens_the_standard_review_card(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC: an ``awaiting_proof`` row is listed and opens the approve/decline card."""
    await seed_user(session_factory, 101, username="neo")
    payment_id = await seed_payment(
        session_factory,
        tg_id=101,
        status=PaymentStatus.AWAITING_PROOF,
        name="Месяц",
    )

    await admin_callback(callback("adm:payments:p:0"), fake_bot, handler_container)

    text = fake_bot.edits[-1][2]
    markup = fake_bot.edits[-1][3]["reply_markup"]
    assert "Ожидают проверки" in text
    assert f"#{payment_id} · Месяц · @neo" in labels_of(markup)

    await admin_callback(
        callback(f"adm:payments:c:{payment_id}", message_id=501),
        fake_bot,
        handler_container,
    )

    chat_id, card_text, kwargs = fake_bot.messages[-1]
    assert chat_id == CHAT
    assert "Новая заявка на оплату" in card_text
    assert payloads_of(kwargs["reply_markup"]) == [
        f"pay:ok:{payment_id}",
        f"pay:no:{payment_id}",
    ]


# --- history (§S2-4.4) ------------------------------------------------------


async def test_history_paginates_newest_first(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC: newest first, `#id · ₽ · @user · icon date` rows, correct pages."""
    await seed_user(session_factory, 101, username="neo")
    ids = []
    for index in range(10):
        ids.append(
            await seed_payment(
                session_factory,
                tg_id=101,
                price=100 + index,
                status=PaymentStatus.APPROVED,
                applied_at=STAMP,
                decided_at=STAMP,
            )
        )

    await admin_callback(callback("adm:payments:h:0"), fake_bot, handler_container)

    text = fake_bot.edits[-1][2]
    markup = fake_bot.edits[-1][3]["reply_markup"]
    rows = [label for label in labels_of(markup) if label.startswith("#")]
    assert "стр. 1/2" in text
    assert len(rows) == 8
    assert rows[0] == f"#{ids[-1]} · 109 ₽ · @neo · ✅ 05.10"
    # Rows are informational: each one parses and points back at its own page.
    assert payloads_of(markup)[:8] == ["adm:payments:h:0"] * 8
    for payload in payloads_of(markup):
        unpack(payload)

    await admin_callback(
        callback("adm:payments:h:1", message_id=502), fake_bot, handler_container
    )

    page_two = [
        label
        for label in labels_of(fake_bot.edits[-1][3]["reply_markup"])
        if label.startswith("#")
    ]
    assert "стр. 2/2" in fake_bot.edits[-1][2]
    assert len(page_two) == 2


async def test_payments_screen_denies_support(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``support`` has ``users.view`` but not ``payments.view``."""
    await seed_staff(session_factory, SUPPORT, Role.SUPPORT)

    await admin_callback(
        callback("adm:payments", tg_id=SUPPORT), fake_bot, handler_container
    )

    assert fake_bot.edits == []
    assert fake_bot.messages == []
    assert fake_bot.callback_answers[-1] == ("cb-500", "Недостаточно прав", True)
