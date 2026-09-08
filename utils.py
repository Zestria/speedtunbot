import os
import json
import time

from config import RATES


def load_banned_users(filepath: str) -> set:
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return set(json.load(f))

        except Exception:
            return set()

    return set()


def save_banned_users(banned: set, filepath: str) -> None:
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(list(banned), f)


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


def get_rate_by_id(rate_id: str | None):
    if rate_id is None or not rate_id.isdigit():
        return None

    index = int(rate_id) - 1
    if index < 0 or index >= len(RATES):
        return None

    return RATES[index]
