import asyncio
import uuid
import os
from dotenv import load_dotenv
from datetime import datetime

from telebot.async_telebot import AsyncTeleBot
from telebot.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    BotCommand
)

from py3xui import (
    AsyncApi,
    Client,
    Inbound
)

load_dotenv()

api = AsyncApi(os.getenv('DOMAIN'), token=os.getenv('VPN_TOKEN'))
bot = AsyncTeleBot(os.getenv('BOT_TOKEN'))

INBOUND_ID = int(os.getenv('INBOUND_ID'))
ADMIN_ID = int(os.getenv('ADMIN_ID'))


@bot.message_handler(commands='start')
async def start_handler(message: Message):
    # на этом этапе надо создавать юзера/либо проверять существование

    print(message.chat.id)

    tg_id: int = message.from_user.id
    username: str = message.from_user.username or "no_username"

    # С тех пор как ебанный api не совпадает с py3xui
    # new_client = await api.client.get_by_email(tg_id)
    # Поиск клиента будет через цикл

    inbound: Inbound = await api.inbound.get_by_id(INBOUND_ID)

    new_client: Client = None

    for _client in inbound.settings.clients:
        if _client.email == str(tg_id):
            new_client = _client
            break

    if new_client is not None:
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


@bot.message_handler(commands='profile')
async def profile_handler(message: Message):
    tg_id: int = message.from_user.id

    inbound: Inbound = await api.inbound.get_by_id(INBOUND_ID)
    existing_client: Client = None

    for _client in inbound.settings.clients:
        if _client.email == str(tg_id):
            existing_client: Client = _client
            break

    if existing_client is None:
        non_existent_warn = "У вас ещё нет аккаунта. Передите в /start."
        await bot.send_message(message.chat.id, non_existent_warn)
        return

    sub_token = existing_client.sub_id

    expiry_date = "Бессрочно"
    if existing_client.expiry_time > 0:
        expiry_date = datetime.fromtimestamp(
            existing_client.expiry_time / 1000
            ).strftime("%d.%m.%Y %H:%M")

    client_info = (
        "Моя подписка:\n"
        f"Статус: {"Активна" if existing_client.enable else "Неактивна"}\n"
        f"Ссылка: {os.getenv('SUB_URL_BASE')}{sub_token}\n"
        f"Действует до: {expiry_date}"
    )
    await bot.send_message(message.chat.id, client_info)


@bot.message_handler(commands='pay')
async def pay_handler(message):
    markup = InlineKeyboardMarkup(
        InlineKeyboardButton(
            text="Уведомить об оплате",
            callback_data="check_payment"
        )
    )

    await bot.send_message(
        message.chat.id,
        "Тут ссылка на совместный счёт.",
        reply_markup=markup)


@bot.callback_query_handler(func=lambda call: True)
async def callback_query(call: CallbackQuery):
    if call.data == "check_payment":
        await bot.send_message(
                call.message.chat.id, "Запрос обработается в течение часа.")

        user_info = f"ID: {call.from_user.id}"
        if call.from_user.username:
            user_info = f"@{call.from_user.username}"

        await bot.send_message(ADMIN_ID,
                               f"Пользователь {user_info} запросил подписку.")


@bot.message_handler(commands='support')
async def support_handler(message):
    await bot.reply_to(message, "За помощью обратиться к @snow")


async def main():
    await bot.delete_my_commands(scope=None)

    await bot.set_my_commands(
        commands=[
            BotCommand("start", "Войти в аккаунт"),
            BotCommand("profile", "Профиль"),
            BotCommand("pay", "Оплата"),
            BotCommand("support", "Служба поддержки")
        ],
        scope=None
    )

    await bot.polling()


if __name__ == "__main__":
    asyncio.run(main())
