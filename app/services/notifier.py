"""Staff notification routing (``TASK_PLAN.md`` §2.9 / M0-06 + M0-07).

M0-06 added **recipient resolution** (:meth:`Notifier.recipients_for`). M0-07
adds the delivery half:

* :meth:`Notifier.safe_send` — the single place that awaits ``send_message``,
  retries once on Telegram 429 (honouring ``retry_after``), flips
  ``users.bot_blocked`` on 403 / "chat not found", and never raises (fixes B2);
* :meth:`Notifier.send_to` / :meth:`fan_out` — fan out to resolved recipients;
* :meth:`Notifier.alert_staff` — route an alert by permission or card kind;
* :meth:`Notifier.send_card` / :meth:`sync_card` — remember every copy of a
  staff card in the ``admin_cards`` table and edit them all (fallback: re-send);
* :meth:`Notifier.allow_error_alert` — error-signature limiter (one owner alert
  per signature per 10 min), consumed by the M0-08 global error handler.

When the bot/sessionmaker is not wired yet, every method degrades gracefully so
services and tests can build a bare ``Notifier(admins)``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telebot.apihelper import ApiTelegramException

from app import texts
from app.callbacks import Access
from app.db.base import utcnow
from app.db.models import CardKind
from app.db.repositories import admin_cards as admin_cards_repo
from app.db.repositories import users as users_repo
from app.permissions import Permission
from app.services.admins import AdminService
from app.services.users import DECISION_ACCEPT, DECISION_BLOCK, DECISION_REJECT
from app.utils.text import esc

logger = logging.getLogger(__name__)

#: Fallback retry delay (s) when Telegram's 429 omits ``retry_after``.
DEFAULT_RETRY_AFTER = 1.0
#: Never block a fan-out longer than this on a 429. A larger ``retry_after``
#: (Telegram can ask for a minute during mass sends) makes the single send fail
#: fast instead, so the broadcast loop is not stalled for everyone else.
MAX_RETRY_AFTER = 5.0
#: Window (s) during which a repeated error signature is suppressed.
ERROR_ALERT_WINDOW = 600.0


@dataclass(frozen=True)
class Route:
    """A permission + optional ``notify_*`` flag used to pick recipients."""

    permission: Permission
    flag: str | None = None


#: §2.5 routing table: card/alert kind → who should be notified.
ROUTES: dict[str, Route] = {
    CardKind.PAYMENT: Route(Permission.PAYMENTS_REVIEW, "notify_payments"),
    CardKind.ACCESS: Route(Permission.ACCESS_REVIEW, "notify_access"),
    "support": Route(Permission.SUPPORT_REPLY, "notify_support"),
    # Admin-grant requests and health/error alerts go to owners + server.view.
    CardKind.ADMIN_GRANT: Route(Permission.ADMINS_MANAGE),
    "alert": Route(Permission.SERVER_VIEW),
}


# --- access request card (§S3-2) --------------------------------------------

#: Decision → the line appended to every copy of the card once it lands. Keyed by
#: :mod:`app.services.users`' decision constants so a rename cannot silently drop
#: the «обработал …» line from the card.
_DECISION_LINES: dict[str, str] = {
    DECISION_ACCEPT: texts.ACCESS_CARD_ACCEPTED,
    DECISION_REJECT: texts.ACCESS_CARD_REJECTED,
    DECISION_BLOCK: texts.ACCESS_CARD_BLOCKED,
}


def actor_label(actor: int | None, username: str | None = None) -> str:
    """Return the admin's display label for a card («@neo» or «ID 7»)."""
    if username:
        return f"@{username}"
    return "системой" if actor is None else f"ID {int(actor)}"


def access_review_markup(tg_id: int) -> Any:
    """Build the accept/reject/block keyboard of the access card (§S3-2.4)."""
    from telebot.util import quick_markup

    return quick_markup(
        {
            texts.BUTTON_ACCESS_ACCEPT: {
                "callback_data": Access("accept", int(tg_id)).pack()
            },
            texts.BUTTON_ACCESS_REJECT: {
                "callback_data": Access("reject", int(tg_id)).pack()
            },
            texts.BUTTON_ACCESS_BLOCK: {
                "callback_data": Access("block", int(tg_id)).pack()
            },
        },
        row_width=2,
    )


