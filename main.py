import asyncio

from telebot.asyncio_filters import StateFilter
from telebot.types import (
    Message,
    BotCommand,
    BotCommandScopeChat
)

from middlewares import (
    ThrottlingMiddleware,
    MaintenanceMiddleware,
    BanMiddleware
)

from config import (
    ADMIN_IDS,
    IS_MAINTENANCE_MODE,
    BANNED_FILE
)
from loads import (
    bot,
    is_maintenance_lock,
    banned
)
from handlers import register_all_handlers
from utils import save_banned_users

bot.add_custom_filter(StateFilter(bot))

bot.setup_middleware(ThrottlingMiddleware())
bot.setup_middleware(MaintenanceMiddleware())
bot.setup_middleware(BanMiddleware())

register_all_handlers(bot)


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


async def main():
    await bot.delete_my_commands()

    commands = [
        BotCommand("start", "🚀 Запустить бота"),
        BotCommand("profile", "👤 Мой профиль"),
        BotCommand("pay", "💳 Оплата"),
        BotCommand("support", "💬 Поддержка")
    ]

    await bot.set_my_commands(commands)

    admin_commands = commands + [
        BotCommand("support_user", "✉️ Написать пользователю"),
        BotCommand("maintenance", "⚙️ Режим обслуживания"),
        BotCommand("ban", "🚫 Забанить"),
        BotCommand("unban", "✅ Разбанить"),
        BotCommand("banned_list", "📋 Список забаненных"),
    ]

    for admin_id in ADMIN_IDS:
        await bot.set_my_commands(
            commands=admin_commands,
            scope=BotCommandScopeChat(admin_id)
        )

    await bot.polling()


if __name__ == "__main__":
    asyncio.run(main())
