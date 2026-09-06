import asyncio
import uuid
import os
import json
import time
from cachetools import TTLCache
from dotenv import load_dotenv
from datetime import datetime

from telebot.async_telebot import AsyncTeleBot
from telebot.asyncio_storage import StateMemoryStorage
from telebot.asyncio_filters import StateFilter
from telebot.util import quick_markup
from telebot.types import (
    Message,
    CallbackQuery,
    BotCommand,
    BotCommandScopeChat
)
from telebot.asyncio_handler_backends import (
    BaseMiddleware,
    CancelUpdate
)
from telebot.asyncio_handler_backends import (
    State,
    StatesGroup
)

from py3xui import (
    AsyncApi,
    Client,
    Inbound
)

load_dotenv()

api = AsyncApi(os.getenv('DOMAIN'), token=os.getenv('VPN_TOKEN'))
bot = AsyncTeleBot(os.getenv('BOT_TOKEN'))
state_storage = StateMemoryStorage()
busy_users = set()

INBOUND_ID = int(os.getenv('INBOUND_ID'))
ADMIN_IDS = json.loads(os.getenv('ADMIN_IDS'))
IS_MAINTENANCE_MODE = False
RATES = [
    {"name": "30 дней - 150 рублей", "days": 30, "price": 150},
    {"name": "60 дней - 250 рублей", "days": 60, "price": 250},
    {"name": "90 дней - 350 рублей", "days": 90, "price": 350}
]
BANK_ACCOUNT_DETAILS = os.getenv("BANK_ACCOUNT_DETAILS")


def calculate_expiry_time(old_expiry_time: int | None, days: int) -> int:
    current_time_ms = int(time.time() * 1000)
    if old_expiry_time is None:
        old_expiry_time = current_time_ms

    base_time = max(current_time_ms, old_expiry_time)

    duration_ms = days * 24 * 60 * 60 * 1000
    return base_time + duration_ms


def compose_rates_text(rates):
    text = ""
    for i in range(0, len(rates)):
        text += f"{i+1}) {rates[i]['name']}"
        if i != len(rates) - 1:
            text += "\n"
    return text


user_limit = TTLCache(maxsize=10000, ttl=1.0)

bot.add_custom_filter(StateFilter(bot))


class UserStates(StatesGroup):
    waiting_for_name = State()
    pending_confirmation = State()


class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self):
        self.update_types = ['message', 'callback_query']

    async def pre_process(self, message: Message, data):
        user_id = message.from_user.id

        if user_id in busy_users:
            return CancelUpdate()

        busy_users.add(user_id)

    async def post_process(self, message: Message, data, exception):
        busy_users.discard(message.from_user.id)


class MaintenanceMiddleware(BaseMiddleware):
    def __init__(self):
        self.update_types = ['message', 'callback_query']

    async def pre_process(self, message: Message, data):
        user_id = message.from_user.id

        if IS_MAINTENANCE_MODE and user_id not in ADMIN_IDS:
            await bot.send_message(
                user_id,
                "Бот на техническом обслуживании."
            )
            return CancelUpdate()

    async def post_process(self, message, data, exception):
        pass


bot.setup_middleware(ThrottlingMiddleware())
bot.setup_middleware(MaintenanceMiddleware())


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
async def pay_command_handler(message: Message):
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

    select_rate_markup = quick_markup({
        str(i+1): {"callback_data": f"pay:select_rate:{i+1}"}
        for i in range(0, len(RATES))
    }, row_width=3)

    await bot.send_message(
        message.chat.id,
        compose_rates_text(RATES),
        reply_markup=select_rate_markup)


# Отлично, реализовал более подробную заявку для подписки
# Теперь нужно добавить:
# - отображение: rate_id <-> text
# - заморозка аккаунта до подтверждения
# - автоматическая выдача подписки в случае одобрения пользователя
@bot.callback_query_handler(
    func=lambda call: call.data.startswith('pay:select_rate')
)
async def cb_pay_select_rate_handler(call: CallbackQuery):
    param = call.data.split(":")[2]

    await bot.answer_callback_query(call.id)

    await bot.set_state(
        call.from_user.id,
        UserStates.waiting_for_name,
        call.message.chat.id
    )

    async with bot.retrieve_data(
        call.from_user.id,
        call.message.chat.id
    ) as data:
        data["rate_id"] = param

    await bot.edit_message_text(
        "Напишите ФИО и банк при оплате в виде:\n"
        "Иван Иванович И., тбанк",
        call.message.chat.id,
        call.message.id
    )


@bot.message_handler(state=UserStates.waiting_for_name)
async def process_user_credentials(message: Message):
    credentials: str = message.text

    # Чтобы память не закончилась
    if len(credentials) > 100:
        credentials = credentials[:100]

    async with bot.retrieve_data(
        message.from_user.id,
        message.chat.id
    ) as data:
        data["credentials"] = credentials
        rate_id = data.get("rate_id")

    await bot.send_message(
        message.chat.id,
        f"Переведите {RATES[int(rate_id)-1]["price"]} рублей"
        f" на этот счёт {BANK_ACCOUNT_DETAILS}\n"
        "Как оплатите, нажмите готово. Иначе отмените.",
        reply_markup=quick_markup({
            "Готово": {"callback_data": "pay:user_confirm:accept"},
            "Отмена": {"callback_data": "pay:user_confirm:decline"}
            }, row_width=2)
        )

    # интересно можно ли убрать waiting_for_name, не удалив при этом остальное.
    # надо будет затестить


