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
#: Body shown in place of a confirmation card after «❌ Отмена» (S1-5.1).
CONFIRM_CANCELLED = "❌ Действие отменено."
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
CALLBACK_CANCELLED = "Отменено"

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

# --- /profile dashboard (M0-09.2 / S1-1) -----------------------------------

PROFILE_TITLE = "👤 <b>Мой профиль</b>"
#: State line. Expiry is evaluated first: the panel disables a client the
#: moment its expiry passes, so a disabled client with a future expiry is a
#: suspension, not an expiry (S1-1.9).
PROFILE_STATE_ACTIVE = "🟢 <b>Подписка активна</b>"
PROFILE_STATE_EXPIRING = "🟡 <b>Подписка скоро заканчивается</b>"
PROFILE_STATE_EXPIRED = "🔴 <b>Подписка не активна</b>"
PROFILE_STATE_SUSPENDED = "🔴 <b>Подписка приостановлена</b>"
PROFILE_STATE_NOT_ACTIVATED = "🔴 <b>Подписка не активирована</b>"
#: Shown under the expired / never-activated states.
PROFILE_STATE_HINT = "Чтобы возобновить доступ, оплатите тариф."
PROFILE_EXPIRES_LABEL = "📅 До:"
#: ``expiry == 0`` with an enabled client — the panel's "no expiry" marker.
PROFILE_UNLIMITED = "∞"
PROFILE_TRAFFIC_LABEL = "📊 Трафик:"
PROFILE_ACTIVITY_LABEL = "🕒 Активность:"
PROFILE_ACTIVITY_TODAY = "сегодня в {time}"
PROFILE_ACTIVITY_YESTERDAY = "вчера в {time}"
PROFILE_ACTIVITY_OTHER = "{date} в {time}"
PROFILE_LINK_LABEL = "🔗"
PROFILE_NO_ACCOUNT = "⚠️ У вас ещё нет аккаунта.\n\nПерейдите в /start для регистрации."

# --- /profile keyboard (S1-1.10) -------------------------------------------

BUTTON_PROFILE_QR = "📱 QR-код"
BUTTON_PROFILE_INSTR = "📖 Инструкция"
BUTTON_PROFILE_EXTEND = "💳 Продлить"
BUTTON_PROFILE_NEWLINK = "🔄 Новая ссылка"
BUTTON_PROFILE_SUPPORT = "🆘 Поддержка"
BUTTON_PROFILE_LINK = "🔗 Показать ссылку"

# --- QR screen (S1-2.2) -----------------------------------------------------

#: Caption of the QR photo. ``{link}`` is replaced with the **escaped** URL —
#: see :data:`INSTRUCTIONS` for why this is a replace and not a ``.format``.
QR_CAPTION = (
    "📱 <b>Подключение по QR-коду</b>\n\n"
    "Отсканируйте в приложении:\n"
    "<code>{link}</code>"
)

# --- link + instruction screens (S1-3) --------------------------------------

PROFILE_LINK_SCREEN = (
    "🔗 <b>Ссылка на подключение</b>\n\n"
    "Скопируйте её в VPN-приложение:\n"
    "<code>{link}</code>"
)

INSTR_PICKER = "📖 <b>Инструкция</b>\n\nВыберите ваше устройство:"

#: Platform keys in picker order (§S1-3.1); :data:`INSTRUCTIONS`,
#: :data:`INSTR_PLATFORM_LABELS` and the picker buttons all share this order.
INSTRUCTION_PLATFORMS = ("android", "ios", "windows", "macos")

#: Button labels of the platform picker, keyed like :data:`INSTRUCTIONS`.
INSTR_PLATFORM_LABELS = {
    "android": "🤖 Android",
    "ios": "🍎 iOS",
    "windows": "🪟 Windows",
    "macos": "💻 macOS",
}

#: Numbered steps per platform (§S1-3.1): one cross-platform client (Hiddify)
#: plus one alternative, 4 steps each. ``{link}`` is substituted with
#: :meth:`str.replace` (never ``.format``) so the operator can retype the prose
#: freely — a stray ``{`` would otherwise raise ``KeyError`` at render time and
#: leave the button spinning.
INSTRUCTIONS = {
    "android": (
        "<b>🤖 Android</b>\n\n"
        "1. Установите <b>Hiddify</b> из Google Play.\n"
        "Альтернатива: <b>v2rayNG</b>.\n"
        "2. Скопируйте вашу ссылку:\n"
        "   <code>{link}</code>\n"
        "3. В Hiddify нажмите «+» → «Добавить из буфера обмена».\n"
        "4. Включите переключатель VPN и разрешите подключение."
    ),
    "ios": (
        "<b>🍎 iOS</b>\n\n"
        "1. Установите <b>Hiddify</b> из App Store.\n"
        "Альтернатива: <b>Streisand</b>.\n"
        "2. Скопируйте вашу ссылку:\n"
        "   <code>{link}</code>\n"
        "3. В Hiddify нажмите «+» → «Добавить из буфера обмена».\n"
        "4. Разрешите добавление конфигурации VPN и включите её."
    ),
    "windows": (
        "<b>🪟 Windows</b>\n\n"
        "1. Скачайте <b>Hiddify</b> с официального сайта.\n"
        "Альтернатива: <b>Nekoray</b>.\n"
        "2. Скопируйте вашу ссылку:\n"
        "   <code>{link}</code>\n"
        "3. В Hiddify нажмите «+» → «Добавить из буфера обмена».\n"
        "4. Выберите профиль и нажмите «Подключить»."
    ),
    "macos": (
        "<b>💻 macOS</b>\n\n"
        "1. Скачайте <b>Hiddify</b> с официального сайта.\n"
        "Альтернатива: <b>FoXray</b>.\n"
        "2. Скопируйте вашу ссылку:\n"
        "   <code>{link}</code>\n"
        "3. В Hiddify нажмите «+» → «Добавить из буфера обмена».\n"
        "4. Разрешите системное расширение VPN и включите профиль."
    ),
}

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

