"""Legacy import + startup guard tests (``TASK_PLAN.md`` §M0-11, bug-fix pass).

Acceptance criteria covered:

* every panel client whose ``email`` is numeric becomes a ``users`` row, idempotently;
* legacy clients created disabled with ``expiry_time=0`` get an explicit ``now``
  expiry, so they are never read as lifetime ("unlimited") subscriptions;
* ids listed in the legacy ban file become ``blocked`` and their client is disabled;
* ``--dry-run`` reports without writing anything, and bank details seed once;
* startup refuses an empty ``users`` table while the panel still carries clients,
  unless ``AUTO_IMPORT_LEGACY=true`` (then it imports automatically).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.app import ensure_legacy_import
from app.container import Container
from app.db.models import UserStatus
from app.db.repositories import settings as settings_repo
from app.db.repositories import users as users_repo
from app.services.legacy_import import (
    BANK_DETAILS_KEY,
    LegacyImporter,
    parse_username,
)
from app.services.subscriptions import MS_PER_DAY, now_ms
from tests.fakes import FakePanel

LEGACY = 1001
PLACEHOLDER = 1002
BANNED = 1003


def panel_of(container: Container) -> FakePanel:
    """Return the container's :class:`FakePanel` (typed)."""
    panel = container.panel
    assert isinstance(panel, FakePanel)
    return panel


def make_importer(
    container: Container,
    *,
    bans: Path | None = None,
    bank_details: str | None = None,
) -> LegacyImporter:
    """Build an importer over the container's sessionmaker + panel."""
    return LegacyImporter(
        container.sessionmaker,
        panel=panel_of(container),  # type: ignore[arg-type]
        bank_details=bank_details,
        banned_users_file=None if bans is None else str(bans),
    )


def write_bans(tmp_path: Path, ids: list[int]) -> Path:
    """Write a legacy ``banned_users.json`` next to ``tmp_path``."""
    path = tmp_path / "banned_users.json"
    path.write_text(json.dumps(ids), encoding="utf-8")
    return path


async def user_status(
    factory: async_sessionmaker[AsyncSession], tg_id: int
) -> str | None:
    """Return the stored ``users.status`` (``None`` when there is no row)."""
    async with factory() as session:
        user = await users_repo.get(session, tg_id)
    return None if user is None else str(user.status)


def test_parse_username() -> None:
    """The legacy ``comment`` carries ``@username`` (or nothing)."""
    assert parse_username("@neo") == "neo"
    assert parse_username("neo") == "neo"
    assert parse_username("  @neo  ") == "neo"
    assert parse_username("") is None
    assert parse_username(None) is None
    assert parse_username("@") is None


# --- the import -----------------------------------------------------------


