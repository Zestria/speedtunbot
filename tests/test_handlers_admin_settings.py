"""Settings screen tests (``TASK_PLAN.md`` §S2-7).

Covers the S2-7 acceptance criteria: the card reflects ``maintenance_mode()`` and
its three buttons parse; the toggle persists the flag (so the maintenance
middleware cancels the **next** non-staff update and only that), re-renders and
audits old → new; the bank-details edit is a multi-line FSM prompt with a
preview/confirm card whose cancel keeps the stored value; and the confirmed value
reaches ``/pay`` immediately (the service cache is refreshed by ``set``).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telebot.asyncio_handler_backends import CancelUpdate

from app import texts
from app.callbacks import AdminNav, Pay, unpack
from app.container import Container
from app.db.models import Admin, AuditLog, Tariff, UserStatus
from app.db.repositories import users as users_repo
from app.handlers.admin import settings_screen as screen
from app.handlers.admin.nav import admin_callback
from app.handlers.confirm import confirm_callback
from app.handlers.payment import pay_callback
from app.middlewares.maintenance import MaintenanceMiddleware
from app.permissions import Role
from app.states import UserStates
from tests.fakes import FakeBot

OWNER = 1
ADMIN = 7
USER = 500
CHAT = 99

BANK_VALUE = "Тинькофф\n+7 999 000-00-00\nполучатель: И. Иванов"


def message(tg_id: int, text: str, *, chat_id: int = CHAT) -> SimpleNamespace:
    """Minimal Telegram message: only the fields the handlers read."""
    return SimpleNamespace(
        from_user=SimpleNamespace(id=tg_id, username="neo", first_name="Neo"),
        chat=SimpleNamespace(id=chat_id),
        message_id=600,
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


def pay_call(tariff_id: int, *, tg_id: int = USER) -> SimpleNamespace:
    """Minimal ``pay:sel`` callback for the user side of the acceptance check."""
    return SimpleNamespace(
        id="cb-pay",
        data=Pay("sel", int(tariff_id)).pack(),
        from_user=SimpleNamespace(id=tg_id, username="neo", first_name="Neo"),
        message=SimpleNamespace(chat=SimpleNamespace(id=CHAT), message_id=700),
    )


def labels_of(markup: Any) -> list[str]:
    """Return the button labels of an inline keyboard, row by row."""
    return [button.text for row in markup.keyboard for button in row]


def payloads_of(markup: Any) -> list[str]:
    """Return the callback payloads of an inline keyboard, row by row."""
    return [button.callback_data for row in markup.keyboard for button in row]


async def seed_admin(
    factory: async_sessionmaker[AsyncSession], tg_id: int, role: Role
) -> None:
    """Grant ``tg_id`` a non-owner role from the ``admins`` table."""
    async with factory() as session:
        session.add(Admin(tg_id=tg_id, role=str(role), added_by=OWNER))
        await session.commit()


async def seed_user(factory: async_sessionmaker[AsyncSession], tg_id: int) -> None:
    """Insert an ``approved`` ``users`` row directly."""
    async with factory() as session:
        await users_repo.upsert_from_telegram(session, tg_id, username="neo")
        await users_repo.set_status(session, tg_id, UserStatus.APPROVED)
        await session.commit()


async def seed_tariff(factory: async_sessionmaker[AsyncSession]) -> Tariff:
    """Insert one active tariff and return it."""
    async with factory() as session:
        tariff = Tariff(
            name="30 дней — 150 ₽", days=30, price=150, is_active=True, sort_order=1
        )
        session.add(tariff)
        await session.commit()
        await session.refresh(tariff)
        return tariff


async def audit_rows(
    factory: async_sessionmaker[AsyncSession],
) -> list[AuditLog]:
    """Return every audit row, oldest first."""
    async with factory() as session:
        return list((await session.execute(select(AuditLog))).scalars().all())


# --- rendering + keyboard (§S2-7.1) -----------------------------------------


def test_render_settings_shows_state_and_bank() -> None:
    """AC: the card reflects ``maintenance_mode()`` and the stored details."""
    off = screen.render_settings(maintenance=False, bank=None)

    assert "⚙️ <b>Настройки</b>" in off
    assert "🔧 Тех. работы: 🟢 выкл" in off
    assert f"🏦 Реквизиты: {screen.BANK_NOT_SET}" in off

    on = screen.render_settings(maintenance=True, bank="Сбер 1234 5678")
    assert "🔧 Тех. работы: 🔴 вкл" in on
    assert "🏦 Реквизиты: <code>Сбер 1234 5678</code>" in on


def test_settings_keyboard_buttons_parse() -> None:
    """AC: the toggle/bank/back buttons pack into valid ``AdminNav`` payloads."""
    markup = screen.settings_keyboard(maintenance=False)

    assert labels_of(markup) == [
        "🔧 Тех. работы: 🟢 выкл",
        "🏦 Реквизиты",
        texts.BUTTON_BACK,
    ]
    payloads = payloads_of(markup)
    assert payloads == ["adm:settings:mt", "adm:settings:bank", "adm:menu"]
    assert [unpack(data) for data in payloads] == [
        AdminNav("settings", screen.OP_TOGGLE),
        AdminNav("settings", screen.OP_BANK),
        AdminNav("menu"),
    ]

    assert labels_of(screen.settings_keyboard(maintenance=True))[0] == (
        "🔧 Тех. работы: 🔴 вкл"
    )


# --- screen (§S2-7.1) -------------------------------------------------------


async def test_settings_screen_renders_the_current_state(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """``adm:settings`` renders the card and the toggle/bank/back keyboard."""
    settings = handler_container.settings_service
    assert settings is not None

    await admin_callback(callback("adm:settings"), fake_bot, handler_container)

    text = fake_bot.edits[-1][2]
    markup = fake_bot.edits[-1][3]["reply_markup"]
    assert "⚙️ <b>Настройки</b>" in text
    assert "🔧 Тех. работы: 🟢 выкл" in text
    assert payloads_of(markup) == ["adm:settings:mt", "adm:settings:bank", "adm:menu"]

    await settings.set_maintenance_mode(True)
    await admin_callback(callback("adm:settings"), fake_bot, handler_container)
    assert "🔧 Тех. работы: 🔴 вкл" in fake_bot.edits[-1][2]


async def test_bank_prompt_enters_the_fsm_and_cancel_keeps_the_value(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """AC (§S2-7.3): the prompt shows the current value and «✖️ Отмена» keeps it."""
    settings = handler_container.settings_service
    assert settings is not None

    await admin_callback(callback("adm:settings:bank"), fake_bot, handler_container)

    prompt = fake_bot.edits[-1][2]
    assert "Отправьте новое значение" in prompt
    assert screen.BANK_NOT_SET in prompt
    assert await fake_bot.get_state(OWNER, CHAT) == UserStates.admin_bank.name
    # «✖️ Отмена» re-opens the home card, which drops the state again.
    assert payloads_of(fake_bot.edits[-1][3]["reply_markup"]) == ["adm:settings"]

    await admin_callback(callback("adm:settings"), fake_bot, handler_container)

    assert await fake_bot.get_state(OWNER, CHAT) is None
    assert await settings.bank_details() is None


def _confirm_payloads(fake_bot: FakeBot) -> list[str]:
    """Return the ``cf:`` payloads of the last message sent (the preview card)."""
    return payloads_of(fake_bot.messages[-1][2]["reply_markup"])


# --- bank details edit (§S2-7.3/.4) -----------------------------------------


async def test_bank_edit_is_previewed_then_applied(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (§S2-7.4): the confirmed value is stored, audited and re-rendered."""
    settings = handler_container.settings_service
    assert settings is not None

    await screen.admin_bank_message(
        message(OWNER, BANK_VALUE), fake_bot, handler_container
    )

    # Nothing is written yet — the value waits behind a preview card.
    preview = fake_bot.messages[-1][1]
    assert "Новые реквизиты" in preview
    assert "И. Иванов" in preview
    assert await fake_bot.get_state(OWNER, CHAT) is None
    assert _confirm_payloads(fake_bot)[0].startswith("cf:")

    await confirm_callback(
        callback(_confirm_payloads(fake_bot)[0]), fake_bot, handler_container
    )

    assert await settings.bank_details() == BANK_VALUE
    assert "И. Иванов" in fake_bot.edits[-1][2]
    assert screen.BANK_SAVED in [text for _, text, _ in fake_bot.callback_answers]

    rows = await audit_rows(session_factory)
    assert [(row.action, row.target_id) for row in rows] == [
        ("setting.set", "bank_details")
    ]
    assert rows[0].details == {"old": None, "new": BANK_VALUE}


