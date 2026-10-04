"""Explicit dependency container.

Replaces the ``loads.py`` import-time singletons with an object that is built
once during startup and passed to the handler factories as ``c``
(``TASK_PLAN.md`` §2.1 / §2.8).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.settings import Settings


@dataclass
class Container:
    """Shared, mutable application state assembled at startup."""

    settings: Settings
    # Cached bot username used to build deep links, e.g.
    # ``https://t.me/<bot_username>?start=...`` (§2.8 step 3). Populated from
    # ``bot.get_me()`` exactly once during startup.
    bot_username: str | None = None
