import asyncio
from telebot.async_telebot import AsyncTeleBot
from telebot.asyncio_storage import StateMemoryStorage

from py3xui import AsyncApi

from config import (
    DOMAIN,
    VPN_TOKEN,
    BOT_TOKEN,
)

api = AsyncApi(DOMAIN, token=VPN_TOKEN)
bot = AsyncTeleBot(BOT_TOKEN)
state_storage = StateMemoryStorage()
busy_users = set()
is_maintenance_lock = asyncio.Lock()
