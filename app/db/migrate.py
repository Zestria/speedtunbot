"""Programmatic Alembic runner used at application startup.

Alembic's async env calls ``asyncio.run`` internally, so this must be invoked
from a thread (``asyncio.to_thread``) when a loop is already running
(``TASK_PLAN.md`` §M0-02.19).
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config

logger = logging.getLogger(__name__)

# .../<repo>/app/db/migrate.py -> <repo>
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INI_PATH = _REPO_ROOT / "alembic.ini"
DEFAULT_SCRIPT_LOCATION = _REPO_ROOT / "migrations"


def make_config(
    db_url: str | None = None, ini_path: Path | str | None = None
) -> Config:
    """Build an Alembic ``Config`` wired to the project migrations."""
    cfg = Config(str(ini_path or DEFAULT_INI_PATH))
    cfg.set_main_option("script_location", str(DEFAULT_SCRIPT_LOCATION))
    if db_url is not None:
        cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def upgrade_head(db_url: str | None = None, ini_path: Path | str | None = None) -> None:
    """Apply all migrations up to ``head`` (blocking)."""
    command.upgrade(make_config(db_url, ini_path), "head")


def downgrade_base(
    db_url: str | None = None, ini_path: Path | str | None = None
) -> None:
    """Roll every migration back (blocking). Used by tests."""
    command.downgrade(make_config(db_url, ini_path), "base")