def render_access_card(
    tg_id: int,
    *,
    username: str | None,
    first_name: str | None,
    created_at: datetime | None = None,
    decision: str | None = None,
    decided_by: str | None = None,
) -> str:
    """Render the §S3-2.4 access card (HTML-escaped).

    The single renderer behind the card fanned out on ``/start``, the copy an
    admin opens from ``adm:access`` and the edit pushed by :meth:`sync_card`, so
    the three can never drift apart. ``decision``/``decided_by`` add the
    «обработал …» line once a reviewer has acted.
    """
    who = f"@{esc(username)}" if username else texts.ACCESS_NO_USERNAME
    name = esc(first_name) if first_name else texts.ACCESS_NO_NAME
    when = (created_at or utcnow()).strftime("%d.%m.%Y %H:%M")
    body = texts.ACCESS_CARD.format(tg_id=int(tg_id), who=who, name=name, time=when)
    line = _DECISION_LINES.get(str(decision))
    if line is not None:
        body += "\n\n" + line.format(actor=esc(decided_by or "?"))
    return body


def _retry_after(exc: ApiTelegramException) -> float:
    """Return Telegram's ``retry_after`` for a 429, or a safe default."""
    result: dict[str, Any] = getattr(exc, "result_json", None) or {}
    params: dict[str, Any] = result.get("parameters") or {}
    value = params.get("retry_after")
    if value is None:
        return DEFAULT_RETRY_AFTER
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return DEFAULT_RETRY_AFTER


def _is_blocked_error(exc: ApiTelegramException) -> bool:
    """Return ``True`` when the chat is unreachable (403 / "chat not found")."""
    code = getattr(exc, "error_code", None)
    if code == 403:
        return True
    if code != 400:
        return False
    description = (getattr(exc, "description", "") or "").lower()
    return (
        "chat not found" in description
        or "user not found" in description
        or "bot was blocked" in description
    )


def _message_id(message: Any) -> int | None:
    """Return ``message.message_id`` when the bot returned a message object."""
    value = getattr(message, "message_id", None)
    return int(value) if isinstance(value, int) else None


