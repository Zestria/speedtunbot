"""Central ``cf:`` confirmation dispatcher (``TASK_PLAN.md`` §S1-5.1).

Every destructive button (link regeneration now, ban/delete/broadcast later)
mints a :class:`~app.callbacks.Confirmations` token and renders a
``[✅ Подтвердить][❌ Отмена]`` card whose payloads are ``cf:<token>`` /
``cf:x:<token>``. **One** callback handler serves them all: the action is not
encoded in the callback data (the token never carries it), it lives in the
server-side store, so a new action needs only a callable in :data:`ACTIONS` —
no new telebot handler that could be shadowed by first-match ordering
(§S1-5.1; the same reason S2-3/S2-5 rely on this indirection).

The handler always answers the callback query, so a crafted, replayed or
expired token leaves no spinner running and never raises into the poller (B5).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app import texts
from app.callbacks import Confirm, ConfirmResult, unpack
from app.container import Container
from app.errors import InvalidCallback
from app.ui import edit_or_send

logger = logging.getLogger(__name__)

#: Callback-data namespace owned by this handler.
NAMESPACE = f"{Confirm.ns}:"

#: Signature every registered action shares: ``(call, bot, container, *args)``.
Action = Callable[..., Awaitable[None]]
#: ``action name`` → handler. Populated by :func:`register_action` at import.
ACTIONS: dict[str, Action] = {}


def register_action(name: str) -> Callable[[Action], Action]:
    """Register ``func`` as the handler of confirmations minted for ``name``."""

    def decorate(func: Action) -> Action:
        ACTIONS[name] = func
        return func

    return decorate


async def confirm_callback(call: Any, bot: Any, container: Container) -> None:
    """Consume a ``cf:`` token and run its action, else answer a stale toast."""
    try:
        payload = unpack(getattr(call, "data", None))
    except InvalidCallback:
        await _answer(bot, call, texts.ERROR_STALE_BUTTON)
        return
    if not isinstance(payload, Confirm):
        await _answer(bot, call, texts.ERROR_STALE_BUTTON)
        return

    store = container.confirmations
    if store is None:  # pragma: no cover - container is wired at startup
        await _answer(bot, call, texts.ERROR_GENERIC)
        return

    tg_id = int(getattr(call.from_user, "id", 0))
    if payload.cancel:
        store.cancel(payload.token, tg_id)
        chat_id, message_id = _target(call)
        await edit_or_send(bot, chat_id, message_id, texts.CONFIRM_CANCELLED)
        await _answer(bot, call, texts.CALLBACK_CANCELLED)
        return

    # The token never encodes the action, so look it up first. ``peek`` leaves
    # the token live: a wrong user (or an unknown action) burns nothing.
    action = store.peek(payload.token, tg_id)
    if action is None:
        await _answer(bot, call, texts.ERROR_CONFIRM_EXPIRED)
        return
    handler = ACTIONS.get(action)
    if handler is None:  # pragma: no cover - every minted action is registered
        await _answer(bot, call, texts.ERROR_STALE_BUTTON)
        return

    result = store.consume(payload.token, tg_id, action)
    if result.status is ConfirmResult.FORBIDDEN:
        await _answer(bot, call, texts.ERROR_ACCESS_DENIED)
        return
    if not result.ok:
        await _answer(bot, call, texts.ERROR_CONFIRM_EXPIRED)
        return
    await handler(call, bot, container, *result.args)


def _target(call: Any) -> tuple[int, int]:
    """Return ``(chat_id, message_id)`` of the message the callback came from."""
    message = call.message
    return int(message.chat.id), int(message.message_id)


async def _answer(bot: Any, call: Any, text: str | None) -> None:
    """Answer the callback query without ever raising."""
    try:
        await bot.answer_callback_query(getattr(call, "id", None), text)
    except Exception:  # pragma: no cover - cosmetic
        logger.debug("failed to answer callback", exc_info=True)


def register_confirm_handler(bot: Any, container: Container) -> None:
    """Register the single ``cf:`` callback dispatcher on ``bot``."""

    @bot.callback_query_handler(
        func=lambda call: (getattr(call, "data", "") or "").startswith(NAMESPACE)
    )
    async def _callback(call: Any) -> None:  # pragma: no cover - thin adapter
        await confirm_callback(call, bot, container)


__all__ = [
    "ACTIONS",
    "NAMESPACE",
    "confirm_callback",
    "register_action",
    "register_confirm_handler",
]
