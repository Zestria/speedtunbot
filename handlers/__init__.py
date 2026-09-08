from telebot.async_telebot import AsyncTeleBot

from handlers.start import register_start_handler
from handlers.profile import register_profile_handler
from handlers.payment import register_payment_handler
from handlers.support import register_support_handler


def register_all_handlers(bot: AsyncTeleBot):
    register_start_handler(bot)
    register_profile_handler(bot)
    register_payment_handler(bot)
    register_support_handler(bot)