@bot.callback_query_handler(
    func=lambda call: call.data.startswith('pay:user_confirm')
)
async def cb_pay_user_confirm_handler(call: CallbackQuery):
    param = call.data.split(":")[2]

    if param == "accept":
        await bot.edit_message_text(
            "Заявка принята на рассмотрение. "
            "По её решению Вам придёт ответ.",
            call.message.chat.id,
            call.message.id
        )
        # тут бы похорошему заморозить следующие заявки юзера

        user_info = f"ID: {call.from_user.id}"
        if call.from_user.username:
            user_info = f"@{call.from_user.username}"

        async with bot.retrieve_data(
            call.from_user.id,
            call.message.chat.id
        ) as data:
            credentials = data.get("credentials")
            rate_id = data.get("rate_id")

        give_sub_markup = quick_markup({
            "Одобрить": {
                "callback_data": "pay:admin_confirm:accept:"
                f"{call.from_user.id}:{call.message.chat.id}"
            },
            "Отклонить": {
                "callback_data": "pay:admin_confirm:decline:"
                f"{call.from_user.id}:{call.message.chat.id}"
            }
        }, row_width=2)

        # Что делать в случае если один уже одобрил подписку
        # Но другой админ тоже решил нажать позже
        for admin_id in ADMIN_IDS:
            await bot.send_message(
                admin_id,
                f"Пользователь {user_info} запросил подписку.\n"
                f"Его реквезиты: {credentials}.\n"
                f"ID подписки: {rate_id}\n",
                reply_markup=give_sub_markup
            )
    elif param == "decline":
        await bot.edit_message_text(
            "Оформление подписки отменено.",
            call.message.chat.id,
            call.message.id
        )

        await bot.delete_state(call.from_user.id, call.message.chat.id)


@bot.callback_query_handler(
    func=lambda call: call.data.startswith('pay:admin_confirm')
)
async def cb_payment_handler(call: CallbackQuery):
    query = call.data.split(":")
    param = query[2]
    user_id = int(query[3])
    chat_id = int(query[4])

    await bot.answer_callback_query(call.id)

    if param == "accept":
        # Нужно автоматически с p3xui выдать подписку
        # Используя состояние юзера
        # rate_id
        # Где то в коде нужно сделать удобный словарик
        # где по id подписки выдаётся текст и время в формате для p3xui

        async with bot.retrieve_data(
            user_id, chat_id
        ) as data:
            rate_id: str = data.get("rate_id")
            credentials = data.get("credentials")

        if not rate_id or int(rate_id) > len(RATES):
            await bot.send_message(
                call.message.chat.id,
                "Тариф не найден или сессия истекла."
            )
            await bot.delete_state(user_id, chat_id)
            return

        rate_info = RATES[int(rate_id)-1]

        try:
            inbound: Inbound = await api.inbound.get_by_id(INBOUND_ID)
            client = None
            for _client in inbound.settings.clients:
                if _client.email == str(user_id):
                    client = _client
                    break

            if client:
                client.enable = True
                client.expiry_time = calculate_expiry_time(
                    client.expiry_time, rate_info["days"]
                )
                await api.client.update(client.id, client)

                await bot.send_message(
                    chat_id,
                    "Ваша подписка успешно активирована!\n"
                    f"Тариф: {rate_info['name']}\n"
                    "Срок действия обновлён."
                )

                await bot.edit_message_text(
                    f"Подписка ({rate_info['name']}) успешно выдана.\n"
                    f"Пользователь: {user_id}."
                    f"Реквезиты: {credentials}.",
                    call.message.chat.id,
                    call.message.id
                )

                await bot.delete_state(user_id, chat_id)
        except Exception as e:
            await bot.send_message(
                call.message.chat.id,
                f"Ошибка при работе с 3xui: {e}"
            )
    elif param == "decline":
        await bot.edit_message_text(
            "Заявка пользователя отклонена.",
            call.message.chat.id,
            call.message.id
        )
        await bot.send_message(chat_id, "Ваша заявка была отклонена.")
        await bot.delete_state(user_id, chat_id)


@bot.message_handler(commands='support')
async def support_handler(message):
    await bot.reply_to(message, "За помощью обратиться к @snow")


@bot.message_handler(commands='maintenance')
async def maintenance_handler(message: Message):
    if message.from_user.id in ADMIN_IDS:
        global IS_MAINTENANCE_MODE
        IS_MAINTENANCE_MODE = not IS_MAINTENANCE_MODE
        text = "Режим тех. обслуживания выключен"
        if IS_MAINTENANCE_MODE:
            text = "Режим тех обслуживания включён"

        await bot.send_message(message.from_user.id, text)


# Реализация с помощью StateStorage, доступ можно иметь только админам

@bot.message_handler(commands='list')
async def list_handler(message: Message):
    pass


@bot.message_handler(commands='profile_of_user')
async def profile_of_user_handler(message: Message):
    pass


@bot.message_handler(commands='ban')
async def ban_handler(message: Message):
    pass


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
