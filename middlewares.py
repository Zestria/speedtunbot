from telebot.types import Message

from telebot.asyncio_handler_backends import (
    BaseMiddleware,
    CancelUpdate
)

from config import (
    ADMIN_IDS,
    IS_MAINTENANCE_MODE,
    busy_users,
    bot
)


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