async def test_bank_preview_cancel_keeps_the_old_value(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC: «❌ Отмена» on the preview leaves the stored details untouched."""
    settings = handler_container.settings_service
    assert settings is not None

    await screen.admin_bank_message(
        message(OWNER, BANK_VALUE), fake_bot, handler_container
    )
    cancel_payload = _confirm_payloads(fake_bot)[1]
    assert cancel_payload.startswith("cf:x:")

    await confirm_callback(callback(cancel_payload), fake_bot, handler_container)

    assert await settings.bank_details() is None
    assert fake_bot.edits[-1][2] == texts.CONFIRM_CANCELLED
    assert await audit_rows(session_factory) == []


async def test_empty_bank_value_clears_the_state(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """A blank reply drops the prompt instead of storing an empty string."""
    settings = handler_container.settings_service
    assert settings is not None
    await fake_bot.set_state(OWNER, UserStates.admin_bank, CHAT)

    await screen.admin_bank_message(message(OWNER, "   "), fake_bot, handler_container)

    assert await fake_bot.get_state(OWNER, CHAT) is None
    assert fake_bot.sent[-1][1] == screen.BANK_EMPTY
    assert await settings.bank_details() is None
    assert fake_bot.edits == []


async def test_confirmed_bank_details_reach_pay(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC (§S2-7.4): ``/pay`` shows the new details immediately (fresh cache)."""
    await seed_user(session_factory, USER)
    tariff = await seed_tariff(session_factory)

    await screen.admin_bank_message(
        message(OWNER, BANK_VALUE), fake_bot, handler_container
    )
    await confirm_callback(
        callback(_confirm_payloads(fake_bot)[0]), fake_bot, handler_container
    )

    await pay_callback(pay_call(int(tariff.id)), fake_bot, handler_container)

    assert BANK_VALUE in fake_bot.edits[-1][2]


# --- maintenance toggle (§S2-7.2) -------------------------------------------


async def test_toggle_flips_maintenance_and_audits_old_to_new(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC: the toggle persists, re-renders immediately and audits old → new."""
    settings = handler_container.settings_service
    assert settings is not None

    await admin_callback(callback("adm:settings:mt"), fake_bot, handler_container)

    assert await settings.maintenance_mode() is True
    assert "🔧 Тех. работы: 🔴 вкл" in fake_bot.edits[-1][2]
    assert screen.MAINTENANCE_TOAST_ON in [t for _, t, _ in fake_bot.callback_answers]

    rows = await audit_rows(session_factory)
    assert [(row.action, row.target_id) for row in rows] == [
        ("setting.set", "maintenance_mode")
    ]
    assert rows[0].details == {"old": False, "new": True}

    await admin_callback(callback("adm:settings:mt"), fake_bot, handler_container)

    assert await settings.maintenance_mode() is False
    rows = await audit_rows(session_factory)
    assert rows[-1].details == {"old": True, "new": False}
    assert screen.MAINTENANCE_TOAST_OFF in [t for _, t, _ in fake_bot.callback_answers]


async def test_toggle_silences_the_next_non_staff_update(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """AC (§S2-7.2): the middleware cancels the next update; off restores service."""
    settings = handler_container.settings_service
    assert settings is not None
    middleware = MaintenanceMiddleware(settings, bot=fake_bot)
    update = SimpleNamespace(
        from_user=SimpleNamespace(id=USER), chat=SimpleNamespace(id=CHAT)
    )
    data: dict[str, Any] = {"role": None}

    assert await middleware.pre_process(update, data) is None

    await admin_callback(callback("adm:settings:mt"), fake_bot, handler_container)
    assert isinstance(await middleware.pre_process(update, data), CancelUpdate)

    await admin_callback(callback("adm:settings:mt"), fake_bot, handler_container)
    assert await middleware.pre_process(update, data) is None


# --- permissions (§S2-7.1) --------------------------------------------------


async def test_settings_screen_is_owner_only(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An ``admin`` holds no ``settings.edit``, so the dispatcher refuses it."""
    await seed_admin(session_factory, ADMIN, Role.ADMIN)

    await admin_callback(
        callback("adm:settings", tg_id=ADMIN), fake_bot, handler_container
    )

    assert fake_bot.edits == []
    assert fake_bot.messages == []
    assert fake_bot.callback_answers[-1] == ("cb-500", "Недостаточно прав", True)


async def test_forged_toggle_is_refused_by_the_screen(
    handler_container: Container,
    fake_bot: FakeBot,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The screen re-checks its own permission: a forged ``mt`` writes nothing."""
    await seed_admin(session_factory, ADMIN, Role.ADMIN)
    settings = handler_container.settings_service
    assert settings is not None

    await screen.settings_screen(
        callback("adm:settings:mt", tg_id=ADMIN), fake_bot, handler_container
    )

    assert await settings.maintenance_mode() is False
    assert fake_bot.edits == []
    assert await audit_rows(session_factory) == []
