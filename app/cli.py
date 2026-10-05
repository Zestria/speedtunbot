"""Command-line entrypoint (``TASK_PLAN.md`` §M0-11).

``python -m app.cli import-legacy [--dry-run]`` imports the 3x-ui panel clients
whose ``email`` is a Telegram id (plus the legacy ``BANNED_USERS_FILE`` list) into
the ``users`` table. Run it **before** starting the new bot on a deployment that
was created by the legacy bot: the startup guard
(:func:`app.app.ensure_legacy_import`) refuses to start otherwise (or imports
automatically when ``AUTO_IMPORT_LEGACY=true``).

The CLI reuses :class:`~app.services.legacy_import.LegacyImporter`, so a manual
import and the automatic one behave identically. It never imports ``loads.py`` or
``telebot`` — no bot session is needed and the bot may stay stopped.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from app.db.migrate import upgrade_head
from app.db.session import create_engine_and_sessionmaker, ensure_sqlite_dir
from app.logging_setup import configure_logging
from app.services.legacy_import import LegacyImporter
from app.services.panel import PanelGateway
from app.settings import Settings, get_settings

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_BAD_CONFIG = 2

logger = logging.getLogger(__name__)


async def import_legacy(settings: Settings, *, dry_run: bool = False) -> int:
    """Run the idempotent import against the configured database and panel."""
    ensure_sqlite_dir(settings.database_url)
    # Alembic's async env calls ``asyncio.run`` itself, so it needs a thread.
    await asyncio.to_thread(upgrade_head, settings.database_url)
    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url)
    try:
        importer = LegacyImporter(
            sessionmaker,
            panel=PanelGateway(settings),
            bank_details=settings.bank_account_details,
            banned_users_file=settings.banned_users_file,
        )
        report = await importer.run(dry_run=dry_run)
    finally:
        await engine.dispose()
    prefix = "DRY RUN — " if dry_run else ""
    print(f"{prefix}{report.summary}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser (extended by later M0-11 commands)."""
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="tgbot_vpn maintenance commands",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    importer = subparsers.add_parser(
        "import-legacy",
        help="import legacy panel clients / bans into the bot database",
    )
    importer.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would happen without changing anything",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the requested command (``0`` on success)."""
    args = build_parser().parse_args(argv)
    try:
        settings = get_settings()
    except Exception as exc:  # a bad .env must not print a traceback
        print(f"cannot load settings: {exc}", file=sys.stderr)
        return EXIT_BAD_CONFIG
    configure_logging(
        settings.log_level, secrets=[settings.bot_token, settings.vpn_token]
    )
    try:
        if args.command == "import-legacy":
            return asyncio.run(import_legacy(settings, dry_run=bool(args.dry_run)))
    except Exception as exc:  # actionable message for the operator
        logger.error("import-legacy failed: %s", exc, exc_info=True)
        print(f"import-legacy failed: {exc}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
