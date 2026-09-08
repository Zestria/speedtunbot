from telebot.async_telebot import AsyncTeleBot
from telebot.asyncio_storage import StateMemoryStorage

from py3xui import AsyncApi

from config import (
    DOMAIN,
    VPN_TOKEN,
    BOT_TOKEN,
    BANNED_FILE
)
from utils import load_banned_users

api = AsyncApi(DOMAIN, token=VPN_TOKEN)
bot = AsyncTeleBot(BOT_TOKEN)
state_storage = StateMemoryStorage()
busy_users = set()
banned: set = load_banned_users(BANNED_FILE)
