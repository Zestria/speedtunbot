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

# --- /start (M0-09.1) ------------------------------------------------------

START_WELCOME = (
    "🎉 Добро пожаловать!\n\n"
    "Ваш аккаунт создан.\n"
    "Выберите тариф командой /pay или посмотрите профиль /profile."
)
START_RETURNING = (
    "👋 С возвращением!\n\n"
    "У вас уже есть аккаунт.\n"
    "Для просмотра подписки используйте /profile."
)

# --- /profile (M0-09.2) ----------------------------------------------------

PROFILE_TITLE = "📱 <b>Моя подписка</b>"
PROFILE_STATUS_LABEL = "Статус:"
PROFILE_STATUS_ACTIVE = "🟢 Активна"
PROFILE_STATUS_INACTIVE = "🔴 Неактивна"
PROFILE_EXPIRES_LABEL = "Действует до:"
PROFILE_UNLIMITED = "Бессрочно"
PROFILE_LINK_HEADING = "<b>Ссылка на подключение:</b>"
PROFILE_LINK_HINT = "Нажмите на ссылку или скопируйте её."
PROFILE_NO_ACCOUNT = "⚠️ У вас ещё нет аккаунта.\n\nПерейдите в /start для регистрации."

# --- /support, /support_user (M0-09.3) -------------------------------------

SUPPORT_ENTERED = (
    "💬 <b>Поддержка</b>\n\n"
    "Опишите вашу проблему — администратор ответит в ближайшее время.\n\n"
    "Для выхода из режима напишите /support ещё раз."
)
SUPPORT_EXITED = (
    "ℹ️ Вы вышли из режима поддержки.\n\nНапишите снова /support, если нужна помощь."
)
SUPPORT_CARD = "📨 <b>Новое обращение в поддержку</b>\n\nПользователь: {who}\n\n{text}"
SUPPORT_USER_ENTERED = (
    "✉️ Теперь сообщения будут отправляться пользователю {tg_id}.\n\n"
    "Для выхода введите /support_user"
)
SUPPORT_USER_EXITED = "ℹ️ Режим общения с клиентом завершён."
SUPPORT_USER_USAGE = "❌ Использование: /support_user &lt;tg_id&gt;"
SUPPORT_REPLY_CARD = "📩 <b>Ответ от поддержки</b>\n\n{text}"
SUPPORT_REPLY_FAILED = "❌ Не удалось отправить сообщение."

# --- /broadcast (M0-09.4) --------------------------------------------------

BROADCAST_USAGE = "❌ Использование: /broadcast &lt;текст&gt;"
BROADCAST_HEADER = "📢 <b>Объявление</b>"
BROADCAST_SUMMARY = (
    "✅ Рассылка завершена\n\nУспешно: <b>{sent}</b>\nНеудачно: <b>{failed}</b>"
)

# --- /ban, /unban, /banned_list (M0-09.5) ----------------------------------

BAN_USAGE = "❌ <b>Неверный формат</b>\nИспользуйте: <code>/ban &lt;tg_id&gt;</code>"
BAN_STAFF_REFUSED = "⚠️ Нельзя забанить администратора."
BAN_DONE = "✅ Пользователь <code>{tg_id}</code> забанен."
BAN_USER_NOTICE = (
    "🚫 <b>Вы забанены</b>\n\n"
    "Вы были заблокированы администратором и больше не можете пользоваться ботом."
)
UNBAN_USAGE = (
    "❌ <b>Неверный формат</b>\nИспользуйте: <code>/unban &lt;tg_id&gt;</code>"
)
UNBAN_NOT_BANNED = "ℹ️ Этот пользователь не забанен."
UNBAN_DONE = "✅ Пользователь <code>{tg_id}</code> разбанен."
UNBAN_EXPIRED = (
    "✅ Пользователь <code>{tg_id}</code> разбанен.\n\n"
    "ℹ️ Подписка истекла — доступ к VPN останется отключённым до оплаты."
)
UNBAN_USER_NOTICE = (
    "✅ <b>Вы разбанены</b>\n\nТеперь вы снова можете пользоваться ботом."
)
BANNED_LIST_EMPTY = "📭 Список забаненных пуст."
BANNED_LIST_HEADER = "<b>🚫 Забаненные пользователи:</b>"
PANEL_WARNING = "⚠️ Не удалось обновить панель: {error}"
PANEL_ALERT = "⚠️ Ошибка панели в {context}: {error}"

# --- /maintenance (M0-09.6) ------------------------------------------------

MAINTENANCE_ON = "Режим техобслуживания 🔴 <b>включён</b>"
MAINTENANCE_OFF = "Режим техобслуживания 🟢 <b>выключен</b>"
