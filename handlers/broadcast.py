import asyncio

from telebot.async_telebot import AsyncTeleBot
from telebot.types import Message

from py3xui import Inbound

from config import (
    INBOUND_ID,
    ADMIN_IDS
)
from loads import (
    api,
    banned
)


def register_broadcast_handler(bot: AsyncTeleBot):
    @bot.message_handler(commands='broadcast')
    async def broadcast_handler(message: Message):
        if message.from_user.id not in ADMIN_IDS:
            return

        text = message.text.partition(' ')[2].strip()
        if not text:
            await bot.send_message(
                message.chat.id,
                "❌ Использование: /broadcast <текст>"
            )
            return

        try:
            inbound: Inbound = await api.inbound.get_by_id(INBOUND_ID)
        except Exception as e:
            await bot.send_message(
                message.chat.id,
                f"❌ Ошибка при работе с 3xui: {e}"
            )
            return

        sent = 0
        failed = 0
        for _client in inbound.settings.clients:
            if not _client.email.isdigit():
                continue

            recipied_id = int(_client.email)
            if recipied_id in banned:
                continue

            try:
                await bot.send_message(
                    recipied_id,
                    f"📢 <b>Объявление</b>\n\n{text}",
                    parse_mode="HTML"
                )
                sent += 1
            except Exception:
                failed += 1

            await asyncio.sleep(0.1)

            await bot.send_message(
                message.chat.id,
                f"✅ Рассылка завершена\n\n"
                f"Успешно: <b>{sent}</b>\n"
                f"Неудачно: <b>{failed}</b>",
                parse_mode="HTML"
            )
