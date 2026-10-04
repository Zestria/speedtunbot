"""Alembic async environment (``TASK_PLAN.md`` §M0-02.15).

Uses ``render_as_batch=True`` so SQLite schema changes are recreated safely.
The database URL is resolved in this order:

1. ``-x db_url=...`` argument,
2. ``DATABASE_URL`` environment variable,
3. ``sqlalchemy.url`` from ``alembic.ini``.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig
from typing import Literal

from alembic import context
from alembic.autogenerate.api import AutogenContext
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.db import models  # noqa: F401  (register tables on Base.metadata)
from app.db.base import Base, JSONText

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False is essential: without it, running a
    # migration (e.g. at startup or in tests) would silence the app's own
    # loggers that are not listed in alembic.ini.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def render_item(
    type_: str, obj: object, autogen_context: AutogenContext
) -> str | Literal[False]:
    """Render the project's custom types with a proper import in migrations."""
    if type_ == "type" and isinstance(obj, JSONText):
        autogen_context.imports.add("from app.db.base import JSONText")
        return "JSONText()"
    return False


def _resolve_url() -> str:
    override = context.get_x_argument(as_dictionary=True).get("db_url")
    if override:
        return override
    env_url = os.getenv("DATABASE_URL")
    if env_url:
        return env_url
    return config.get_main_option("sqlalchemy.url") or ""


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live DB connection."""
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    connectable = async_engine_from_config(
        {"sqlalchemy.url": _resolve_url()},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        future=True,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