# --- Startup guard (bug-fix pass) -----------------------------------------

#: Raised at startup when ``users`` is empty but the panel still has customers.
LEGACY_IMPORT_REQUIRED = (
    "База данных пуста, но на панели найдено клиентов: {clients}.\n"
    "Похоже, бот запускается поверх развёртывания старого бота: рассылка, "
    "/ban и /pay будут работать некорректно.\n"
    "Выполните `python -m app.cli import-legacy` (или включите "
    "AUTO_IMPORT_LEGACY=true) и запустите сервис снова."
)

# --- /pay (M0-10) ----------------------------------------------------------

PAYMENT_NO_ACCOUNT = "⚠️ У вас ещё нет аккаунта.\n\nПерейдите в /start для регистрации."
PAYMENT_NO_CLIENT = (
    "⚠️ Ваш аккаунт ещё не подключён к серверу.\n\n"
    "Запустите /start, чтобы создать подключение."
)
PAYMENT_NOT_APPROVED = "⚠️ Ваш аккаунт ещё не подтверждён. Обратитесь в /support."
PAYMENT_NO_TARIFFS = "⚠️ Тарифы временно недоступны. Попробуйте позже."
PAYMENT_CHOOSE_TARIFF = "💳 <b>Выберите тариф</b>"
PAYMENT_TARIFF_BUTTON = "{days} дней — {price} ₽"
PAYMENT_INSTRUCTIONS = (
    "💳 <b>Оплата тарифа</b>\n\n"
    "Тариф: <b>{name}</b>\n"
    "К оплате: <b>{price} ₽</b>\n\n"
    "Переведите сумму на счёт:\n"
    "<code>{bank_details}</code>\n\n"
    "После перевода нажмите <b>✅ Готово</b>.\n"
    "Если передумали — <b>❌ Отмена</b>."
)
PAYMENT_NO_BANK_DETAILS = "⚠️ Реквизиты для оплаты не настроены. Обратитесь в /support."
PAYMENT_ALREADY_SUBMITTED = (
    "⏳ Ваша заявка на рассмотрении.\n\nПожалуйста, дождитесь подтверждения."
)
PAYMENT_AWAITING_PROOF = (
    "✅ Заявка отправлена на проверку.\n\n"
    "Если хотите, пришлите фото или файл чека — он попадёт к администратору.\n\n"
    "Отменить заявку можно кнопкой ниже."
)
PAYMENT_RECEIPT_SAVED = "🧾 Чек прикреплён к заявке #{id}. Спасибо!"
PAYMENT_MEDIA_UNROUTED = (
    "❓ Не понял вложение.\n\n"
    "Чек можно приложить после /pay, а вопросы задать через /support."
)
PAYMENT_USER_EXPIRED = (
    "⌛ Ваша заявка была отменена автоматически: истёк срок рассмотрения.\n\n"
    "Начать заново: /pay"
)
PAYMENT_CANCELLED_BY_USER = "❌ Оформление подписки отменено.\n\nНачать заново: /pay"
PAYMENT_NOT_FOUND = "❌ Заявка не найдена. Начните заново /pay"
PAYMENT_UNKNOWN_TARIFF = "❌ Тариф не найден. Начните заново /pay"
PAYMENT_ALREADY_PROCESSED = "⏳ Заявка уже обработана."
PAYMENT_UNAVAILABLE = "⚠️ Сервис временно недоступен. Попробуйте позже."
PAYMENT_APPROVED_BY_ADMIN = "✅ Заявка одобрена."
PAYMENT_DECLINED_BY_ADMIN = "❌ Заявка отклонена."
PAYMENT_RETRY_OK = "✅ Применение повторено."
PAYMENT_RETRY_NOOP = "ℹ️ Заявка уже применена."

