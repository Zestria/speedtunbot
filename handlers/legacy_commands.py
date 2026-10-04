"""Legacy admin commands lifted verbatim out of ``main.py``.

These are the pre-M0 implementation and are **not** refactored here (M0-05 and
M0-09 port them onto ``app.services``). The code is moved as-is so that
``main.py`` can become a thin entrypoint. Note this file intentionally retains
bug B1 (``IS_MAINTENANCE_MODE`` is imported by value, so the toggle has no
effect on the middleware until M0-05 rewrites it).
"""

import asyncio

from telebot.async_telebot import AsyncTeleBot
from telebot.types import Message

from config import (
    ADMIN_IDS,
    IS_MAINTENANCE_MODE,
    BANNED_FILE,
)
from loads import (
    bot,
    is_maintenance_lock,
    banned,
)
from utils import save_banned_users


def register_legacy_admin_handlers(bot: AsyncTeleBot) -> None:
    @bot.message_handler(commands='maintenance')
    async def maintenance_handler(message: Message):
        if message.from_user.id in ADMIN_IDS:
            async with is_maintenance_lock:
                global IS_MAINTENANCE_MODE
                IS_MAINTENANCE_MODE = not IS_MAINTENANCE_MODE
                await asyncio.sleep(0)

                status = "🔴 <b>включён</b>"
                if not IS_MAINTENANCE_MODE:
                    status = "🟢 <b>выключен</b>"
                text = f"Режим техобслуживания {status}"
                await bot.send_message(
                    message.from_user.id,
                    text,
                    parse_mode="HTML"
                )

    # Админ может посмотреть список всех юзеров
    @bot.message_handler(commands='list')
    async def list_handler(message: Message):
        pass

    # Админ может посмотреть профиль пользователя по tg_id
    @bot.message_handler(commands='profile_of_user')
    async def profile_of_user_handler(message: Message):
        pass

    # Админ может банить пользователей
    # Бот пишет забаненному юзеру, что он забанен и больше не отвечает до разбана
    @bot.message_handler(commands='ban')
    async def ban_handler(message: Message):
        if message.from_user.id not in ADMIN_IDS:
            return

        parts = message.text.split()

        if len(parts) < 2 or not parts[1].isdigit():
            await bot.send_message(
                message.chat.id,
                "❌ <b>Неверный формат</b>\nИспользуйте: <code>/ban &lt;tg_id&gt;</code>",
                parse_mode="HTML"
            )
            return

        target_id = int(parts[1])

        if target_id in ADMIN_IDS:
            await bot.send_message(
                message.chat.id,
                "⚠️ Нельзя забанить администратора."
            )
            return
        banned.add(target_id)
        save_banned_users(banned, BANNED_FILE)

        try:
            await bot.send_message(
                target_id,
                "🚫 <b>Вы забанены</b>\n\n"
                "Вы были заблокированы администратором и больше не можете пользоваться ботом.",
                parse_mode="HTML"
            )
        except Exception as e:
            await bot.send_message(
                message.chat.id,
                f"⚠️ Ошибка отправки: {e}"
            )
        await bot.send_message(
            message.chat.id,
            f"✅ Пользователь <code>{target_id}</code> забанен.",
            parse_mode="HTML"
        )

    @bot.message_handler(commands='unban')
    async def unban_handler(message: Message):
        if message.from_user.id not in ADMIN_IDS:
            return

        parts = message.text.split()
        if len(parts) < 2 or not parts[1].isdigit():
            await bot.send_message(
                message.chat.id,
                "❌ <b>Неверный формат</b>\nИспользуйте: <code>/unban &lt;tg_id&gt;</code>",
                parse_mode="HTML"
            )
            return

        target_id = int(parts[1])

        if target_id not in banned:
            await bot.send_message(
                message.chat.id,
                "ℹ️ Этот пользователь не забанен."
            )
            return

        banned.discard(target_id)
        save_banned_users(banned, BANNED_FILE)

        try:
            await bot.send_message(
                target_id,
                "✅ <b>Вы разбанены</b>\n\nТеперь вы снова можете пользоваться ботом.",
                parse_mode="HTML"
            )
        except Exception:
            pass

        await bot.send_message(
            message.chat.id,
            f"✅ Пользователь <code>{target_id}</code> разбанен.",
            parse_mode="HTML"
        )

    @bot.message_handler(commands='banned_list')
    async def banned_list_handler(message: Message):
        if message.from_user.id not in ADMIN_IDS:
            return

        if not banned:
            await bot.send_message(
                message.chat.id,
                "📭 Список забаненных пуст."
            )
            return

        text = "<b>🚫 Забаненные пользователи:</b>\n\n" + "\n".join(
            f"<code>{tg_id}</code>" for tg_id in sorted(banned)
        )
        await bot.send_message(message.chat.id, text, parse_mode="HTML")
