"""Startup orchestration (``TASK_PLAN.md`` §2.8).

M0-01 only wires the new scaffolding (settings, logging, container, bot
username) and then delegates to the legacy startup that earlier tasks will port
incrementally. Legacy imports are performed inside :func:`run` so that importing
this module has no side effects (no traceback at import).
"""

from __future__ import annotations

import asyncio
import logging

from app.container import Container
from app.logging_setup import configure_logging
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)


def build_container(settings: Settings) -> Container:
    """Build the process-wide container from the loaded settings."""
    return Container(settings=settings)


def _warn_if_local_sub_url(settings: Settings) -> None:
    """Startup never blocks on a questionable ``SUB_URL_BASE`` — warn only."""
    if "localhost" in settings.sub_url_base or "127.0.0.1" in settings.sub_url_base:
        logger.warning(
            "SUB_URL_BASE points at a local host (%s); subscription links will "
            "only work on this machine.",
            settings.sub_url_base,
        )


async def _publish_command_menus(bot: object, owner_ids: list[int]) -> None:
    """Publish the legacy command menus (ported to per-role menus in M1-07)."""
    from telebot.types import BotCommand, BotCommandScopeChat

    await bot.delete_my_commands()  # type: ignore[attr-defined]

    commands = [
        BotCommand("start", "🚀 Запустить бота"),
        BotCommand("profile", "👤 Мой профиль"),
        BotCommand("pay", "💳 Оплата"),
        BotCommand("support", "💬 Поддержка"),
    ]
    await bot.set_my_commands(commands)  # type: ignore[attr-defined]

    admin_commands = commands + [
        BotCommand("support_user", "✉️ Написать пользователю"),
        BotCommand("maintenance", "⚙️ Режим обслуживания"),
        BotCommand("ban", "🚫 Забанить"),
        BotCommand("unban", "✅ Разбанить"),
        BotCommand("banned_list", "📋 Список забаненных"),
    ]
    for owner_id in owner_ids:
        await bot.set_my_commands(  # type: ignore[attr-defined]
            commands=admin_commands,
            scope=BotCommandScopeChat(owner_id),
        )


async def run() -> None:
    """Load config, configure logging, build the container, then poll."""
    settings = get_settings()
    configure_logging(
        settings.log_level,
        secrets=[settings.bot_token, settings.vpn_token],
    )
    container = build_container(settings)
    _warn_if_local_sub_url(settings)
    logger.info("Starting bot (timezone=%s)", settings.timezone)

    # Legacy startup, imported late to avoid import-time side effects.
    from telebot.asyncio_filters import StateFilter

    from handlers import register_all_handlers
    from loads import bot
    from middlewares import (
        BanMiddleware,
        MaintenanceMiddleware,
        ThrottlingMiddleware,
    )

    bot.add_custom_filter(StateFilter(bot))
    bot.setup_middleware(ThrottlingMiddleware())
    bot.setup_middleware(MaintenanceMiddleware())
    bot.setup_middleware(BanMiddleware())
    register_all_handlers(bot)

    me = await bot.get_me()
    container.bot_username = me.username
    logger.info("Bot is @%s", container.bot_username)

    await _publish_command_menus(bot, settings.owner_ids)
    await bot.polling()


def main() -> None:
    """Synchronous entrypoint used by ``main.py``."""
    asyncio.run(run())
