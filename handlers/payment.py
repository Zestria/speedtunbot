from telebot.async_telebot import AsyncTeleBot
from telebot.types import (
    Message,
    CallbackQuery
)

from py3xui import (
    Inbound,
    Client
)

from telebot.util import quick_markup

from config import (
    INBOUND_ID,
    ADMIN_IDS,
    RATES,
    BANK_ACCOUNT_DETAILS,
    UserStates
)
from loads import api
from utils import (
    calculate_expiry_time,
    compose_rates_text,
    get_rate_by_id
)


def register_payment_handler(bot: AsyncTeleBot):
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

    @bot.callback_query_handler(
        func=lambda call: call.data.startswith('pay:select_rate')
    )
    async def cb_pay_select_rate_handler(call: CallbackQuery):
        param = call.data.split(":")[2]

        rate_info = get_rate_by_id(param)
        if rate_info is None:
            await bot.answer_callback_query(
                call.id,
                "Такого тарифа не существует."
            )
            return

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

    @bot.message_handler(
        state=UserStates.waiting_for_name,
        content_types='text'
    )
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

        rate_info = get_rate_by_id(rate_id)
        if rate_info is None:
            await bot.send_message(
                message.chat.id,
                "Тариф не найден или сессия истекла."
            )
            await bot.delete_state(message.from_user.id, message.chat.id)
            return

        await bot.send_message(
            message.chat.id,
            f"Переведите {rate_info['price']} рублей"
            f" на этот счёт {BANK_ACCOUNT_DETAILS}\n"
            "Как оплатите, нажмите готово. Иначе отмените.",
            reply_markup=quick_markup({
                "Готово": {"callback_data": "pay:user_confirm:accept"},
                "Отмена": {"callback_data": "pay:user_confirm:decline"}
                }, row_width=2)
            )

        await bot.set_state(
            message.from_user.id,
            UserStates.awaiting_payment,
            message.chat.id
        )

    @bot.callback_query_handler(
        func=lambda call: call.data.startswith('pay:user_confirm')
    )
    async def cb_pay_user_confirm_handler(call: CallbackQuery):
        param = call.data.split(":")[2]

        if param == "accept":
            async with bot.retrieve_data(
                call.from_user.id,
                call.message.chat.id
            ) as data:
                credentials = data.get("credentials")
                rate_id = data.get("rate_id")

            rate_info = get_rate_by_id(rate_id)
            if rate_info is None:
                await bot.edit_message_text(
                    "Тариф не найден или сессия истекла. Начните заново /pay",
                    call.message.chat.id,
                    call.message.id
                )
                await bot.delete_state(call.from_user.id, call.message.chat.id)
                return

            await bot.edit_message_text(
                "Заявка принята на рассмотрение. "
                "По её решению Вам придёт ответ.",
                call.message.chat.id,
                call.message.id
            )

            await bot.set_state(
                call.from_user.id,
                UserStates.pending_confirmation,
                call.message.chat.id
            )

            user_info = f"ID: {call.from_user.id}"
            if call.from_user.username:
                user_info = f"@{call.from_user.username}"

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

    @bot.message_handler(state=UserStates.pending_confirmation)
    async def pending_confirmation_handler(message: Message):
        await bot.send_message(
            message.chat.id,
            "Ваша заявка на подписку находится на рассмотрении."
            "Дождитесь решения."
        )

    @bot.callback_query_handler(
        func=lambda call: call.data.startswith('pay:admin_confirm')
    )
    async def cb_payment_handler(call: CallbackQuery):
        query = call.data.split(":")
        param = query[2]
        user_id = int(query[3])
        chat_id = int(query[4])

        await bot.answer_callback_query(call.id)
        current_state = await bot.get_state(user_id, chat_id)
        if current_state != UserStates.pending_confirmation.name:
            await bot.edit_message_text(
                "Заявка уже обработана.",
                call.message.chat.id,
                call.message.id
            )
            return

        if param == "accept":
            async with bot.retrieve_data(
                user_id, chat_id
            ) as data:
                rate_id: str = data.get("rate_id")
                credentials = data.get("credentials")

            # убрать у юзера состояние pending_for_confirmation

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
