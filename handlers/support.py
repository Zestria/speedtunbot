from telebot.async_telebot import AsyncTeleBot


def register_support_handler(bot: AsyncTeleBot):

    @bot.message_handler(commands='support')
    async def support_handler(message):
        # ПОльзователь входит в состояние waiting_for_help
        # Затем все его сообщения перенаправляются админам
        # Админы могут ответить ему, командой support_user tg_id
        # Админ переходить в состояние helping_user
        # В таком случае все сообщения от имени бота будут отправляться от юзер
        # Юзер и админ могут удалить свои состояния
        await bot.reply_to(message, "За помощью обратиться к @snow")
