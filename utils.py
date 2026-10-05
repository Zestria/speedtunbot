import time

from config import RATES


def calculate_expiry_time(old_expiry_time: int | None, days: int) -> int:
    current_time_ms = int(time.time() * 1000)
    if old_expiry_time is None:
        old_expiry_time = current_time_ms

    base_time = max(current_time_ms, old_expiry_time)

    duration_ms = days * 24 * 60 * 60 * 1000
    return base_time + duration_ms


def compose_rates_text(rates):
    text = "💳 <b>Доступные тарифы</b>\n\n"
    for i in range(1, len(rates)+1):
        text += f"{i}. <b>{rates[i-1]['name']}</b>"
        if i != len(rates):
            text += "\n"
    return text


def get_rate_by_id(rate_id: str | None):
    if rate_id is None or not rate_id.isdigit():
        return None

    index = int(rate_id) - 1
    if index < 0 or index >= len(RATES):
        return None

    return RATES[index]
