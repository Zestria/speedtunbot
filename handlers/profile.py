from datetime import datetime

from telebot.async_telebot import AsyncTeleBot
from telebot.types import Message

from py3xui import (
    Inbound,
    Client
)

from config import (
    SUB_URL_BASE,
    INBOUND_ID,
    ADMIN_IDS
)
from loads import api


def register_profile_handler(bot: AsyncTeleBot):
    @bot.message_handler(commands='profile')
    async def profile_command_handler(message: Message):
        tg_id: int = message.from_user.id

        try:
            inbound: Inbound = await api.inbound.get_by_id(INBOUND_ID)
        except Exception as e:
            for admin_id in ADMIN_IDS:
                bot.send_message(
                    admin_id,
                    f"Ошибка в 3xui: {e}"
                )
        existing_client: Client = None

        for _client in inbound.settings.clients:
            if _client.email == str(tg_id):
                existing_client: Client = _client
                break

        if existing_client is None:
            non_existent_warn = "У вас ещё нет аккаунта. Передите в /start."
            await bot.send_message(message.chat.id, non_existent_warn)
            return

        sub_token: str = existing_client.sub_id

        expiry_date = "Бессрочно"
        if not existing_client.enable:
            expiry_date = "Закончилась"
        elif existing_client.expiry_time > 0:
            expiry_date = datetime.fromtimestamp(
                existing_client.expiry_time / 1000
                ).strftime("%d.%m.%Y %H:%M")

        client_info = (
            "Моя подписка:\n"
            f"Статус: {'Активна' if existing_client.enable else 'Неактивна'}\n"
            f"Ссылка: {SUB_URL_BASE}{sub_token}\n"
            f"Действует до: {expiry_date}"
        )
        await bot.send_message(message.chat.id, client_info)