#: Review card fanned out to ``payments.review`` staff (§M0-10.3).
PAYMENT_CARD = (
    "📥 <b>Новая заявка на оплату</b> #{id}\n\n"
    "Пользователь: {who}\n"
    "ID: <code>{tg_id}</code>\n"
    "Тариф: <b>{name}</b>\n"
    "Цена: <b>{price} ₽</b>\n"
    "Срок: {days} дн.\n"
    "Текущий срок действия: {expiry}"
)
PAYMENT_CARD_UNLIMITED_WARNING = (
    "⚠️ У клиента бессрочная подписка — срок действия изменён не будет."
)
PAYMENT_CARD_APPROVED = "✅ Одобрено {actor}\n\nДобавлено {days} дн."
PAYMENT_CARD_APPLY_FAILED = (
    "⚠️ Одобрено {actor}, но панель не ответила.\nНажмите «Повторить применение»."
)
PAYMENT_CARD_DECLINED = "❌ Отклонено {actor}"
PAYMENT_CARD_CANCELLED = "🚫 Заявка отменена пользователем"
PAYMENT_CARD_EXPIRED = "⌛ Заявка истекла (не рассмотрена вовремя)"
PAYMENT_RECEIPT_CARD = "🧾 Чек к заявке #{id}"
PAYMENT_CARD_SYSTEM = "системой"
PAYMENT_NO_USERNAME = "нет @username"
PAYMENT_EXPIRY_UNLIMITED = "бессрочно"
#: A disabled ``0`` expiry (never activated) shown on the review card (S0-1.5).
PAYMENT_EXPIRY_NOT_ACTIVATED = "не активирован"
PAYMENT_USER_APPROVED = (
    "✅ <b>Оплата подтверждена!</b>\n\n"
    "Тариф: <b>{name}</b>\n"
    "Подписка до {expiry}.\n\n"
    "Проверить статус можно кнопкой ниже."
)
#: The perpetual-client variant of :data:`PAYMENT_USER_APPROVED` (S1-5.6):
#: ``expiry_after_ms`` was ``None``/``0``, so there is no «до …» date to show.
PAYMENT_USER_APPROVED_UNLIMITED = (
    "✅ <b>Оплата подтверждена!</b>\n\n"
    "Тариф: <b>{name}</b>\n"
    "Подписка бессрочная.\n\n"
    "Проверить статус можно кнопкой ниже."
)
PAYMENT_USER_DECLINED = "❌ Ваша заявка была отклонена.\n\nПо вопросам: /support"
PAYMENT_RETRY_ALERT = (
    "⚠️ Платёж #{id} одобрен, но не применён к панели.\n"
    "Пользователь: <code>{tg_id}</code>, тариф: <b>{name}</b>"
)

# --- payment buttons --------------------------------------------------------

BUTTON_PAY_DONE = "✅ Готово"
BUTTON_PAY_CANCEL = "❌ Отмена"
BUTTON_APPROVE = "✅ Одобрить"
BUTTON_DECLINE = "❌ Отклонить"
BUTTON_RETRY_APPLY = "🔁 Повторить применение"

# --- main menu + /help (S1-4) -----------------------------------------------

#: Body appended to every ``/start`` greeting (§S1-4.5): the menu itself is the
#: keyboard, so this is only the line that tells the user it is there.
START_MENU = "🏠 <b>Главное меню</b>\n\nВыберите действие:"

HELP_TEXT = (
    "ℹ️ <b>Помощь</b>\n\n"
    "Доступные команды:\n"
    "/start — главное меню\n"
    "/profile — подписка, трафик и ссылка\n"
    "/pay — оплата тарифов\n"
    "/support — написать в поддержку"
)

BUTTON_MENU_PROFILE = "👤 Профиль"
BUTTON_MENU_PAY = "💳 Оплата"
#: The menu and the profile card label these two identically, so they share one
#: constant each instead of two literals that could drift apart.
BUTTON_MENU_INSTR = BUTTON_PROFILE_INSTR
BUTTON_MENU_SUPPORT = BUTTON_PROFILE_SUPPORT
#: Staff-only entry to the admin dashboard, appended on /start and /help
#: when the viewer holds ``users.view`` (§S2-1.11).
BUTTON_MENU_ADMIN = "🛠 Админ-панель"

# --- link regeneration + post-payment polish (S1-5) -------------------------

#: Confirmation card shown before «🔄 Новая ссылка» regenerates the sub id.
NEWLINK_CONFIRM = (
    "🔄 <b>Новая ссылка</b>\n\n"
    "⚠️ Старая ссылка перестанет работать — нужно будет заново добавить "
    "подписку в приложение.\n\n"
    "Продолжить?"
)
#: Toast when a regeneration is requested before :data:`REGEN_COOLDOWN` passed.
NEWLINK_RATE_LIMITED = "⌛ Новую ссылку можно создать раз в 10 минут."
#: Shown in place of the card when the panel refused to regenerate; the old link
#: is untouched, so the user is told so explicitly (§S1-5.3).
NEWLINK_FAILED = (
    "⚠️ Не удалось обновить ссылку. Старая ссылка продолжает работать.\n\n"
    "Попробуйте позже или напишите в /support."
)

#: Post-payment buttons (§S1-5.6) — aliases, so the menu, the dashboard and the
#: payment notification can never name the same destination differently.
BUTTON_GO_PROFILE = BUTTON_MENU_PROFILE
BUTTON_GO_SUPPORT = BUTTON_MENU_SUPPORT
