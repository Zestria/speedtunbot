import os
import json

from dotenv import load_dotenv

from telebot.asyncio_handler_backends import (
    State,
    StatesGroup
)

load_dotenv()

DOMAIN = os.getenv('DOMAIN')
VPN_TOKEN = os.getenv('VPN_TOKEN')
BOT_TOKEN = os.getenv('BOT_TOKEN')

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


class UserStates(StatesGroup):
    waiting_for_name = State()
    awaiting_payment = State()
    pending_confirmation = State()
    waiting_for_help = State()
    writing_to_user = State()
