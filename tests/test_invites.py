"""Invite-link tests (``TASK_PLAN.md`` §S3-3).

Covers the S3-3 acceptance criteria:

* only the sha256 of a token is stored — never the raw token, never in the audit
  trail (§S3-3.2);
* a single-use link is redeemed exactly once, even by two simultaneous callers,
  and a revoked/expired link is refused (§S3-3.3);
* the wizard shows the link once together with its QR image (§S3-3.5);
* the active-invite list renders and revoke removes the row (§S3-3.6);
* ``/invite`` is in the admin menu only (§S3-3.8);
* a valid invite approves the redeemer and a bad token is answered as stale
  (§S3-3.9).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import texts
from app.callbacks import Invite
from app.container import Container
from app.db.base import utcnow
from app.db.models import Admin, AuditLog, InviteKind, UserStatus
from app.db.models import Invite as InviteRow
from app.db.repositories import invites as invites_repo
from app.db.repositories import users as users_repo
from app.handlers.admin import invites as screen
from app.handlers.start import start_command
from app.permissions import Role
from app.services.invites import InviteService, hash_token
from tests.fakes import FakeBot

OWNER = 1
ADMIN = 7
SUPPORT = 8
STRANGER = 555
CHAT = 99
MSG = 500


def message(text: str = "/start", *, tg_id: int = STRANGER) -> SimpleNamespace:
    """Minimal Telegram message: only the fields the handlers read."""
    return SimpleNamespace(
        from_user=SimpleNamespace(id=tg_id, username="neo", first_name="Neo"),
        chat=SimpleNamespace(id=CHAT),
        text=text,
    )


def callback(
    data: str, *, tg_id: int = OWNER, message_id: int = MSG
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


async def rows(factory: async_sessionmaker[AsyncSession]) -> list[InviteRow]:
    """Return every invite row, oldest first."""
    async with factory() as session:
        result = await session.execute(select(InviteRow).order_by(InviteRow.id))
        return list(result.scalars().all())


async def get_status(
    factory: async_sessionmaker[AsyncSession], tg_id: int
) -> str | None:
    """Return the ``status`` column of ``users.tg_id`` (``None`` when absent)."""
    async with factory() as session:
        user = await users_repo.get(session, tg_id)
        return None if user is None else str(user.status)


# --- service + repository (§S3-3.2/.3) --------------------------------------


def test_encode_decode_days_round_trip() -> None:
    """§S3-3.5: one int carries both wizard answers."""
    assert screen.decode_days(screen.encode_days(0, 1)) == (0, 1)
    assert screen.decode_days(screen.encode_days(5, 30)) == (5, 30)
    assert screen.uses_label(0) == texts.INVITE_UNLIMITED
    assert screen.uses_label(5) == "5"


async def test_create_stores_only_the_hash_and_never_the_token(
    session_factory: async_sessionmaker[AsyncSession], handler_container: Container
) -> None:
    """AC (§S3-3.2): the DB and the audit trail hold the hash, never the token."""
    service = InviteService(session_factory, audit=handler_container.audit)
    before = utcnow()

    invite, token = await service.create(
        InviteKind.USER, max_uses=1, ttl_days=7, actor=OWNER
    )

    assert token and len(token) >= 8
    assert invite.token_hash == hash_token(token)
    assert invite.token_hash != token
    assert invite.kind == str(InviteKind.USER)
    assert invite.max_uses == 1 and invite.uses == 0
    assert invite.created_by == OWNER
    assert (
        timedelta(days=6) < invite.expires_at - before <= timedelta(days=7, seconds=1)
    )

    # The raw token appears in no invite column and in no audit row.
    assert token not in {invite.kind, invite.label or ""}
    async with session_factory() as session:
        audits = (await session.execute(select(AuditLog))).scalars().all()
    assert [row.action for row in audits] == ["invite.create"]
    for row in audits:
        assert token not in str(row.details)
        assert token not in (row.target_id or "")
    # ``create`` is the only place a series is minted: a second call differs.
    _, second = await service.create(
        InviteKind.USER, max_uses=None, ttl_days=1, actor=OWNER
    )
    assert second != token


async def test_a_single_use_invite_is_spent_once(
    session_factory: async_sessionmaker[AsyncSession], handler_container: Container
) -> None:
    """AC (§S3-3.3): the second redemption of a 1-use link fails, no side effect."""
    service = InviteService(session_factory, audit=handler_container.audit)
    _, token = await service.create(
        InviteKind.USER, max_uses=1, ttl_days=1, actor=OWNER
    )

    first = await service.redeem(token)
    second = await service.redeem(token)

    assert first.ok and first.invite is not None
    assert not second.ok and second.invite is None
    stored = (await rows(session_factory))[0]
    assert stored.uses == 1


async def test_concurrent_redeem_has_exactly_one_winner(
    session_factory: async_sessionmaker[AsyncSession], handler_container: Container
) -> None:
    """AC (§S3-3.3): two simultaneous callers → one success, one refusal."""
    service = InviteService(session_factory, audit=handler_container.audit)
    _, token = await service.create(
        InviteKind.USER, max_uses=1, ttl_days=1, actor=OWNER
    )

    results = await asyncio.gather(service.redeem(token), service.redeem(token))

    assert sorted(result.ok for result in results) == [False, True]
    assert (await rows(session_factory))[0].uses == 1


async def test_unlimited_invite_survives_many_redeems(
    session_factory: async_sessionmaker[AsyncSession], handler_container: Container
) -> None:
    """``max_uses=None`` (∞) keeps the link redeemable and counts every use."""
    service = InviteService(session_factory, audit=handler_container.audit)
    _, token = await service.create(
        InviteKind.USER, max_uses=None, ttl_days=1, actor=OWNER
    )

    results = [await service.redeem(token) for _ in range(3)]

    assert all(result.ok for result in results)
    assert (await rows(session_factory))[0].uses == 3


async def test_revoked_invite_is_refused(
    session_factory: async_sessionmaker[AsyncSession], handler_container: Container
) -> None:
    """AC (§S3-3.6): a revoked link can no longer be redeemed."""
    service = InviteService(session_factory, audit=handler_container.audit)
    invite, token = await service.create(
        InviteKind.USER, max_uses=None, ttl_days=1, actor=OWNER
    )

    assert await service.revoke(int(invite.id), actor=OWNER) is True
    assert await service.revoke(int(invite.id), actor=OWNER) is False
    assert (await service.redeem(token)).ok is False
    assert await service.list_active() == []


async def test_expired_invite_is_refused(
    session_factory: async_sessionmaker[AsyncSession], handler_container: Container
) -> None:
    """AC (§S3-3.3): an expired link is refused even with uses left."""
    async with session_factory() as session:
        await invites_repo.create(
            session,
            kind=str(InviteKind.USER),
            token_hash=hash_token("stale-token"),
            max_uses=None,
            expires_at=utcnow() - timedelta(minutes=1),
            created_by=OWNER,
        )
        await session.commit()

    service = InviteService(session_factory, audit=handler_container.audit)
    assert (await service.redeem("stale-token")).ok is False
    assert (await rows(session_factory))[0].uses == 0
    assert await service.list_active() == []


# --- wizard + list screens (§S3-3.5/.6) -------------------------------------


async def _run_wizard(
    fake_bot: FakeBot, container: Container, *, uses: int = 5, days: int = 7
) -> None:
    """Drive step 1 → step 2 → create for ``uses``×``days``."""
    await screen.invites_screen(callback("adm:invites"), fake_bot, container)
    await screen.invites_callback(
        callback(Invite(screen.OP_USES, uses).pack()), fake_bot, container
    )
    await screen.invites_callback(
        callback(Invite(screen.OP_DAYS, screen.encode_days(uses, days)).pack()),
        fake_bot,
        container,
    )


async def test_wizard_shows_the_link_once_with_its_qr(
    session_factory: async_sessionmaker[AsyncSession],
    handler_container: Container,
    fake_bot: FakeBot,
) -> None:
    """AC (§S3-3.5): one link message + one QR photo; the link is never repeated."""
    handler_container.bot_username = "test_bot"

    await _run_wizard(fake_bot, handler_container, uses=5, days=7)

    prefixed = "https://t.me/test_bot?start=inv_"
    link_messages = [text for chat_id, text, _ in fake_bot.messages if prefixed in text]
    assert len(link_messages) == 1  # shown once, never re-rendered

    link = link_messages[0].split("<code>")[1].split("</code>")[0]
    token = link.split("inv_", 1)[1]
    stored = (await rows(session_factory))[0]
    assert stored.token_hash == hash_token(token)
    assert stored.max_uses == 5 and stored.uses == 0
    assert "Использований: 5" in link_messages[0]

    # The QR travels as an in-memory upload, captioned, exactly once.
    assert [kind for _, kind, _, _ in fake_bot.media] == ["photo"]
    _, _, photo, kwargs = fake_bot.media[0]
    assert kwargs["caption"] == texts.INVITE_QR_CAPTION
    assert getattr(photo, "name", None) == "qr.png"

    # The wizard is left ready for another invite (step 1, no link).
    assert fake_bot.edits[-1][2] == texts.INVITE_USES_PROMPT
    assert token not in fake_bot.edits[-1][2]


async def test_list_renders_and_revoke_removes_the_row(
    session_factory: async_sessionmaker[AsyncSession],
    handler_container: Container,
    fake_bot: FakeBot,
) -> None:
    """AC (§S3-3.6): active invites list with «🗑 Отозвать»; revoke drops the row."""
    service = InviteService(session_factory, audit=handler_container.audit)
    first, _ = await service.create(
        InviteKind.USER, max_uses=1, ttl_days=1, actor=OWNER
    )
    second, _ = await service.create(
        InviteKind.USER, max_uses=None, ttl_days=30, actor=OWNER
    )

    await screen.invites_callback(
        callback(Invite(screen.OP_LIST, 0).pack()), fake_bot, handler_container
    )

    text = fake_bot.edits[-1][2]
    markup = fake_bot.edits[-1][3]["reply_markup"]
    assert texts.INVITE_LIST_TITLE in text
    assert f"№{first.id} · 0/1 · до " in text
    assert f"№{second.id} · 0/{texts.INVITE_UNLIMITED} · до " in text
    assert labels_of(markup).count(texts.BUTTON_INVITE_CREATE) == 1
    assert f"inv:revoke:{first.id}" in payloads_of(markup)

    await screen.invites_callback(
        callback(Invite(screen.OP_REVOKE, int(first.id)).pack()),
        fake_bot,
        handler_container,
    )

    assert fake_bot.callback_answers[-1][1] == texts.INVITE_REVOKED
    assert f"№{first.id} ·" not in fake_bot.edits[-1][2]
    assert [invite.id for invite in await service.list_active()] == [second.id]

    # A second click on the same (now stale) button is answered, not applied.
    await screen.invites_callback(
        callback(Invite(screen.OP_REVOKE, int(first.id)).pack()),
        fake_bot,
        handler_container,
    )
    assert fake_bot.callback_answers[-1][1] == texts.INVITE_REVOKE_GONE


async def test_empty_list_is_rendered(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """An empty section still answers instead of showing a bare title."""
    await screen.invites_callback(
        callback(Invite(screen.OP_LIST, 0).pack()), fake_bot, handler_container
    )

    assert texts.INVITE_LIST_EMPTY in fake_bot.edits[-1][2]


async def test_invite_screens_require_invites_create(
    session_factory: async_sessionmaker[AsyncSession],
    handler_container: Container,
    fake_bot: FakeBot,
) -> None:
    """AC (§S3-3.7): support/strangers are refused before any invite is minted."""
    await seed_admin(session_factory, SUPPORT, Role.SUPPORT)

    await screen.invites_callback(
        callback(
            Invite(screen.OP_DAYS, screen.encode_days(5, 7)).pack(), tg_id=SUPPORT
        ),
        fake_bot,
        handler_container,
    )
    await screen.invites_callback(
        callback(
            Invite(screen.OP_DAYS, screen.encode_days(5, 7)).pack(), tg_id=STRANGER
        ),
        fake_bot,
        handler_container,
    )
    await screen.invites_screen(
        callback("adm:invites", tg_id=SUPPORT), fake_bot, handler_container
    )

    assert await rows(session_factory) == []
    assert fake_bot.callback_answers[-1][1] == "Недостаточно прав"


# --- ``/start inv_…`` redemption (§S3-3.9) ----------------------------------


async def test_a_valid_invite_approves_the_redeemer(
    session_factory: async_sessionmaker[AsyncSession],
    handler_container: Container,
    fake_bot: FakeBot,
) -> None:
    """AC (§S3-3.9): a valid user invite approves + grants a client, once."""
    invite, token = await InviteService(
        session_factory, audit=handler_container.audit
    ).create(InviteKind.USER, max_uses=1, ttl_days=1, actor=OWNER)

    await start_command(message(f"/start inv_{token}"), fake_bot, handler_container)

    assert await get_status(session_factory, STRANGER) == str(UserStatus.APPROVED)
    assert await handler_container.panel.get_client(STRANGER) is not None
    assert texts.START_MENU in fake_bot.texts_to(CHAT)[-1]
    assert (await rows(session_factory))[0].uses == 1
    assert int(invite.id) == 1


async def test_a_bad_token_is_answered_as_stale(
    session_factory: async_sessionmaker[AsyncSession],
    handler_container: Container,
    fake_bot: FakeBot,
) -> None:
    """AC (§S3-3.9): an unknown token is a stale link, never a crash."""
    await start_command(message("/start inv_nope"), fake_bot, handler_container)
    assert texts.INVITE_REDEEM_FAILED in fake_bot.texts_to(CHAT)

    # A spent link re-answers the stale copy instead of creating a second client.
    service = InviteService(session_factory, audit=handler_container.audit)
    _, token = await service.create(
        InviteKind.USER, max_uses=1, ttl_days=1, actor=OWNER
    )
    await start_command(message(f"/start inv_{token}"), fake_bot, handler_container)
    await start_command(message(f"/start inv_{token}"), fake_bot, handler_container)

    assert fake_bot.texts_to(CHAT)[-1] == texts.INVITE_REDEEM_FAILED
    assert await get_status(session_factory, STRANGER) == str(UserStatus.APPROVED)


async def test_invite_command_opens_the_wizard(
    handler_container: Container, fake_bot: FakeBot
) -> None:
    """AC (§S3-3.8): ``/invite`` shows step 1 to an authorised admin."""
    await screen.invite_command(
        message("/invite", tg_id=OWNER), fake_bot, handler_container
    )

    assert fake_bot.sent[-1][1] == texts.INVITE_USES_PROMPT
    assert payloads_of(fake_bot.messages[-1][2]["reply_markup"]) == [
        "inv:uses:1",
        "inv:uses:5",
        "inv:uses:0",
        "inv:list:0",
        "adm:menu",
    ]
