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
            await bot.send_message(
                message.chat.id,
                "⚠️ У вас ещё нет аккаунта.\n\n"
                "Перейдите в /start для регистрации."
            )
            return

        select_rate_markup = quick_markup({
            str(i): {"callback_data": f"pay:select_rate:{i}"}
            for i in range(1, len(RATES)+1)
        }, row_width=3)

        await bot.send_message(
            message.chat.id,
            compose_rates_text(RATES),
            reply_markup=select_rate_markup,
            parse_mode="HTML"
        )

    @bot.callback_query_handler(
        func=lambda call: call.data.startswith('pay:select_rate')
    )
    async def cb_pay_select_rate_handler(call: CallbackQuery):
        param = call.data.split(":")[2]

        rate_info = get_rate_by_id(param)
        if rate_info is None:
            await bot.answer_callback_query(
                call.id,
                "❌ Такого тарифа не существует."
            )
            return

        await bot.answer_callback_query(call.id)

        await bot.set_state(
            call.from_user.id,
            UserStates.awaiting_payment,
            call.message.chat.id
        )

        async with bot.retrieve_data(
            call.from_user.id,
            call.message.chat.id
        ) as data:
            data["rate_id"] = param

        await bot.edit_message_text(
            f"💳 <b>Оплата тарифа</b>\n\n"
            f"Сумма к оплате: <b>{rate_info['price']} ₽</b>\n\n"
            f"Переведите средства на счёт:\n"
            f"<code>{BANK_ACCOUNT_DETAILS}</code>\n\n"
            "После оплаты нажмите ✅ Готово.\n"
            "Если передумали — ❌ Отмена.",
            call.message.chat.id, call.message.id,
            reply_markup=quick_markup({
                "✅ Готово": {"callback_data": "pay:user_confirm:accept"},
                "❌ Отмена": {"callback_data": "pay:user_confirm:decline"}
                }, row_width=2),
            parse_mode="HTML"
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
                rate_id = data.get("rate_id")

            rate_info = get_rate_by_id(rate_id)
            if rate_info is None:
                await bot.edit_message_text(
                    "❌ Тариф не найден или сессия истекла.\n"
                    "Начните заново /pay",
                    call.message.chat.id,
                    call.message.id
                )
                await bot.delete_state(call.from_user.id, call.message.chat.id)
                return

            await bot.edit_message_text(
                "⏳ <b>Заявка принята</b>\n\n"
                "Ваша заявка на рассмотрении.\n"
                "Ожидайте подтверждения от администратора.",
                call.message.chat.id,
                call.message.id,
                parse_mode="HTML"
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
                "✅ Одобрить": {
                    "callback_data": "pay:admin_confirm:accept:"
                    f"{call.from_user.id}:{call.message.chat.id}"
                },
                "❌ Отклонить": {
                    "callback_data": "pay:admin_confirm:decline:"
                    f"{call.from_user.id}:{call.message.chat.id}"
                }
            }, row_width=2)

            for admin_id in ADMIN_IDS:
                await bot.send_message(
                    admin_id,
                    f"📥 <b>Новая заявка на оплату</b>\n\n"
                    f"Пользователь: {user_info}\n"
                    f"Тариф: <b>{rate_info['name']}</b>\n"
                    f"Цена: <b>{rate_info['price']} ₽</b>",
                    reply_markup=give_sub_markup,
                    parse_mode="HTML"
                )
        elif param == "decline":
            await bot.edit_message_text(
                "❌ Оформление подписки отменено.",
                call.message.chat.id,
                call.message.id
            )

            await bot.delete_state(call.from_user.id, call.message.chat.id)

    @bot.message_handler(state=UserStates.pending_confirmation)
    async def pending_confirmation_handler(message: Message):
        await bot.send_message(
            message.chat.id,
            "⏳ Ваша заявка на рассмотрении.\n\n"
            "Пожалуйста, дождитесь подтверждения."
        )

    @bot.callback_query_handler(
        func=lambda call: call.data.startswith('pay:admin_confirm')
    )
    async def cb_payment_handler(call: CallbackQuery):
        await bot.answer_callback_query(call.id)

        query = call.data.split(":")
        param = query[2]
        if not query[3].isdigit() or not query[4].isdigit():
            await bot.edit_message_text(
                "❌ Ошибка: некорректные данные пользователя.",
                call.message.chat.id,
                call.message.id
            )
            return

        user_id = int(query[3])
        chat_id = int(query[4])

        current_state = await bot.get_state(user_id, chat_id)
        if current_state != UserStates.pending_confirmation.name:
            await bot.edit_message_text(
                "⚠️ Заявка уже обработана.",
                call.message.chat.id,
                call.message.id
            )
            return

        if param == "accept":
            async with bot.retrieve_data(
                user_id, chat_id
            ) as data:
                rate_id: str = data.get("rate_id")

            # убрать у юзера состояние awaiting_payment

            if not rate_id or int(rate_id) > len(RATES):
                await bot.send_message(
                    call.message.chat.id,
                    "❌ Тариф не найден или сессия истекла."
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
                        "✅ <b>Подписка активирована!</b>\n\n"
                        f"Тариф: <b>{rate_info['name']}</b>\n"
                        "Срок действия обновлён.\n"
                        "Проверить статус: /profile",
                        parse_mode="HTML"
                    )

                    await bot.edit_message_text(
                        f"✅ Подписка выдана\n\n"
                        f"Тариф: <b>{rate_info['name']}</b>\n"
                        f"Пользователь: {user_id}",
                        call.message.chat.id,
                        call.message.id,
                        parse_mode="HTML"
                    )

                    await bot.delete_state(user_id, chat_id)
            except Exception as e:
                await bot.send_message(
                    call.message.chat.id,
                    f"Ошибка при работе с 3xui: {e}"
                )
        elif param == "decline":
            await bot.edit_message_text(
                "❌ Заявка отклонена.",
                call.message.chat.id,
                call.message.id
            )
            await bot.send_message(
                chat_id,
                "❌ Ваша заявка была отклонена.\n"
                "По вопросам: /support"
            )
            await bot.delete_state(user_id, chat_id)
