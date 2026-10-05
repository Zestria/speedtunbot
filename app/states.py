"""Conversation states (``TASK_PLAN.md`` §M0-09).

Payments no longer use the FSM (M0-10); the only remaining state machine is the
support relay: ``/support`` puts a user into :attr:`UserStates.waiting_for_help`
and ``/support_user <id>`` puts staff into :attr:`UserStates.writing_to_user`.

Kept in ``app/`` (not ``config``) so handlers never import the legacy modules.
"""

from __future__ import annotations

from telebot.asyncio_handler_backends import State, StatesGroup


class UserStates(StatesGroup):
    """States used by the support handlers."""

    waiting_for_help = State()
    writing_to_user = State()
    #: Admin users screen: waiting for a Telegram ID / ``@username`` (§S2-2.7).
    admin_search = State()
    #: Admin user card: waiting for a hand-typed day count (§S2-3.6).
    admin_grant = State()
