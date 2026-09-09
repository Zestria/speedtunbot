import asyncio

from telebot.asyncio_filters import StateFilter
from telebot.types import (
    Message,
    BotCommand,
    BotCommandScopeChat
)

from middlewares import (
    ThrottlingMiddleware,
    MaintenanceMiddleware
)

from config import (
    ADMIN_IDS,
    IS_MAINTENANCE_MODE
)
from loads import (
    bot,
    is_maintenance_lock
)
from handlers import register_all_handlers

bot.add_custom_filter(StateFilter(bot))

bot.setup_middleware(ThrottlingMiddleware())
bot.setup_middleware(MaintenanceMiddleware())

register_all_handlers(bot)


@bot.message_handler(commands='maintenance')
async def maintenance_handler(message: Message):
    if message.from_user.id in ADMIN_IDS:
        async with is_maintenance_lock:
            global IS_MAINTENANCE_MODE
            IS_MAINTENANCE_MODE = not IS_MAINTENANCE_MODE
            await asyncio.sleep(0)
            text = "Режим тех. обслуживания выключен"
            if IS_MAINTENANCE_MODE:
                text = "Режим тех обслуживания включён"

            await bot.send_message(message.from_user.id, text)


# Админы могут написать важное объявление всем клиентам
@bot.message_handler(commands='broadcast')
async def broadcast_handler(message: Message):
    pass


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
    pass


# Бот пишет пользователю что он разбанен и восстанавливает ему доступ
# Хранение забаненных пользователей в json + загрузка в set() при запуске бота
@bot.message_handler(commands='unban')
async def unban_handler(message: Message):
    pass


@bot.message_handler(commands='banned_list')
async def banned_list_handler(message: Message):
    pass


async def main():
    await bot.delete_my_commands()

    commands = [
        BotCommand("start", "Войти в аккаунт"),
        BotCommand("profile", "Профиль"),
        BotCommand("pay", "Оплата"),
        BotCommand("support", "Служба поддержки")
    ]

    await bot.set_my_commands(commands)

    commands.append(
        BotCommand("maintenance", "Режим тех. обслуживания")
    )

    for admin_id in ADMIN_IDS:
        await bot.set_my_commands(
            commands=commands,
            scope=BotCommandScopeChat(admin_id)
        )

    await bot.polling()


if __name__ == "__main__":
    asyncio.run(main())
