"""``/admin`` dashboard + navigation tests (``TASK_PLAN.md`` §S2-1).

Covers the S2-1 acceptance criteria: the home card collects its counters from
separate DB reads (dropping a source that fails), the keyboard is filtered by
role *and* by the registered screens, ``/admin`` is staff-only and clears a
half-finished FSM prompt, and a forged ``adm:`` callback is answered with a
toast instead of a render.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import texts
from app.callbacks import AdminNav
from app.container import Container
from app.db.base import utcnow
from app.db.models import Admin, Payment, PaymentStatus, UserStatus
from app.db.repositories import payments as payments_repo
from app.db.repositories import users as users_repo
from app.handlers.admin import home
from app.handlers.admin import nav as admin_nav
from app.handlers.admin.nav import admin_callback
from app.handlers.start import menu_keyboard, start_command
from app.permissions import Role
from tests.fakes import FakeBot

OWNER = 1
SUPPORT = 42
STRANGER = 777
CHAT = 99


def message(tg_id: int) -> SimpleNamespace:
    """Minimal Telegram message: only the fields the handlers read."""
    return SimpleNamespace(
        from_user=SimpleNamespace(id=tg_id, username="neo", first_name="Neo"),
        chat=SimpleNamespace(id=CHAT),
        text="/admin",
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


async def seed_payment(
    factory: async_sessionmaker[AsyncSession],
    *,
    tg_id: int,
    price: int,
    status: PaymentStatus,
    applied: bool = False,
) -> None:
    """Insert a payment snapshot; ``applied`` stamps ``applied_at`` (revenue)."""
    async with factory() as session:
        payment = Payment(
            user_tg_id=tg_id,
            tariff_name="Месяц",
            days=30,
            price=price,
            status=str(status),
        )
        await payments_repo.add(session, payment)
        if applied:
            payment.applied_at = utcnow()
        await session.commit()


async def seed_support(factory: async_sessionmaker[AsyncSession], tg_id: int) -> None:
    """Grant ``tg_id`` the ``support`` role (``users.view`` only)."""
    async with factory() as session:
        session.add(Admin(tg_id=tg_id, role="support", added_by=OWNER))
        await session.commit()


# --- dashboard counters (§S2-1.5) --------------------------------------------


async def test_dashboard_counts_reads_the_db(
    handler_container: Container,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await seed_user(session_factory, 10)
    await seed_user(session_factory, 11)
    await seed_user(session_factory, 12, UserStatus.BLOCKED)
    await seed_payment(
        session_factory, tg_id=10, price=100, status=PaymentStatus.SUBMITTED
    )
    await seed_payment(
        session_factory,
        tg_id=11,
        price=250,
        status=PaymentStatus.APPROVED,
        applied=True,
    )
    await seed_payment(
        session_factory, tg_id=12, price=999, status=PaymentStatus.REVOKED
    )

    counts = await home.dashboard_counts(handler_container)

    assert counts["users"] == 3
    assert counts["approved"] == 2
    assert counts["blocked"] == 1
    assert counts["pending"] == 0
    assert counts["queued_payments"] == 1
    # Revenue counts approved+applied only: the revoked 999 is excluded.
    assert counts["revenue_30d"] == 250


async def test_dashboard_counts_drops_a_failing_source(
    handler_container: Container,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC: a read that raises is omitted, never propagated."""

    async def boom(*args: object, **kwargs: object) -> int:
        raise RuntimeError("db down")

    monkeypatch.setattr(payments_repo, "count_by_statuses", boom)

    counts = await home.dashboard_counts(handler_container)

    assert "queued_payments" not in counts
    assert "users" in counts  # the other reads still ran


# --- dashboard rendering (§S2-1.6) -------------------------------------------


def test_render_dashboard_lines_and_outage() -> None:
    counts = {
        "users": 5,
        "approved": 3,
        "blocked": 1,
        "pending": 1,
        "queued_payments": 2,
        "revenue_30d": 1500,
    }
    text = home.render_dashboard(counts, server_ok=True, online=4, maintenance=False)

    assert "Пользователей: 5" in text
    assert "В очереди платежей: 2" in text
    assert "1500 ₽" in text
    assert "✅ Xray работает · онлайн: 4" in text
    assert "Тех. работы: выкл" in text

    outage = home.render_dashboard({}, server_ok=False, online=None, maintenance=True)

    assert "Пользователей: 0" in outage
    assert "⚠️ панель недоступна" in outage
    assert "Тех. работы: вкл" in outage


# --- dashboard keyboard (§S2-1.7) --------------------------------------------


