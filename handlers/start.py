import uuid

from telebot.async_telebot import AsyncTeleBot
from telebot.types import (
    Message,
    Inbound,
    Client
)

from config import (
    INBOUND_ID,
    api
)


def register_start_handler(bot: AsyncTeleBot):

    @bot.message_handler(commands='start')
    async def start_command_handler(message: Message):
        # на этом этапе надо создавать юзера/либо проверять существование
        tg_id: int = message.from_user.id
        username: str = message.from_user.username or "no_username"

        # С тех пор как ебанный api не совпадает с py3xui
        # new_client = await api.client.get_by_email(tg_id)
        # Поиск клиента будет через цикл

        try:
            inbound: Inbound = await api.inbound.get_by_id(INBOUND_ID)

            is_client_exist: Client = None

            for _client in inbound.settings.clients:
                if _client.email == str(tg_id):
                    is_client_exist = _client
                    break

            if is_client_exist is not None:
                await bot.send_message(message.chat.id, "С возвращением!")
                return

            # клиент отсутствует, значит нужно создать нового
            new_client = Client(
                id=str(uuid.uuid4()),
                email=str(tg_id),
                enable=False,
                comment=f"@{username}"
            )
            await api.client.add(INBOUND_ID, [new_client])

            # иначе он enable=True изначально
            await api.client.update(new_client.id, new_client)

            await bot.send_message(message.chat.id, "Добро пожаловать!")
        except Exception as e:
            print(f"Ошибка при работе с 3xui: {e}")