async def test_import_creates_users_and_pins_placeholders(
    handler_container: Container, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """AC: numeric panel clients become users; a placeholder gets a finite expiry."""
    panel = panel_of(handler_container)
    seeded = panel.seed(LEGACY, expiry_ms=now_ms() + 10 * MS_PER_DAY)
    seeded.comment = "@neo"
    panel.seed(PLACEHOLDER, expiry_ms=0, enable=False)

    report = await make_importer(handler_container).run()

    assert report.panel_clients == 2
    assert report.created == 2
    assert report.banned_imported == 0
    assert report.normalized == [PLACEHOLDER]
    assert await user_status(session_factory, LEGACY) == UserStatus.APPROVED
    assert await user_status(session_factory, PLACEHOLDER) == UserStatus.APPROVED
    async with session_factory() as session:
        user = await users_repo.get(session, LEGACY)
    assert user is not None and user.username == "neo" and user.panel_client_uuid
    pinned = await panel.get_client(PLACEHOLDER)
    assert pinned is not None
    assert int(pinned.expiry_time) > 0  # no longer "0 = unlimited"
    assert bool(pinned.enable) is False


async def test_import_is_idempotent(
    handler_container: Container, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """AC: a second run changes nothing (no duplicates, no re-pinning)."""
    panel = panel_of(handler_container)
    panel.seed(LEGACY, expiry_ms=now_ms() + 10 * MS_PER_DAY)
    panel.seed(PLACEHOLDER, expiry_ms=0, enable=False)
    await make_importer(handler_container).run()
    first = await panel.get_client(PLACEHOLDER)
    assert first is not None

    report = await make_importer(handler_container).run()

    assert report.created == 0
    assert report.refreshed == 2
    assert report.normalized == []  # already pinned on the previous run
    second = await panel.get_client(PLACEHOLDER)
    assert second is not None and int(second.expiry_time) == int(first.expiry_time)
    async with session_factory() as session:
        user = await users_repo.get(session, PLACEHOLDER)
    assert user is not None


async def test_import_marks_and_disables_banned_ids(
    handler_container: Container,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """AC: the legacy ban list becomes ``blocked`` + a disabled panel client."""
    panel = panel_of(handler_container)
    panel.seed(BANNED, expiry_ms=now_ms() + MS_PER_DAY, enable=True)
    bans = write_bans(tmp_path, [BANNED])

    report = await make_importer(handler_container, bans=bans).run()

    assert report.banned_imported == 1
    assert report.banned_disabled == 1


async def test_import_dry_run_changes_nothing(
    handler_container: Container,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """AC: ``--dry-run`` reports what it would do without writing anywhere."""
    panel = panel_of(handler_container)
    panel.seed(LEGACY, expiry_ms=now_ms() + MS_PER_DAY, enable=True)
    panel.seed(PLACEHOLDER, expiry_ms=0, enable=False)
    bans = write_bans(tmp_path, [LEGACY])

    report = await make_importer(handler_container, bans=bans).run(dry_run=True)

    assert report.created == 2
    assert report.normalized == [PLACEHOLDER]
    assert report.banned_imported == 1
    assert await user_status(session_factory, LEGACY) is None
    assert await user_status(session_factory, PLACEHOLDER) is None
    placeholder = await panel.get_client(PLACEHOLDER)
    legacy = await panel.get_client(LEGACY)
    assert placeholder is not None and int(placeholder.expiry_time) == 0
    assert legacy is not None and bool(legacy.enable) is True


async def test_import_seeds_bank_details_once(
    handler_container: Container, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """AC: ``BANK_ACCOUNT_DETAILS`` seeds ``settings.bank_details`` exactly once."""
    report = await make_importer(handler_container, bank_details="Сбер 1234").run()

    assert report.bank_details_seeded is True
    async with session_factory() as session:
        assert await settings_repo.get(session, BANK_DETAILS_KEY) == "Сбер 1234"

    again = await make_importer(handler_container, bank_details="Сбер 1234").run()
    assert again.bank_details_seeded is False


# --- startup guard --------------------------------------------------------


async def test_startup_guard_refuses_an_unimported_deployment(
    handler_container: Container,
) -> None:
    """AC: empty ``users`` + panel clients → startup fails with instructions."""
    panel_of(handler_container).seed(LEGACY, expiry_ms=now_ms() + MS_PER_DAY)

    with pytest.raises(RuntimeError) as error:
        await ensure_legacy_import(handler_container)

    assert "import-legacy" in str(error.value)
    assert "AUTO_IMPORT_LEGACY=true" in str(error.value)


async def test_startup_guard_imports_when_the_flag_is_set(
    handler_container: Container, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """AC: with ``AUTO_IMPORT_LEGACY=true`` the guard imports instead of failing."""
    panel_of(handler_container).seed(LEGACY, expiry_ms=now_ms() + MS_PER_DAY)
    handler_container.settings.auto_import_legacy = True

    created = await ensure_legacy_import(handler_container)

    assert created == 1
    assert await user_status(session_factory, LEGACY) == UserStatus.APPROVED


async def test_startup_guard_skips_a_fresh_deployment(
    handler_container: Container,
) -> None:
    """AC: an empty DB with an empty panel is a normal first start."""
    assert await ensure_legacy_import(handler_container) == 0


async def test_startup_guard_skips_an_unreachable_panel(
    handler_container: Container,
) -> None:
    """AC: a panel outage must never block startup (the guard cannot know)."""
    panel = panel_of(handler_container)
    panel.seed(LEGACY, expiry_ms=now_ms() + MS_PER_DAY)
    panel.unavailable = True

    assert await ensure_legacy_import(handler_container) == 0


async def test_startup_guard_is_a_no_op_once_users_exist(
    handler_container: Container, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """AC: the guard never touches a database that already has users."""
    panel_of(handler_container).seed(LEGACY, expiry_ms=now_ms() + MS_PER_DAY)
    async with session_factory() as session:
        await users_repo.upsert_from_telegram(session, 4242, username="neo")
        await session.commit()

    assert await ensure_legacy_import(handler_container) == 0
    assert await user_status(session_factory, LEGACY) is None
