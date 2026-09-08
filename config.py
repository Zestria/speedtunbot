import os
import json

from dotenv import load_dotenv

from telebot.async_telebot import AsyncTeleBot
from telebot.asyncio_storage import StateMemoryStorage
from telebot.asyncio_handler_backends import (
    State,
    StatesGroup
)

from py3xui import AsyncApi

from utils import load_banned_users


load_dotenv()

api = AsyncApi(os.getenv('DOMAIN'), token=os.getenv('VPN_TOKEN'))
bot = AsyncTeleBot(os.getenv('BOT_TOKEN'))
state_storage = StateMemoryStorage()
busy_users = set()

INBOUND_ID: int = int(os.getenv('INBOUND_ID'))
ADMIN_IDS = json.loads(os.getenv('ADMIN_IDS'))
IS_MAINTENANCE_MODE = False
RATES = [
    {"name": "30 дней - 150 рублей", "days": 30, "price": 150},
    {"name": "60 дней - 250 рублей", "days": 60, "price": 250},
    {"name": "90 дней - 350 рублей", "days": 90, "price": 350}
]
BANK_ACCOUNT_DETAILS: str = os.getenv("BANK_ACCOUNT_DETAILS")
BANNED_FILE: str = os.getenv("BANNED_USERS_FILE")
BANNED: set = load_banned_users()


class UserStates(StatesGroup):
    waiting_for_name = State()
    awaiting_payment = State()
    pending_confirmation = State()
    waiting_for_help = State()
    writing_to_user = State()