class Notifier:
    """Resolve recipients and deliver messages safely."""

    def __init__(
        self,
        admins: AdminService,
        bot: Any | None = None,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        *,
        clock: Any | None = None,
    ) -> None:
        self._admins = admins
        self._bot = bot
        self._sessionmaker = sessionmaker
        self._clock = clock or time.monotonic
        self._seen_errors: dict[str, float] = {}

    def attach_bot(self, bot: Any) -> None:
        """Wire the bot used by :meth:`safe_send` (called at startup)."""
        self._bot = bot

    # --- recipient resolution (§2.5 / M0-06) -------------------------------

    async def recipients_for(
        self, permission: Permission, flag: str | None = None
    ) -> list[int]:
        """Return IDs of staff holding ``permission``, filtered by ``flag``."""
        return await self._admins.staff_with_all(permission, flag)

    async def recipients_for_kind(self, kind: str) -> list[int]:
        """Resolve via :data:`ROUTES` for a :class:`~app.db.models.CardKind`."""
        try:
            route = ROUTES[str(kind)]
        except KeyError as exc:
            raise ValueError(f"unknown notification kind: {kind!r}") from exc
        return await self.recipients_for(route.permission, route.flag)

    # --- safe delivery (§2.9 / M0-07) --------------------------------------

    async def safe_send(
        self, chat_id: int, text: str, *, patient: bool = False, **kwargs: Any
    ) -> bool:
        """Send ``text`` to ``chat_id`` without ever raising (returns ``bool``).

        Retries once on 429 using ``retry_after``; a 403 / "chat not found"
        reply marks the user ``bot_blocked`` and returns ``False``.

        ``patient=True`` (§S2-6.5) honours ``retry_after`` **fully**: it always
        sleeps the delay Telegram asks for and retries once, ignoring
        :data:`MAX_RETRY_AFTER`. Only the broadcast loop opts in — every other
        caller keeps the capped behaviour so a long rate-limit never stalls a
        time-sensitive fan-out.
        """
        delivered, _ = await self._deliver(chat_id, text, patient=patient, **kwargs)
        return delivered

    async def _deliver(
        self, chat_id: int, text: str, *, patient: bool = False, **kwargs: Any
    ) -> tuple[bool, int | None]:
        """Send once (with the 429 retry) and report success + message id."""
        bot = self._bot
        if bot is None:
            logger.warning("safe_send(%s) skipped: no bot configured", chat_id)
            return False, None
        try:
            message = await bot.send_message(chat_id, text, **kwargs)
        except ApiTelegramException as exc:
            return await self._handle_api_error(
                chat_id, text, kwargs, exc, patient=patient
            )
        except Exception:  # pragma: no cover - other transport/bot errors
            logger.warning("safe_send to %s failed", chat_id, exc_info=True)
            return False, None
        return True, _message_id(message)

    async def _handle_api_error(
        self,
        chat_id: int,
        text: str,
        kwargs: dict[str, Any],
        exc: ApiTelegramException,
        *,
        patient: bool = False,
    ) -> tuple[bool, int | None]:
        """Apply the 429 retry / blocked-chat policy for one failed send."""
        bot = self._bot
        if bot is None:  # pragma: no cover - guarded by _deliver
            return False, None
        if getattr(exc, "error_code", None) == 429:
            delay = _retry_after(exc)
            if not patient and delay > MAX_RETRY_AFTER:
                logger.warning(
                    "rate limited for %.1fs (> %.1fs cap) sending to %s; "
                    "giving up this send",
                    delay,
                    MAX_RETRY_AFTER,
                    chat_id,
                )
                return False, None
            logger.info("rate limited sending to %s; retrying in %.1fs", chat_id, delay)
            await asyncio.sleep(delay)
            try:
                message = await bot.send_message(chat_id, text, **kwargs)
            except Exception:
                logger.warning("safe_send retry to %s failed", chat_id, exc_info=True)
                return False, None
            return True, _message_id(message)
        if _is_blocked_error(exc):
            logger.info("user %s unreachable (%s); marking bot_blocked", chat_id, exc)
            await self._mark_blocked(chat_id)
            return False, None
        logger.warning("safe_send to %s failed: %s", chat_id, exc)
        return False, None

    async def _mark_blocked(self, chat_id: int) -> None:
        """Set ``users.bot_blocked=True`` in its own short transaction.

        Fully isolated from the send loop: the write runs in a dedicated
        session/transaction and any failure (DB down, missing row) is logged and
        swallowed, so one unreachable user can never abort a fan-out for the
        rest of the recipients.
        """
        sessionmaker = self._sessionmaker
        if sessionmaker is None:
            return
        try:
            async with sessionmaker() as session:
                await users_repo.set_bot_blocked(session, int(chat_id), True)
                await session.commit()
        except Exception:  # never break sending on a bookkeeping failure
            logger.warning("failed to mark %s bot_blocked", chat_id, exc_info=True)

    async def send_to(self, chat_ids: list[int], text: str, **kwargs: Any) -> list[int]:
        """Send ``text`` to each id; return the ids that were delivered."""
        delivered: list[int] = []
        for chat_id in chat_ids:
            if await self.safe_send(chat_id, text, **kwargs):
                delivered.append(int(chat_id))
        return delivered

    async def fan_out(self, kind: str, text: str, **kwargs: Any) -> list[int]:
        """Send ``text`` to everyone routed for ``kind`` (§2.5)."""
        return await self.send_to(await self.recipients_for_kind(kind), text, **kwargs)

    async def alert_staff(
        self,
        text: str,
        *,
        kind: str = "alert",
        permission: Permission | None = None,
        flag: str | None = None,
        **kwargs: Any,
    ) -> list[int]:
        """Alert staff: by explicit ``permission``/``flag`` or by card ``kind``."""
        if permission is not None:
            recipients = await self.recipients_for(permission, flag)
        else:
            recipients = await self.recipients_for_kind(kind)
        return await self.send_to(recipients, text, **kwargs)

    # --- admin cards (all-copies sync) -------------------------------------

    async def send_card(
        self,
        kind: str,
        ref_id: int,
        recipients: list[int],
        text: str,
        **kwargs: Any,
    ) -> list[int]:
        """Send a staff card and remember each delivered copy (``admin_cards``)."""
        delivered: list[int] = []
        for chat_id in recipients:
            ok, message_id = await self._deliver(chat_id, text, **kwargs)
            if not ok:
                continue
            delivered.append(int(chat_id))
            if message_id is not None:
                await self._store_card(kind, ref_id, int(chat_id), message_id)
        return delivered

    async def send_access_card(
        self,
        tg_id: int,
        *,
        username: str | None,
        first_name: str | None,
        created_at: datetime | None = None,
        recipients: list[int] | None = None,
    ) -> list[int]:
        """Fan the «Новая заявка» card out to ``access.review`` staff (§S3-2.4).

        ``recipients`` overrides the resolved reviewers so the ``adm:access``
        screen can open a single copy for the acting admin; the copy is stored in
        ``admin_cards`` either way, so a later ``sync_card`` edits every copy.
        """
        if recipients is None:
            recipients = await self.recipients_for_kind(CardKind.ACCESS)
        if not recipients:
            logger.info("access request %s delivered to nobody", tg_id)
            return []
        return await self.send_card(
            CardKind.ACCESS,
            int(tg_id),
            recipients,
            render_access_card(
                tg_id,
                username=username,
                first_name=first_name,
                created_at=created_at,
            ),
            reply_markup=access_review_markup(int(tg_id)),
            parse_mode="HTML",
        )

    async def _store_card(
        self, kind: str, ref_id: int, chat_id: int, message_id: int
    ) -> None:
        """Persist one card copy so :meth:`sync_card` can edit it later."""
        sessionmaker = self._sessionmaker
        if sessionmaker is None:
            return
        try:
            async with sessionmaker() as session:
                await admin_cards_repo.add(
                    session,
                    kind=str(kind),
                    ref_id=int(ref_id),
                    chat_id=int(chat_id),
                    message_id=int(message_id),
                )
                await session.commit()
        except Exception:  # pragma: no cover - card bookkeeping is best effort
            logger.warning("failed to store card copy", exc_info=True)

    async def sync_card(
        self,
        kind: str,
        ref_id: int,
        text: str,
        *,
        reply_markup: Any | None = None,
    ) -> list[int]:
        """Edit every stored copy of a card; re-send when editing fails.

        Returns the chat ids that were updated. When a copy can no longer be
        edited (deleted message, stale token) a fresh message is sent and the
        stored ``message_id`` is re-pointed at it.
        """
        bot = self._bot
        sessionmaker = self._sessionmaker
        if bot is None or sessionmaker is None:
            return []
        async with sessionmaker() as session:
            copies = await admin_cards_repo.list_for(session, kind, ref_id)
        updated: list[int] = []
        for card in copies:
            try:
                await bot.edit_message_text(
                    text,
                    card.chat_id,
                    card.message_id,
                    reply_markup=reply_markup,
                )
            except Exception:
                ok, new_id = await self._deliver(
                    card.chat_id, text, reply_markup=reply_markup
                )
                if not ok or new_id is None:
                    continue
                async with sessionmaker() as session:
                    await admin_cards_repo.set_message_id(session, card.id, new_id)
                    await session.commit()
            updated.append(int(card.chat_id))
        return updated

    # --- error-signature rate limiter (M0-07.4 / M0-08.2) ------------------

    def allow_error_alert(
        self, signature: str, *, window: float = ERROR_ALERT_WINDOW
    ) -> bool:
        """Return ``True`` at most once per ``window`` for a given ``signature``."""
        now = self._clock()
        last = self._seen_errors.get(signature)
        if last is not None and now - last < window:
            return False
        self._seen_errors[signature] = now
        return True
