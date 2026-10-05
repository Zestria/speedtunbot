"""Update middlewares and their registration order (``TASK_PLAN.md`` §M0-05.8).

Order matters and is fixed here: **Context → Maintenance → Access → Throttle**.
``Context`` populates ``data["user"]``/``data["role"]``, which the later
middlewares rely on. ``Throttle`` is **last** on purpose: telebot skips
``post_process`` for every middleware as soon as one returns ``CancelUpdate``, so
a cancelling Maintenance/Access *after* Throttle would leak its in-flight lock.
With Throttle last it only ever acquires a lock on updates that actually reach a
handler (see ``docs/DECISIONS.md`` § *M0-05 — implementation*).
:func:`build_middlewares` is the single place that decides this order (asserted
by ``tests/test_middlewares.py``).
"""

from __future__ import annotations

from typing import Any

from telebot.asyncio_handler_backends import BaseMiddleware

from app.container import Container
from app.middlewares.access import AccessMiddleware
from app.middlewares.context import ContextMiddleware
from app.middlewares.maintenance import MaintenanceMiddleware
from app.middlewares.throttle import ThrottleMiddleware

__all__ = [
    "AccessMiddleware",
    "ContextMiddleware",
    "MaintenanceMiddleware",
    "ThrottleMiddleware",
    "build_middlewares",
]


def build_middlewares(container: Container, bot: Any = None) -> list[BaseMiddleware]:
    """Return the middlewares in their required registration order."""
    return [
        ContextMiddleware(container),
        MaintenanceMiddleware(container.settings_service, bot=bot),
        AccessMiddleware(bot=bot),
        ThrottleMiddleware(bot=bot),
    ]
