"""Seed helpers run at startup (``TASK_PLAN.md`` §M0-02.18/§2.8).

Seeding is idempotent: existing rows are never overwritten.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import settings as settings_repo
from app.db.repositories import tariffs as tariffs_repo

logger = logging.getLogger(__name__)


async def seed_all(session: AsyncSession) -> tuple[int, int]:
    """Seed tariffs and default settings.

    Returns ``(inserted_settings, inserted_tariffs)`` counts (already-present
    rows are left untouched, so a second run inserts nothing).
    """
    inserted_settings = await settings_repo.seed_defaults(session)
    inserted_tariffs = await tariffs_repo.seed_defaults(session)
    if inserted_settings or inserted_tariffs:
        logger.info(
            "Seeded database (settings=%d, tariffs=%d)",
            inserted_settings,
            inserted_tariffs,
        )
    return inserted_settings, inserted_tariffs
