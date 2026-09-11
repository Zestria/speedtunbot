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
            await bot.send_message(
                message.chat.id,
                "⚠️ У вас ещё нет аккаунта.\n\n"
                "Перейдите в /start для регистрации."
            )
            return

        sub_token: str = existing_client.sub_id

        status_text = "🟢 Активна" if existing_client.enable else "🔴 Неактивна"
        expiry_date = "Бессрочно"

        if not existing_client.enable:
            expiry_date = "Истекла"
        elif existing_client.expiry_time > 0:
            expiry_date = datetime.fromtimestamp(
                existing_client.expiry_time / 1000
            ).strftime("%d.%m.%Y %H:%M")

        client_info = (
            "📱 <b>Моя подписка</b>\n\n"
            f"Статус: {status_text}\n"
            f"Действует до: {expiry_date}\n\n"
            f"<b>Ссылка на подключение:</b>\n"
            f"<code>{SUB_URL_BASE}{sub_token}</code>\n\n"
            "Нажмите на ссылку или скопируйте её."
        )
        await bot.send_message(message.chat.id, client_info, parse_mode="HTML")
