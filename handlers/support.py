from telebot.async_telebot import AsyncTeleBot
from telebot.types import Message

from config import (
    ADMIN_IDS,
    UserStates
)


def register_support_handler(bot: AsyncTeleBot):

    @bot.message_handler(commands='support')
    async def support_handler(message: Message):
        current_state = await bot.get_state(
            message.from_user.id,
            message.chat.id
        )

        if current_state == UserStates.waiting_for_help.name:
            await bot.delete_state(
                message.from_user.id,
                message.chat.id
            )
            await bot.send_message(
                message.chat.id,
                "ℹ️ Вы вышли из режима поддержки.\n\n"
                "Напишите снова /support, если нужна помощь."
            )
            return

        await bot.set_state(
            message.from_user.id,
            UserStates.waiting_for_help,
            message.chat.id
        )
        await bot.send_message(
            message.chat.id,
            "💬 <b>Поддержка</b>\n\n"
            "Опишите вашу проблему — администратор ответит в ближайшее время.\n\n"
            "Для выхода из режима напишите /support ещё раз.",
            parse_mode="HTML"
        )

    @bot.message_handler(
        state=UserStates.waiting_for_help,
        content_types='text'
    )
    async def process_support_message(message: Message):
        user_info = f"ID {message.from_user.id}"
        if message.from_user.username:
            user_info = (
                f"@{message.from_user.username}\n"
                f"ID: {message.from_user.id}"
            )

        for admin_id in ADMIN_IDS:
            await bot.send_message(
                admin_id,
                f"📨 <b>Новое обращение в поддержку</b>\n\n"
                f"Пользователь: {user_info}\n\n"
                f"{message.text}",
                parse_mode="HTML"
            )

    @bot.message_handler(commands='support_user')
    async def support_user_handler(message: Message):
        if message.from_user.id not in ADMIN_IDS:
            return

        current_state = await bot.get_state(
            message.from_user.id,
            message.chat.id
        )

        parts = message.text.split()
        if current_state == UserStates.writing_to_user.name and len(parts) < 2:
            await bot.delete_state(
                message.from_user.id,
                message.chat.id
            )
            await bot.send_message(
                message.chat.id,
                "ℹ️ Режим общения с клиентом завершён."

            )
            return

        if len(parts) < 2 or not parts[1].isdigit():
            await bot.send_message(
                message.chat.id,
                "❌ Использование: /support_user <tg_id>"
            )
            return

        target_user_id = int(parts[1])

        await bot.set_state(
            message.from_user.id,
            UserStates.writing_to_user,
            message.chat.id
        )

        async with bot.retrieve_data(
            message.from_user.id,
            message.chat.id
        ) as data:
            data["target_user_id"] = target_user_id

        await bot.send_message(
            message.chat.id,
            f"✉️ Теперь сообщения будут отправляться пользователю {target_user_id}.\n\n"
            "Для выхода введите /support_user"
        )

    @bot.message_handler(
        state=UserStates.writing_to_user,
        content_types='text'
    )
    async def process_admin_writing(message: Message):
        async with bot.retrieve_data(
            message.from_user.id,
            message.chat.id
        ) as data:
            target_user_id = data.get("target_user_id")

        if not target_user_id:
            await bot.send_message(
                message.chat.id,
                "❌ Пользователь не найден."
            )
            await bot.delete_state(
                message.from_user.id, message.chat.id
            )
            return

        try:
            await bot.send_message(
                target_user_id,
                f"📩 <b>Ответ от поддержки</b>\n\n"
                f"{message.text}",
                parse_mode="HTML"
            )
        except Exception as e:
            await bot.send_message(
                message.chat.id,
                f"❌ Не удалось отправить сообщение: {e}"
            )
