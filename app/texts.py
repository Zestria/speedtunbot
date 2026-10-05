"""Russian UI string catalog (``TASK_PLAN.md`` §M0-07.9).

A single place for user-facing Russian text so ported handlers carry no UI
literals and wording can be reviewed in one spot. This is a **skeleton**: the
strings needed by M0-07's helpers are here; handler-specific copy is added as
those handlers are ported (M0-09+).

Only plain constants — no formatting logic — so the module is importable with
no side effects.
"""

from __future__ import annotations

# --- generic errors --------------------------------------------------------

ERROR_GENERIC = "⚠️ Что-то пошло не так. Попробуйте позже."
ERROR_PANEL = "⚠️ Сервис временно недоступен. Попробуйте позже."
ERROR_ACCESS_DENIED = "⛔ Недостаточно прав."
ERROR_STALE_BUTTON = "⌛ Кнопка устарела."
ERROR_CONFIRM_EXPIRED = "⌛ Подтверждение устарело."
ERROR_WAIT = "⏳ Подождите…"
ERROR_MAINTENANCE = "🛠 Идут технические работы. Попробуйте позже."
ERROR_UNKNOWN_USER = "❓ Пользователь не найден."

# --- welcome / profile -----------------------------------------------------

WELCOME = (
    "👋 <b>Добро пожаловать!</b>\n\n"
    "Это бот для получения доступа к VPN.\n"
    "Отправьте /profile, чтобы посмотреть свой профиль."
)

# --- buttons ---------------------------------------------------------------

BUTTON_CONFIRM = "✅ Подтвердить"
BUTTON_CANCEL = "❌ Отмена"
BUTTON_BACK = "⬅️ Назад"
BUTTON_NEXT = "Вперёд ➡️"

# --- callback answers ------------------------------------------------------

CALLBACK_DONE = "Готово"
CALLBACK_ALREADY_DONE = "Уже обработано"