def test_dashboard_keyboard_filters_by_role_and_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A section shows only when the permission matches *and* its screen exists."""

    async def noop(*args: object, **kwargs: object) -> None: ...

    monkeypatch.setitem(admin_nav.SCREENS, "users", noop)
    monkeypatch.setitem(admin_nav.SCREENS, "settings", noop)

    owner = home.dashboard_keyboard(Role.OWNER)
    admin = home.dashboard_keyboard(Role.ADMIN)
    support = home.dashboard_keyboard(Role.SUPPORT)
    stranger = home.dashboard_keyboard(None)

    assert labels_of(owner) == [
        "👥 Пользователи",
        "💳 Платежи",
        "⚙️ Настройки",
        "🔄 Обновить",
    ]
    # ``admin`` keeps ``users.view``/``payments.view`` but not ``settings.edit``.
    assert labels_of(admin) == ["👥 Пользователи", "💳 Платежи", "🔄 Обновить"]
    assert labels_of(support) == ["👥 Пользователи", "🔄 Обновить"]
    assert labels_of(stranger) == ["🔄 Обновить"]
    assert payloads_of(owner)[-1] == "adm:menu"
    # ``server``/``broadcast`` are unregistered here → no dead button.
    assert "adm:payments" in payloads_of(owner)
    assert "adm:server" not in payloads_of(owner)


# --- /admin command (§S2-1.8) ------------------------------------------------


async def test_admin_command_is_staff_only(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """Non-staff callers are dropped silently, as the legacy guards were."""
    await home.admin_command(message(STRANGER), fake_bot, handler_container)

    assert fake_bot.messages == []
    assert fake_bot.callback_answers == []


async def test_admin_command_renders_and_clears_fsm(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """/admin drops a half-finished prompt and renders the dashboard (§S2-1.8)."""
    await fake_bot.set_state(OWNER, "admin_search", CHAT)

    await home.admin_command(message(OWNER), fake_bot, handler_container)

    assert await fake_bot.get_state(OWNER, CHAT) is None
    assert len(fake_bot.messages) == 1
    assert "Панель администратора" in fake_bot.messages[-1][1]


# --- admin callback dispatch (§S2-1.9, §S2-1.10) -----------------------------


async def test_admin_callback_menu_renders_the_dashboard(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    await admin_callback(callback("adm:menu"), fake_bot, handler_container)

    assert fake_bot.edits[-1][2].startswith("🛠")
    assert fake_bot.callback_answers[-1] == ("cb-500", None, False)


async def test_admin_callback_denies_a_forged_section(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A ``support`` admin pressing a crafted ``adm:server`` is refused (§S2-1.10)."""
    await seed_support(session_factory, SUPPORT)

    await admin_callback(
        callback("adm:server", tg_id=SUPPORT), fake_bot, handler_container
    )

    assert fake_bot.edits == []
    assert fake_bot.messages == []
    assert fake_bot.callback_answers[-1] == ("cb-500", "Недостаточно прав", True)


async def test_admin_callback_stale_when_the_screen_is_absent(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """An owner pressing a not-yet-built section gets the stale toast, no render.

    ``audit`` is a valid section (so this is not the unknown-section path) whose
    viewer screen is not part of Stage 2, so it stays unregistered.
    """
    await admin_callback(callback("adm:audit"), fake_bot, handler_container)

    assert fake_bot.edits == []
    assert fake_bot.callback_answers[-1] == ("cb-500", texts.ERROR_STALE_BUTTON, False)


async def test_admin_callback_answers_a_crafted_payload(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """AC: a payload that is not an ``AdminNav`` never raises and is answered (B5)."""
    await admin_callback(callback("adm:bogus"), fake_bot, handler_container)

    assert fake_bot.callback_answers == [("cb-500", texts.ERROR_STALE_BUTTON, False)]
    assert fake_bot.edits == []


async def test_admin_callback_dispatches_a_registered_screen(
    handler_container: Container,
    fake_bot: FakeBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[AdminNav] = []

    async def screen(call: Any, bot: Any, container: Any, payload: AdminNav) -> None:
        seen.append(payload)

    monkeypatch.setitem(admin_nav.SCREENS, "settings", screen)

    await admin_callback(callback("adm:settings:bank"), fake_bot, handler_container)

    assert [payload.pack() for payload in seen] == ["adm:settings:bank"]
    assert fake_bot.callback_answers[-1] == ("cb-500", None, False)


# --- menu entry point (§S2-1.11) ---------------------------------------------


def test_menu_keyboard_adds_the_admin_button_for_staff() -> None:
    assert "adm:menu" not in payloads_of(menu_keyboard())
    assert "adm:menu" in payloads_of(menu_keyboard(is_staff=True))


async def test_start_shows_the_admin_button_only_for_staff(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    await start_command(message(OWNER), fake_bot, handler_container)
    staff_menu = payloads_of(fake_bot.messages[-1][2]["reply_markup"])

    await start_command(message(STRANGER), fake_bot, handler_container)
    user_menu = payloads_of(fake_bot.messages[-1][2]["reply_markup"])

    assert "adm:menu" in staff_menu
    assert "adm:menu" not in user_menu
