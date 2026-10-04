"""Declarative base, metadata naming convention and shared column types."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import MetaData, Text
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    """Return the current UTC time as a *naive* datetime.

    SQLite has no timezone type and drops ``tzinfo`` on read, so the whole app
    stores naive UTC values to keep writes and reads consistent
    (``TASK_PLAN.md`` §0.1 rule 8).
    """
    return datetime.now(UTC).replace(tzinfo=None)


# Deterministic constraint/index names so Alembic batch migrations on SQLite can
# recreate objects, and so tests can assert on names (``TASK_PLAN.md`` §2.4).
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base shared by every ORM model."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class JSONText(TypeDecorator[Any]):
    """Store arbitrary Python objects as JSON text.

    Round-trips dicts/lists/scalars through a plain ``TEXT`` column, keeping the
    schema portable across SQLite and other backends.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def process_result_value(self, value: str | None, dialect: Any) -> Any:
        if value is None:
            return None
        return json.loads(value)
