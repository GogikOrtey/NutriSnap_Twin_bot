"""
main.py — точка входа Telegram-бота NutriSnap (@nutrisnap_ultra_bot).

Зачем нужен файл
----------------
Запуск бота (long polling), инфраструктура (Bot, Dispatcher, MemoryStorage),
главное меню (дневник, распознать, настройки, напоминания, выгрузка) и /start.
Распознавание еды — в food_recognition.py (отдельный Router).
Первичный опрос — в initial_survey.py; /start смотрит NocoDB users.
Обратная связь (баг / идея) — SMTP-письмо на FEEDBACK_TO_EMAIL.
Консольные ошибки → error_notify (письмо с префиксом 🟨⬛🍎).
FAQ в настройках — обзор возможностей и ответы по темам (распознавание, дневник, …).

Как устроен файл
----------------
1. Импорты, .env, константы кнопок, MenuFlow.
2. Хранилище NocoDB (db_nocodb): users (кэш по telegram_id + singleflight) /
   food_logs / reminders.
3. SMTP обратной связи (send_feedback_email) + форматтеры / Reply-клавиатуры.
4. UI-хелперы: Reply → новое сообщение; Inline дневника → edit; чистка «Выберите действие:».
5. Router меню + хендлеры; /start → опрос или меню по БД; on_food_saved → food_logs + reminders.
6. UserNotRegisteredError → error-handler → та же ветка, что /start;
   сбои БД/Gemini/сети → TECH_ISSUES_USER_TEXT + письмо 🟧🍎;
   прочие необработанные update-ошибки → report_console_error (🟨⬛🍎).
7. main() — хуки error_notify + polling + фоновый usage-reminder (13:00);
   Telegram-сессия через прокси при TELEGRAM_PROXY / OUTBOUND_HTTPS_PROXY
   (VPS mihomo), иначе напрямую.
8. DropStaleMessagesMiddleware — сообщения старше 10 мин (очередь после
   даунтайма) не обрабатываются; чату один раз пишется, что бот снова онлайн.
"""

from __future__ import annotations

import asyncio
import html
import io
import os
import random
import smtplib
import threading
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import CommandStart, ExceptionTypeFilter, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    TelegramObject,
)
from dotenv import load_dotenv

import db_nocodb as db
from error_notify import (
    TECH_ISSUES_USER_TEXT,
    attach_asyncio_error_handler,
    install_error_email_hooks,
    is_external_service_error,
    notify_user_tech_issues,
    notify_user_tech_issues_from_event,
    report_console_error,
    report_error_auto,
    report_service_problem,
)
from food_recognition import setup_food_recognition
from initial_survey import setup_initial_survey, start_initial_survey
from proxy_config import get_telegram_proxy

#region Конфиг и тексты кнопок
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# SMTP для обратной связи (Yandex и др.). Письма → FEEDBACK_TO_EMAIL.
FEEDBACK_TO_EMAIL = os.getenv("FEEDBACK_TO_EMAIL", "gog.ortey@yandex.ru")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.yandex.ru")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "gog.ortey@yandex.ru")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")

# Если True — /start всегда кидает на первичный опрос (для отладки UI).
# Если False — маршрутизация по наличию записи в users (NocoDB).
INITIAL_SURVEY_ENABLED = False

# Сообщения старше этого возраста (после даунтайма бота) не обрабатываем.
STALE_MESSAGE_MAX_AGE_SEC = 10 * 60
# Антиспам: при пачке старых апдейтов из одного чата — одно уведомление.
STALE_RECOVERY_NOTIFY_COOLDOWN_SEC = 60
# Текст «снова онлайн» (без точки в конце — стиль бота).
STALE_RECOVERY_TEXT = (
    "Я был ненадолго офлайн, но уже снова на связи ⭐\n"
    "Если отправляли что-то пока меня не было — пришлите ещё раз"
)

# Кнопка возврата в корень — есть в основных разделах.
BTN_MAIN_MENU = "🏠 Главное меню"

BTN_DIARY = "📒 Дневник питания"
BTN_RECOGNIZE = "🔍 Распознать"
BTN_SETTINGS = "⚙️ Настройки"

BTN_ADD_DISH = "🟩 Добавить блюдо"
BTN_EDIT_DISH = "✏️ Изменить блюдо"
BTN_DELETE_DISH = "🗑 Удалить блюдо"
# Устарело: раньше вело к выгрузке из дневника; оставлено в MENU_BUTTON_TEXTS
# на случай старой reply-клавиатуры у клиента (не слать в Gemini).
BTN_EXTRA = "📎 Дополнительно"
BTN_BACK = "⬅️ Назад"
# Пагинация номеров блюд в флоу изменить/удалить (по 10 на страницу).
BTN_PICK_PAGE_NEXT = "▶️ Далее"
BTN_PICK_PAGE_PREV = "◀️ Ранее"
PICK_PAGE_SIZE = 10

BTN_EXPORT_TODAY = "📅 Текущий день"
BTN_EXPORT_YESTERDAY = "📆 Прошлый день"
BTN_EXPORT_WEEK = "🗓 Прошедшую неделю"
BTN_EXPORT_MONTH = "🗂 Месяц (30 дней)"

BTN_MONTH_0_30 = "1️⃣ Последние 30 дней"
BTN_MONTH_30_60 = "2️⃣ От 30 до 60 дней назад"
BTN_MONTH_60_90 = "3️⃣ От 60 до 90 дней назад"

BTN_SET_DAY_HOUR = "🕓 Время смены суток"
BTN_SET_REMINDERS = "🔔 Напоминания и Витамины"
BTN_SET_EXPORT = "📤 Сделать выгрузку журнала"
BTN_SET_HELP = "❓ Инструкции к боту в вопросах и ответах"
BTN_HELP_OVERVIEW = "📋 Обзор возможностей"
BTN_HELP_RECOGNIZE = "🍽 Как работает распознавание"
BTN_HELP_DIARY = "📒 Как устроен дневник"
BTN_HELP_REMINDERS = "🔔 Как работают напоминания"
BTN_HELP_EXPORT = "📤 Как сделать выгрузку"
BTN_HELP_DAY_HOUR = "🕓 Что такое смена суток"
BTN_SET_FEEDBACK = "💬 Отправить отзыв или сообщить об ошибке"
BTN_FEEDBACK_BUG = "🐞 Сообщить об ошибке"
BTN_FEEDBACK_IDEA = "💡 Предложение по улучшению функционала"
BTN_SET_PROFILE = "👤 Изменить данные профиля"
BTN_UPDATE_PROFILE = "🔄 Обновить данные пользователя"
BTN_SET_GOAL = "🎯 Изменить тип отслеживания"
BTN_SET_CALORIES = "🔥 Целевые ккал в сутки"
BTN_CONFIRM_UPDATE_YES = "✅ Да, запустить опрос"
BTN_CONFIRM_UPDATE_NO = "❌ Нет, отмена"
BTN_CONFIRM_RECALC_YES = "✅ Да, пересчитать ккал"
BTN_CONFIRM_RECALC_NO = "❌ Нет, оставить как есть"

BTN_REM_ADD = "➕ Добавить напоминание"
BTN_REM_LIST = "📋 Мои напоминания"
BTN_REM_USAGE = "📲 Напоминание использования бота"
BTN_REM_WINDOW_BREAKFAST = "🌅 Завтрак 07:00–11:00"
BTN_REM_WINDOW_LUNCH = "☀️ Обед 12:00–16:00"
BTN_REM_WINDOW_DINNER = "🌙 Ужин 17:00–22:00"
BTN_REM_ANY_FOOD = "🍽 Любая еда"
BTN_REM_HEARTY = "🍲 Только сытный приём (>250 ккал)"
BTN_REM_TOGGLE = "⏯ Вкл / Выкл"
BTN_REM_DELETE = "🗑 Удалить"
BTN_REM_DELETE_YES = "✅ Да, удалить"
BTN_REM_DELETE_NO = "❌ Нет, оставить"

BTN_GOAL_LOSS = "📉 Похудение"
BTN_GOAL_GAIN = "📈 Набор веса"
BTN_GOAL_MAINTAIN = "⚖️ Просто отслеживание"

CALLBACK_DIARY_PREV = "diary:prev"
CALLBACK_DIARY_NEXT = "diary:next"
CALLBACK_REM_SNOOZE_PREFIX = "rem:snooze:"
CALLBACK_REM_OK_PREFIX = "rem:ok:"
# Inline под финалом опроса: подтвердить текст про usage-reminder.
CALLBACK_SURVEY_USAGE_OK = "survey:usage_ok"

# Напоминание «нет еды до 13:00» (локальное время пользователя).
USAGE_REMINDER_HOUR = 13
USAGE_REMINDER_CHECK_INTERVAL_SEC = 60
SURVEY_USAGE_REMINDER_TEXT = (
    "Мы добавили напоминание об использовании бота 🙂\n"
    "\n"
    "В первые дни легко забыть заглянуть сюда — поэтому если до 13:00 "
    "не будет ни одной фиксации еды, бот мягко напомнит. "
    "Работает каждый день; отключить можно в "
    "Настройки → Напоминания → «Напоминание использования бота»"
)
USAGE_REMINDER_EMOJIS = (
    "🍭", "🍬", "🍫", "🥧", "🥘", "🍳", "☕️", "🍖", "🍛", "🍲", "🥪", "🧀", "🍱",
)


# Собирает текст usage-reminder со случайным food-emoji и spoiler-строкой «Отключить…».
# Используется в usage_reminder_loop при send_message (parse_mode=HTML).
def get_usage_reminder_notify_text() -> str:
    emoji = random.choice(USAGE_REMINDER_EMOJIS)
    return (
        f"Эй! Сегодня ещё нет ни одной записи о еде {emoji}\n"
        "Если уже поели — загляните и зафиксируйте, это займёт минуту\n"
        "\n"
        "<tg-spoiler>Отключить: Настройки → Напоминания → «Напоминание использования бота»</tg-spoiler>"
    )

# Памятка экрана «Распознать» — также открывается после завершения первичного опроса.
RECOGNIZE_HINT_TEXT = (
    "💡 Отправлять фото или текст можно в любой момент — кнопка не обязательна\n"
    "\n"
    "✨ Что умеет бот:\n"
    "⠀⠀⠀📸 Оценить блюдо по фото (можно с подписью)\n"
    "⠀⠀⠀📝 Разобрать текстовое описание / ккал\n"
    "⠀⠀⠀🏷️ Прочитать этикетку с пищевой ценностью\n"
    "\n"
    "📋 После оценки появится превью — подтвердите или поправьте результат\n"
    "\n"
    "🚀 Можешь начинать распознавание прямо сейчас — отправь в чат фото или текст описания еды:"
)

# Обзор FAQ (~2/3 приветствия из initial_survey.WELCOME_TEXT, без блока «С чего начать»).
# Используется экраном «Инструкции к боту» в настройках.
HELP_OVERVIEW_TEXT = (
    "Инструкции к боту:\n"
    "\n"
    "👋 NutriClick — твой помощник по учёту калорий\n"
    "\n"
    "Помогает легко следить за тем, что ты ешь за день: сколько уже съедено, "
    "сколько осталось до цели и из чего складывается рацион.\n"
    "\n"
    "<b>✨ Что умеет</b>\n"
    "\n"
    "<b>🍽 Быстрый учёт еды</b>\n"
    "Пришли фото блюда или просто напиши, что съел — я распознаю еду "
    "и посчитаю калории и БЖУ.\n"
    "\n"
    "<b>📒 Дневник питания</b>\n"
    "Все приёмы пищи за день — в одном удобном месте. Можно смотреть прогресс, "
    "менять и удалять записи.\n"
    "\n"
    "<b>🔔 Напоминания</b>\n"
    "Напомню позавтракать, пообедать или принять витамины — "
    "чтобы ничего важного не выпало из дня.\n"
    "\n"
    "<b>📊 Прогресс за день</b>\n"
    "Сразу видно: сколько калорий съедено, сколько осталось до цели "
    "и баланс белков, жиров и углеводов.\n"
    "\n"
    "Выберите тему ниже, чтобы узнать подробнее"
)

# Подробные ответы FAQ по темам (кнопки в kb_help).
# Используется хендлерами BTN_HELP_* в настройках.
HELP_TOPIC_TEXTS: dict[str, str] = {
    BTN_HELP_RECOGNIZE: (
        "🍽 Как работает распознавание\n"
        "\n"
        "Учитывать еду можно в любой момент — кнопка «🔍 Распознать» "
        "не обязательна. Просто отправьте сообщение в чат.\n"
        "\n"
        "<b>Что можно прислать</b>\n"
        "⠀⠀⠀📸 фото блюда\n"
        "⠀⠀⠀📸+📝 фото с подписью (что это и/или примерный вес)\n"
        "⠀⠀⠀🏷️ фото этикетки с ккал и БЖУ\n"
        "⠀⠀⠀📝 только текст: «борщ 300 г», «яйцо и тост» и т.п.\n"
        "\n"
        "<b>После оценки</b>\n"
        "Появится превью с ккал, БЖУ и порцией. Можно подтвердить ✅ "
        "или нажать ✏️ и:\n"
        "⠀⠀⠀• изменить вес порции (КБЖУ пересчитаются)\n"
        "⠀⠀⠀• дополнить описание\n"
        "⠀⠀⠀• заменить фото/текст\n"
        "⠀⠀⠀• не сохранять результат\n"
        "\n"
        "Если ничего не нажать — через несколько секунд результат "
        "подтвердится сам.\n"
        "\n"
        "Уже сохранённые записи потом правятся в «📒 Дневник питания» "
        "(изменить или удалить блюдо)"
    ),
    BTN_HELP_DIARY: (
        "📒 Дневник питания\n"
        "\n"
        "Здесь собраны все приёмы пищи за выбранный день: время, название, "
        "ккал и БЖУ.\n"
        "\n"
        "<b>Что можно делать</b>\n"
        "⠀⠀⠀◀️▶️ листать вчера / другие дни\n"
        "⠀⠀⠀🟩 добавить блюдо — откроется экран распознавания\n"
        "⠀⠀⠀✏️ изменить блюдо — выбрать запись по номеру\n"
        "⠀⠀⠀🗑 удалить блюдо — убрать ошибочную запись\n"
        "\n"
        "На главном экране видно прогресс дня: съедено / цель, остаток "
        "или перебор и сумма БЖУ. Дневник — место, где этот список "
        "удобно править"
    ),
    BTN_HELP_REMINDERS: (
        "🔔 Напоминания и витамины\n"
        "\n"
        "Нужны, чтобы не забыть приём витаминов, добавок или важный "
        "приём пищи.\n"
        "\n"
        "<b>Как настроить</b>\n"
        "Настройки → «🔔 Напоминания и Витамины» → добавить:\n"
        "⠀⠀⠀1. название (например, «Омега-3» или «Витамин D»)\n"
        "⠀⠀⠀2. окно времени: завтрак / обед / ужин\n"
        "⠀⠀⠀3. на что реагировать: любая еда или только сытный "
        "приём (&gt;250 ккал)\n"
        "\n"
        "<b>Как срабатывает</b>\n"
        "Когда вы сохраняете еду внутри выбранного окна и она подходит "
        "по калориям — бот напишет «🔔 Нужно: …». Можно ответить "
        "«✅ Понятно» или отложить «⏰ На следующую еду».\n"
        "\n"
        "В списке напоминания можно включать, выключать и удалять. "
        "Если ботом долго не пользоваться — уведомления временно "
        "замирают, чтобы не мешать.\n"
        "\n"
        "Отдельно есть «📲 Напоминание использования бота»: если до "
        "13:00 не было ни одной фиксации еды — бот мягко напомнит. "
        "Его можно выключить в том же разделе"
    ),
    BTN_HELP_EXPORT: (
        "📤 Выгрузка журнала\n"
        "\n"
        "Файл .txt со всеми записями дневника за выбранный период: "
        "блюда, ккал, БЖУ, дата и время.\n"
        "\n"
        "<b>Где взять</b>\n"
        "Настройки → «📤 Сделать выгрузку журнала».\n"
        "\n"
        "<b>Периоды</b>\n"
        "⠀⠀⠀📅 текущий день\n"
        "⠀⠀⠀📆 вчера\n"
        "⠀⠀⠀🗓 прошедшая неделя\n"
        "⠀⠀⠀🗂 окна по 30 дней (последние 30 / 30–60 / 60–90)\n"
        "\n"
        "<b>Зачем</b>\n"
        "Сохранить историю для себя или вставить файл в ChatGPT / "
        "другую нейросеть. В начало уже добавлены ваши характеристики "
        "и короткий промпт — так проще получить разбор рациона"
    ),
    BTN_HELP_DAY_HOUR: (
        "🕓 Смена суток\n"
        "\n"
        "В боте новый день начинается не в полночь, а в выбранный час "
        "(по умолчанию 04:00).\n"
        "\n"
        "Всё, что вы съели до этого часа, относится к предыдущему дню — "
        "удобно, если ужинаете или перекусываете после полуночи.\n"
        "\n"
        "Час и часовой пояс влияют на дневник, прогресс «сегодня» "
        "и окна напоминаний.\n"
        "\n"
        "Поменять час: Настройки → «🕓 Время смены суток». "
        "Часовой пояс задаётся при опросе (и позже — в данных профиля)"
    ),
}

# Шаблоны окон напоминаний (time_start, time_end) — каждый день, без выбора дней недели.
REMINDER_WINDOWS: dict[str, tuple[str, str]] = {
    BTN_REM_WINDOW_BREAKFAST: ("07:00", "11:00"),
    BTN_REM_WINDOW_LUNCH: ("12:00", "16:00"),
    BTN_REM_WINDOW_DINNER: ("17:00", "22:00"),
}
# Порог «сытного» приёма для min_calories в reminders.
REMINDER_HEARTY_MIN_KCAL = 250
# Заморозка уведомлений, если пользователь не заходил N дней (users.last_active_at).
REMINDER_FREEZE_AFTER_DAYS = 3

GOAL_LABELS = {
    "weight_loss": "Похудение",
    "muscle_gain": "Набор веса",
    "maintain": "Просто отслеживание",
}
GOAL_BY_BTN = {
    BTN_GOAL_LOSS: "weight_loss",
    BTN_GOAL_GAIN: "muscle_gain",
    BTN_GOAL_MAINTAIN: "maintain",
}
# Подписи коэффициента активности (users.activity_level) для сводки профиля.
ACTIVITY_LABELS = {
    1.2: "Сидячий образ жизни",
    1.375: "Лёгкая активность",
    1.55: "Умеренная активность",
    1.725: "Высокая активность",
    1.9: "Очень высокая активность",
}

# Все тексты Reply-кнопок меню — food_recognition не должен слать их в Gemini.
MENU_BUTTON_TEXTS: frozenset[str] = frozenset(
    {
        BTN_MAIN_MENU,
        BTN_DIARY,
        BTN_RECOGNIZE,
        BTN_SETTINGS,
        BTN_ADD_DISH,
        BTN_EDIT_DISH,
        BTN_DELETE_DISH,
        BTN_EXTRA,
        BTN_BACK,
        BTN_PICK_PAGE_NEXT,
        BTN_PICK_PAGE_PREV,
        BTN_EXPORT_TODAY,
        BTN_EXPORT_YESTERDAY,
        BTN_EXPORT_WEEK,
        BTN_EXPORT_MONTH,
        BTN_MONTH_0_30,
        BTN_MONTH_30_60,
        BTN_MONTH_60_90,
        BTN_SET_DAY_HOUR,
        BTN_SET_REMINDERS,
        BTN_SET_EXPORT,
        BTN_SET_HELP,
        BTN_HELP_OVERVIEW,
        BTN_HELP_RECOGNIZE,
        BTN_HELP_DIARY,
        BTN_HELP_REMINDERS,
        BTN_HELP_EXPORT,
        BTN_HELP_DAY_HOUR,
        BTN_SET_FEEDBACK,
        BTN_FEEDBACK_BUG,
        BTN_FEEDBACK_IDEA,
        BTN_SET_PROFILE,
        BTN_UPDATE_PROFILE,
        BTN_SET_GOAL,
        BTN_SET_CALORIES,
        BTN_CONFIRM_UPDATE_YES,
        BTN_CONFIRM_UPDATE_NO,
        BTN_CONFIRM_RECALC_YES,
        BTN_CONFIRM_RECALC_NO,
        BTN_REM_ADD,
        BTN_REM_LIST,
        BTN_REM_USAGE,
        BTN_REM_WINDOW_BREAKFAST,
        BTN_REM_WINDOW_LUNCH,
        BTN_REM_WINDOW_DINNER,
        BTN_REM_ANY_FOOD,
        BTN_REM_HEARTY,
        BTN_REM_TOGGLE,
        BTN_REM_DELETE,
        BTN_REM_DELETE_YES,
        BTN_REM_DELETE_NO,
        BTN_GOAL_LOSS,
        BTN_GOAL_GAIN,
        BTN_GOAL_MAINTAIN,
    }
)
#endregion

#region FSM меню
# Состояния ввода в меню (выбор блюда, настройки, отзыв, окно выгрузки).
# Используется хендлерами menu_router; не пересекается с FoodFlow.
class MenuFlow(StatesGroup):
    diary_pick_edit = State()
    diary_pick_delete = State()
    settings_day_hour = State()
    settings_calories = State()
    settings_goal = State()
    settings_goal_recalc = State()
    feedback_wait = State()
    export_month_pick = State()
    reminders_add_title = State()
    reminders_add_window = State()
    reminders_add_min_cal = State()
    reminders_list_pick = State()
    reminders_item_action = State()
    reminders_delete_confirm = State()
#endregion

#region Хранилище NocoDB (users / food_logs / reminders)
# Логическая дата последнего сброса is_triggered_today (process-local, до cron).
_reminder_day_reset: dict[int, str] = {}
# Фоновый upsert профиля после опроса: user_id → Task (пока читают про reminder).
_survey_profile_saves: dict[int, asyncio.Task[None]] = {}

# Кэш профиля users по Telegram id (process-local, как MemoryStorage).
_user_cache: dict[int, dict[str, Any]] = {}
# id, для которых кэш устарел — следующий get_user ждёт свежий GET.
_user_stale: set[int] = set()
# Поколение кэша: invalidate бампит gen, чтобы GET «до PATCH» не закоммитил устаревшее.
_user_gen: dict[int, int] = {}
# Singleflight: один активный GET на user_id+gen; параллельные читатели ждут тот же Future.
_user_inflight: dict[int, Future[dict[str, Any] | None]] = {}
_user_inflight_gen: dict[int, int] = {}
_user_cache_lock = threading.Lock()


# Профиль отсутствует в NocoDB — пользователь ещё не прошёл опрос / не в БД.
# Используется get_user; ловится error-handler → ветка как у /start.
class UserNotRegisteredError(LookupError):
    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        super().__init__(f"user {user_id} not found in NocoDB")


# Стартует GET users для текущего поколения; иначе возвращает тот же Future.
# Используется get_user и invalidate_user_cache (singleflight).
def _ensure_user_fetch(user_id: int) -> Future[dict[str, Any] | None]:
    with _user_cache_lock:
        gen = _user_gen.get(user_id, 0)
        existing = _user_inflight.get(user_id)
        if (
            existing is not None
            and not existing.done()
            and _user_inflight_gen.get(user_id) == gen
        ):
            return existing
        fut: Future[dict[str, Any] | None] = Future()
        _user_inflight[user_id] = fut
        _user_inflight_gen[user_id] = gen

    def worker() -> None:
        try:
            row = db.get_user(user_id)
            with _user_cache_lock:
                # Коммитим только если invalidate не сменил поколение пока шёл GET.
                if _user_gen.get(user_id, 0) == gen:
                    if row is None:
                        _user_cache.pop(user_id, None)
                    else:
                        _user_cache[user_id] = dict(row)
                    _user_stale.discard(user_id)
            fut.set_result(row)
        except Exception as e:
            fut.set_exception(e)
        finally:
            with _user_cache_lock:
                if _user_inflight.get(user_id) is fut:
                    del _user_inflight[user_id]
                    _user_inflight_gen.pop(user_id, None)

    threading.Thread(
        target=worker,
        name=f"user-fetch-{user_id}",
        daemon=True,
    ).start()
    return fut


# Помечает кэш users устаревшим и сразу запускает один GET (не дублируя inflight).
# Используется когда локально нет полного профиля для optimistic patch.
def invalidate_user_cache(user_id: int) -> None:
    if not user_id:
        return
    with _user_cache_lock:
        _user_stale.add(user_id)
        _user_gen[user_id] = _user_gen.get(user_id, 0) + 1
    _ensure_user_fetch(user_id)


# Патчит поля в кэше users без HTTP; нет записи → invalidate+GET.
# Используется после set_day_change_hour / set_goal / set_daily_calories / touch.
def _patch_user_cache(user_id: int, fields: dict[str, Any]) -> None:
    if not user_id:
        return
    with _user_cache_lock:
        row = _user_cache.get(user_id)
        if row is not None:
            row.update(fields)
            _user_stale.discard(user_id)
            # Бамп gen: GET «до PATCH» не перезапишет оптимистичный кэш.
            _user_gen[user_id] = _user_gen.get(user_id, 0) + 1
            return
    invalidate_user_cache(user_id)


# Кладёт полный профиль в кэш (после upsert_profile).
# Используется set_profile.
def _put_user_cache(user_id: int, row: dict[str, Any]) -> None:
    if not user_id:
        return
    with _user_cache_lock:
        _user_cache[user_id] = dict(row)
        _user_stale.discard(user_id)
        _user_gen[user_id] = _user_gen.get(user_id, 0) + 1


# Профиль пользователя: кэш по telegram_id, иначе GET (с ожиданием inflight).
# Используется экранами меню, выгрузкой и расчётом логической даты.
def get_user(user_id: int) -> dict[str, Any]:
    while True:
        with _user_cache_lock:
            cached = _user_cache.get(user_id)
            stale = user_id in _user_stale
            if cached is not None and not stale:
                return dict(cached)

        row = _ensure_user_fetch(user_id).result()

        with _user_cache_lock:
            # Пока ждали, мог прийти invalidate → нужен ещё один круг.
            if user_id in _user_stale:
                continue
            cached = _user_cache.get(user_id)
            if cached is not None:
                return dict(cached)

        if row is None:
            raise UserNotRegisteredError(user_id)
        return dict(row)


# Обновляет users.last_active_at (для заморозки напоминаний через 3 дня).
# Используется при активности в боте и перед проверкой триггеров.
def touch_user_activity(user_id: int) -> None:
    now = int(time.time())
    db.touch_user_activity(user_id)
    _patch_user_cache(user_id, {"last_active_at": now})


# Записи дневника за логическую дату YYYY-MM-DD.
# Используется главным меню, дневником, удалением и выгрузкой.
def get_food_logs_for_date(user_id: int, logged_date: str) -> list[dict[str, Any]]:
    return db.get_food_logs_for_date(user_id, logged_date)


# Записи за диапазон дат [date_from, date_to] включительно.
# Используется выгрузкой журнала за неделю/месяц.
def get_food_logs_range(
    user_id: int, date_from: str, date_to: str
) -> list[dict[str, Any]]:
    return db.get_food_logs_range(user_id, date_from, date_to)


# Удаление записи food_logs по id (только свои).
# Используется флоу «Удалить блюдо» в дневнике.
def delete_food_log(
    user_id: int, log_id: int, *, check_owner: bool = True
) -> bool:
    return db.delete_food_log(user_id, log_id, check_owner=check_owner)


# INSERT food_logs после ✅ распознавания (emoji → details_json).
# Используется _on_food_saved.
def insert_food_log_from_result(
    user_id: int, result: Any, logged_date: str
) -> dict[str, Any]:
    details: dict[str, Any]
    if hasattr(result, "model_dump"):
        details = result.model_dump()
    else:
        details = dict(result) if isinstance(result, dict) else {}
    return db.insert_food_log(
        user_id,
        title=str(getattr(result, "dish", None) or details.get("dish") or "Блюдо"),
        calories=int(getattr(result, "calories", 0) or 0),
        proteins=float(getattr(result, "proteins", 0) or 0),
        fats=float(getattr(result, "fats", 0) or 0),
        carbs=float(getattr(result, "carbs", 0) or 0),
        portion_g=float(getattr(result, "portion_g", 0) or 0),
        logged_date=logged_date,
        details_json=details,
    )


# Обновление day_change_hour в users + optimistic patch кэша.
# Используется настройкой «Время смены суток».
def set_day_change_hour(user_id: int, hour: int) -> None:
    db.set_day_change_hour(user_id, hour)
    _patch_user_cache(user_id, {"day_change_hour": int(hour)})


# Обновление goal в users + optimistic patch кэша.
# Используется настройкой «Тип отслеживания».
def set_goal(user_id: int, goal: str) -> None:
    db.set_goal(user_id, goal)
    _patch_user_cache(user_id, {"goal": goal})


# Обновление daily_calories в users + optimistic patch кэша.
# Используется настройкой «Целевые ккал».
def set_daily_calories(user_id: int, calories: int) -> None:
    db.set_daily_calories(user_id, calories)
    _patch_user_cache(user_id, {"daily_calories": int(calories)})


# Запись полей профиля из первичного опроса + кладёт ответ upsert в кэш.
# Используется on_survey_complete после прохождения initial_survey.
def set_profile(
    user_id: int,
    *,
    first_name: str,
    gender: str,
    age: int,
    height: float,
    weight: float,
    activity_level: float,
    goal: str,
    timezone: str,
    daily_calories: int,
) -> None:
    row = db.upsert_profile(
        user_id,
        first_name=first_name,
        gender=gender,
        age=age,
        height=height,
        weight=weight,
        activity_level=activity_level,
        goal=goal,
        timezone=timezone,
        daily_calories=daily_calories,
    )
    _put_user_cache(user_id, row)


# Вкл/выкл users.usage_reminder_enabled + patch кэша.
# Используется экраном «Напоминание использования бота».
def set_usage_reminder_enabled(user_id: int, enabled: bool) -> None:
    row = db.set_usage_reminder_enabled(user_id, enabled)
    _put_user_cache(user_id, row)


# Помечает, что usage-reminder уже отправлен за дату (антидубль).
# Используется фоновым чекером после успешной отправки в чат.
def mark_usage_reminder_sent(user_id: int, sent_on: str) -> None:
    db.mark_usage_reminder_sent(user_id, sent_on)
    _patch_user_cache(user_id, {"usage_reminder_sent_on": str(sent_on)})


# Подписи типов обратной связи для писем и UI.
# Используется флоу отзыва / сообщения об ошибке.
FEEDBACK_KIND_LABELS = {
    "bug": "Сообщение об ошибке",
    "idea": "Предложение по улучшению функционала",
}


# Синхронная отправка письма через SMTP (вызывается из to_thread).
# Используется send_feedback_email.
def _smtp_send_feedback(
    *,
    subject: str,
    body: str,
    photo_bytes: bytes | None,
    photo_filename: str,
) -> None:
    msg = MIMEMultipart()
    msg["From"] = SMTP_USER
    msg["To"] = FEEDBACK_TO_EMAIL
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    if photo_bytes:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(photo_bytes)
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition",
            f'attachment; filename="{photo_filename}"',
        )
        msg.attach(part)

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, [FEEDBACK_TO_EMAIL], msg.as_string())


# Отправка отзыва/багрепорта на почту разработчика (SMTP из .env).
# Используется хендлерами MenuFlow.feedback_wait (текст / фото).
async def send_feedback_email(
    *,
    user_id: int,
    username: str | None,
    kind: str,
    text: str,
    photo_bytes: bytes | None = None,
) -> None:
    if not SMTP_USER or not SMTP_PASSWORD:
        raise RuntimeError(
            "SMTP_USER / SMTP_PASSWORD не заданы в .env — письмо не отправлено"
        )

    kind_label = FEEDBACK_KIND_LABELS.get(kind, kind)
    uname = f"@{username}" if username else "—"
    subject = f"🍎 [NutriClick] {kind_label} — user {user_id}"
    body = (
        f"Тип: {kind_label}\n"
        f"user_id: {user_id}\n"
        f"username: {uname}\n"
        f"\n"
        f"Сообщение:\n"
        f"{text or '(без текста)'}\n"
    )
    photo_filename = "screenshot.jpg" if photo_bytes else ""
    await asyncio.to_thread(
        _smtp_send_feedback,
        subject=subject,
        body=body,
        photo_bytes=photo_bytes,
        photo_filename=photo_filename,
    )
    print(
        f"feedback sent kind={kind} user_id={user_id} "
        f"has_photo={bool(photo_bytes)}",
        flush=True,
    )


# Скачать фото Telegram в память (для вложения в письмо отзыва).
# Используется on_feedback_photo.
async def download_photo_bytes(bot: Bot, file_id: str) -> bytes:
    buffer = io.BytesIO()
    await bot.download(file_id, destination=buffer)
    return buffer.getvalue()


# Список напоминаний пользователя (reminders), от новых к старым по id.
# Используется экранами «Напоминания» / «Мои напоминания».
def get_reminders(user_id: int) -> list[dict[str, Any]]:
    return db.get_reminders(user_id)


# Одно напоминание по id (только своё).
# Используется карточкой напоминания, toggle/delete и snooze.
def get_reminder(user_id: int, reminder_id: int) -> dict[str, Any] | None:
    return db.get_reminder(user_id, reminder_id)


# Создание напоминания (reminders).
# Используется флоу «➕ Добавить напоминание».
def add_reminder(
    user_id: int,
    title: str,
    time_start: str,
    time_end: str,
    min_calories: int,
) -> dict[str, Any]:
    return db.add_reminder(user_id, title, time_start, time_end, min_calories)


# Вкл/выкл напоминания (reminders.is_active).
# Используется карточкой «Мои напоминания». check_owner=False если id из FSM.
def set_reminder_active(
    user_id: int,
    reminder_id: int,
    is_active: bool,
    *,
    check_owner: bool = True,
) -> dict[str, Any] | None:
    return db.set_reminder_active(
        user_id, reminder_id, is_active, check_owner=check_owner
    )


# Удаление напоминания.
# Используется карточкой «Мои напоминания». check_owner=False если id из FSM.
def delete_reminder(
    user_id: int, reminder_id: int, *, check_owner: bool = True
) -> bool:
    return db.delete_reminder(user_id, reminder_id, check_owner=check_owner)


# Параллельный PATCH нескольких reminders (одно поле на id).
# Используется сбросом суток и mark_triggered после еды.
def _patch_reminders_parallel(
    reminder_ids: list[int], fields: dict[str, Any]
) -> None:
    if not reminder_ids:
        return
    workers = min(8, len(reminder_ids))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [
            pool.submit(db.update_reminder, rid, fields) for rid in reminder_ids
        ]
        for fut in futs:
            fut.result()


# Сброс is_triggered_today при смене логических суток (process-local дата).
# rows — уже загруженный список (чтобы не делать второй GET). Возвращает актуальный list.
# Используется перед проверкой триггеров после сохранения еды.
def _reset_reminder_triggers_if_new_day(
    user_id: int, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    user = get_user(user_id)
    today = logical_today(user)
    if _reminder_day_reset.get(user_id) == today:
        return rows
    to_reset = [int(r["id"]) for r in rows if r.get("is_triggered_today")]
    if to_reset:
        _patch_reminders_parallel(to_reset, {"is_triggered_today": False})
        rows = [
            {**r, "is_triggered_today": False} if r.get("is_triggered_today") else r
            for r in rows
        ]
    _reminder_day_reset[user_id] = today
    return rows


# Пользователь «заморожен» по last_active_at (нет активности > N дней).
# Используется proactive/missed-уведомлениями; food-триггер обычно идёт при визите.
def reminders_frozen(user_id: int) -> bool:
    user = get_user(user_id)
    last = int(user.get("last_active_at") or 0)
    if last <= 0:
        return False
    return (time.time() - last) > REMINDER_FREEZE_AFTER_DAYS * 86400


# Перенос сработавшего напоминания на следующую еду (сброс is_triggered_today).
# Используется inline «⏰ На следующую еду» под уведомлением.
def snooze_reminder(
    user_id: int, reminder_id: int, *, check_owner: bool = True
) -> bool:
    return db.snooze_reminder(user_id, reminder_id, check_owner=check_owner)


# После INSERT в food_logs: активные reminders в текущем окне времени,
# calories >= min_calories, ещё не срабатывали сегодня → пометить и вернуть список.
# Один GET reminders + параллельные PATCH; без повторного list.
# Используется колбэком on_food_saved из food_recognition.
def trigger_reminders_for_food(
    user_id: int, calories: int
) -> list[dict[str, Any]]:
    touch_user_activity(user_id)
    rows = get_reminders(user_id)
    rows = _reset_reminder_triggers_if_new_day(user_id, rows)
    user = get_user(user_id)
    now_hm = datetime.now(ZoneInfo(user["timezone"])).strftime("%H:%M")
    kcal = int(calories or 0)
    triggered: list[dict[str, Any]] = []
    mark_ids: list[int] = []
    for row in rows:
        if not row.get("is_active"):
            continue
        if row.get("is_triggered_today"):
            continue
        if not (row["time_start"] <= now_hm <= row["time_end"]):
            continue
        if kcal < int(row.get("min_calories") or 0):
            continue
        mark_ids.append(int(row["id"]))
        updated = dict(row)
        updated["is_triggered_today"] = True
        triggered.append(updated)
        print(
            f"reminder triggered user_id={user_id} id={updated['id']} "
            f"title={updated['title']!r} kcal={kcal}",
            flush=True,
        )
    _patch_reminders_parallel(mark_ids, {"is_triggered_today": True})
    return triggered


# 🎈 Заглушка проверки «окно закончилось, еду не залогировали» (будущий cron/job).
# Сейчас не вызывается — планировщик подключим отдельно.
def check_missed_reminders(user_id: int) -> list[dict[str, Any]]:
    # 🎈 SELECT active reminders WHERE now > time_end AND NOT is_triggered_today
    #    AND user not frozen; затем уведомить «пропущено».
    if reminders_frozen(user_id):
        return []
    _ = user_id
    return []
#endregion

#region Дата / прогресс / форматтеры
# Логическая «сегодняшняя» дата YYYY-MM-DD с учётом timezone и day_change_hour.
# Используется дневником, главным меню и выгрузкой.
def logical_today(user: dict[str, Any]) -> str:
    tz = ZoneInfo(user["timezone"])
    now = datetime.now(tz)
    if now.hour < int(user["day_change_hour"]):
        now = now - timedelta(days=1)
    return now.strftime("%Y-%m-%d")


# Дата со смещением от логического «сегодня» (offset дней).
# Используется навигацией дневника и периодами выгрузки.
def logical_date_with_offset(user: dict[str, Any], offset_days: int) -> str:
    base = datetime.strptime(logical_today(user), "%Y-%m-%d").date()
    return (base + timedelta(days=offset_days)).strftime("%Y-%m-%d")


# Эмодзи блюда из записи food_logs (fallback 🍽).
# Используется списками главного меню, дневника, выбора и выгрузки.
def format_log_emoji(row: dict[str, Any]) -> str:
    emoji = (row.get("emoji") or "").strip()
    return emoji or "🍽"


# Граммы макронутриента для UI: целое число + суффикс g.
# Используется строкой БЖУ на карточке дня.
def format_macro_g(value: float | int) -> str:
    return f"{int(round(float(value)))}g"


# Прогресс-бар ккал: ▓ при недоборе, полные █ при перерасходе.
# Возвращает (bar_text, pct). Используется format_day_card.
def format_calorie_bar(eaten: int, target: int) -> tuple[str, int]:
    if target <= 0:
        return "[░░░░░░░░░░░░]", 0
    pct = int(round(100 * eaten / target))
    if eaten > target:
        return "[" + ("█" * 12) + "]", pct
    filled = min(12, int(round(12 * eaten / target)))
    bar = "▓" * filled + "░" * (12 - filled)
    return f"[{bar}]", pct


# Время HH:MM из unix created_at в TZ пользователя.
# Используется списками блюд в меню и дневнике.
def format_log_time(created_at: int, timezone: str) -> str:
    dt = datetime.fromtimestamp(created_at, ZoneInfo(timezone))
    return dt.strftime("%H:%M")


# Полная дата+время для строк выгрузки.
# Используется генератором .txt журнала.
def format_log_datetime(created_at: int, timezone: str) -> str:
    dt = datetime.fromtimestamp(created_at, ZoneInfo(timezone))
    return dt.strftime("%Y-%m-%d %H:%M")


# Граммы макронутриента в формате дневника: «14гр.».
# Используется format_log_entry_diary.
def format_macro_gr(value: float | int) -> str:
    return f"{int(round(float(value)))}гр."


# Блок одной записи дневника: время → блюдо/ккал → курсив с БЖУ.
# Используется format_day_card при show_item_macros=True.
def format_log_entry_diary(row: dict[str, Any], timezone: str) -> list[str]:
    t = format_log_time(row["created_at"], timezone)
    emoji = format_log_emoji(row)
    dish = html.escape(str(row.get("title") or "Блюдо"))
    cal = int(row["calories"] or 0)
    p = format_macro_gr(row.get("proteins") or 0)
    f = format_macro_gr(row.get("fats") or 0)
    c = format_macro_gr(row.get("carbs") or 0)
    indent = "      "
    return [
        f"▫️ <code>{t}</code>",
        f"{indent}{emoji} {dish} — <b>{cal} ккал</b>",
        f"{indent}<i>– (Б {p} • Ж {f} • У {c})</i>",
    ]


# Текст карточки дня (HTML): ккал/бар/БЖУ + список записей или пустой день.
# show_item_macros=True — многострочный формат записи с БЖУ (только дневник).
# Используется главным меню и экраном дневника (parse_mode=HTML).
def format_day_card(
    user: dict[str, Any],
    logged_date: str,
    logs: list[dict[str, Any]],
    *,
    is_today: bool,
    title: str,
    show_item_macros: bool = False,
) -> str:
    lines = [title, ""]
    if not logs:
        if is_today:
            lines.append(
                "За сегодня записей нет. Отправь фото или описание блюда, "
                "чтобы зафиксировать прием пищи!"
            )
        else:
            lines.append("За этот день записей нет")
        return "\n".join(lines)

    eaten = sum(int(r["calories"] or 0) for r in logs)
    target = int(user["daily_calories"])
    proteins = sum(float(r.get("proteins") or 0) for r in logs)
    fats = sum(float(r.get("fats") or 0) for r in logs)
    carbs = sum(float(r.get("carbs") or 0) for r in logs)
    bar, pct = format_calorie_bar(eaten, target)
    over = eaten > target

    lines.append(
        f"🔥 <b>Калории:</b> <code>{eaten}</code> / <code>{target}</code> ккал "
        f"({pct}%)"
    )
    if over:
        lines.append(f"<code>{bar}</code>")
        lines.append("")
        # При наборе веса превышение нормы — позитивный «донабор», иначе «перебор».
        if user.get("goal") == "muscle_gain":
            over_label = "💪 <b>Донабор:</b>"
        else:
            over_label = "⚠️ <b>Перебор:</b>"
        lines.append(
            f"{over_label} <code>+{eaten - target} ккал</code>"
        )
    else:
        remaining = target - eaten
        if remaining > 0:
            lines.append(f"<code>{bar}</code> (Осталось: {remaining} ккал)")
        else:
            lines.append(f"<code>{bar}</code>")

    lines.append(
        f"🥩 <code>{format_macro_g(proteins)}</code> Б | "
        f"🥑 <code>{format_macro_g(fats)}</code> Ж | "
        f"🍞 <code>{format_macro_g(carbs)}</code> У"
    )
    lines.append("")
    lines.append("📋 <b>Записи за день:</b>")
    lines.append("")
    for i, row in enumerate(logs):
        if show_item_macros:
            if i > 0:
                lines.append("")
            lines.extend(format_log_entry_diary(row, user["timezone"]))
        else:
            t = format_log_time(row["created_at"], user["timezone"])
            emoji = format_log_emoji(row)
            dish = html.escape(str(row.get("title") or "Блюдо"))
            cal = int(row["calories"] or 0)
            lines.append(
                f"▫️ <code>{t}</code> {emoji} {dish} — <b>{cal} ккал</b>"
            )
    return "\n".join(lines)


# Человекочитаемая подпись цели (weight_loss → Похудение).
# Используется настройками и шапкой выгрузки.
def goal_label(goal: str) -> str:
    return GOAL_LABELS.get(goal, goal)


# Человекочитаемая подпись уровня активности по коэффициенту.
# Используется сводкой профиля в «Изменить данные профиля».
def activity_label(level: float | int | None) -> str:
    if level is None:
        return "—"
    try:
        key = float(level)
    except (TypeError, ValueError):
        return str(level)
    return ACTIVITY_LABELS.get(key, str(level))


# Сводка характеристик профиля (имя, пол, рост, вес и т.д.).
# Используется экраном «Изменить данные профиля».
def format_profile_summary(user: dict[str, Any]) -> str:
    gender = "М" if user.get("gender") == "male" else "Ж"
    return (
        f"Имя: {user.get('first_name', '—')}\n"
        f"Пол: {gender}\n"
        f"Возраст: {user.get('age', '—')}\n"
        f"Рост: {user.get('height', '—')} см\n"
        f"Вес: {user.get('weight', '—')} кг\n"
        f"Активность: {activity_label(user.get('activity_level'))}\n"
        f"Тип отслеживания: {goal_label(user.get('goal', ''))}\n"
        f"Норма: {user.get('daily_calories', '—')} ккал/сутки"
    )


# Содержимое .txt выгрузки: промпт-шапка + строки дневника.
# Используется хендлерами периодов выгрузки.
def build_export_txt(
    user: dict[str, Any], logs: list[dict[str, Any]], period_title: str
) -> str:
    gender = "М" if user.get("gender") == "male" else "Ж"
    header = (
        "Вот данные из дневника питания, такие характеристики: "
        f"{gender}, рост {user.get('height')} см, вес {user.get('weight')} кг, "
        f"тип отслеживания: {goal_label(user.get('goal', ''))}. "
        "Проанализируй данные, и дай рекомендации по питанию.\n"
        f"\nПериод выгрузки: {period_title}\n"
        f"{'=' * 40}\n"
    )
    if not logs:
        body = "Записей за выбранный период нет.\n"
    else:
        parts: list[str] = []
        for row in logs:
            dt = format_log_datetime(row["created_at"], user["timezone"])
            parts.append(
                f"{dt} | {format_log_emoji(row)} {row['title']}\n"
                f"  ккал: {row['calories']}, "
                f"Б: {row['proteins']} г, Ж: {row['fats']} г, У: {row['carbs']} г, "
                f"порция: {row['portion_g']} г\n"
            )
        body = "\n".join(parts)
    return header + body
#endregion

#region Клавиатуры
# Reply-клавиатура корня главного меню.
# Используется show_main_menu и возвратами «Назад» / 🏠.
def kb_main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_DIARY)],
            [KeyboardButton(text=BTN_RECOGNIZE), KeyboardButton(text=BTN_SETTINGS)],
        ],
        resize_keyboard=True,
    )


# Reply-клавиатура раздела «Дневник питания».
# Используется show_diary и возвратами из подменю дневника.
def kb_diary() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_ADD_DISH)],
            [KeyboardButton(text=BTN_EDIT_DISH), KeyboardButton(text=BTN_DELETE_DISH)],
            [KeyboardButton(text=BTN_BACK), KeyboardButton(text=BTN_MAIN_MENU)],
        ],
        resize_keyboard=True,
    )


# Reply-клавиатура выбора номера блюда: узкие кнопки 1…N по 10 на страницу + «Назад».
# Используется флоу «Изменить блюдо» / «Удалить блюдо».
def kb_pick_dish(total: int, page: int = 0) -> ReplyKeyboardMarkup:
    if total <= 0:
        return ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=BTN_BACK)]],
            resize_keyboard=True,
        )
    max_page = max(0, (total - 1) // PICK_PAGE_SIZE)
    page = max(0, min(page, max_page))
    start = page * PICK_PAGE_SIZE + 1
    end = min(total, (page + 1) * PICK_PAGE_SIZE)

    rows: list[list[KeyboardButton]] = []
    row: list[KeyboardButton] = []
    for n in range(start, end + 1):
        row.append(KeyboardButton(text=str(n)))
        # По 5 в ряд — кнопки уже, чем полный ряд дневника.
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    nav: list[KeyboardButton] = []
    if page > 0:
        nav.append(KeyboardButton(text=BTN_PICK_PAGE_PREV))
    if page < max_page:
        nav.append(KeyboardButton(text=BTN_PICK_PAGE_NEXT))
    if nav:
        rows.append(nav)

    rows.append([KeyboardButton(text=BTN_BACK)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


# Текст экрана выбора блюда (edit/delete) с учётом текущей страницы номеров.
# Используется стартом флоу и перелистыванием страниц кнопок.
def format_dish_pick_prompt(
    *,
    mode: str,
    user: dict[str, Any],
    logs: list[dict[str, Any]],
    page: int,
) -> str:
    total = len(logs)
    max_page = max(0, (total - 1) // PICK_PAGE_SIZE) if total else 0
    page = max(0, min(page, max_page))
    start = page * PICK_PAGE_SIZE + 1
    end = min(total, (page + 1) * PICK_PAGE_SIZE)
    title = "✏️ Изменить блюдо" if mode == "edit" else "🗑 Удалить блюдо"
    page_hint = ""
    if total > PICK_PAGE_SIZE:
        page_hint = (
            f"\nКнопки на экране: {start}–{end} "
            f"(стр. {page + 1}/{max_page + 1})"
        )
    return (
        f"{title}\n"
        "\n"
        "Выберите номер блюда кнопкой:\n"
        f"{format_numbered_logs(user, logs)}"
        f"{page_hint}\n"
        "\n"
        "Или нажмите «⬅️ Назад»"
    )


# Reply-клавиатура «Дополнительно» / выгрузки.
# Используется экраном доп. меню и настройкой «Сделать выгрузку».
def kb_export() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_EXPORT_TODAY)],
            [KeyboardButton(text=BTN_EXPORT_YESTERDAY)],
            [KeyboardButton(text=BTN_EXPORT_WEEK)],
            [KeyboardButton(text=BTN_EXPORT_MONTH)],
            [KeyboardButton(text=BTN_BACK), KeyboardButton(text=BTN_MAIN_MENU)],
        ],
        resize_keyboard=True,
    )


# Reply-клавиатура выбора окна «месяц» (0–30 / 30–60 / 60–90).
# Используется флоу BTN_EXPORT_MONTH.
def kb_export_month() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_MONTH_0_30)],
            [KeyboardButton(text=BTN_MONTH_30_60)],
            [KeyboardButton(text=BTN_MONTH_60_90)],
            [KeyboardButton(text=BTN_BACK), KeyboardButton(text=BTN_MAIN_MENU)],
        ],
        resize_keyboard=True,
    )


# Reply-клавиатура экрана «Распознать» (возврат в корень через «Назад»).
# Используется show_recognize.
def kb_recognize() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_BACK)]],
        resize_keyboard=True,
    )


# Reply-клавиатура только навигации: «Назад» + «Главное меню».
# Используется экранами ввода (смена суток, целевые ккал, обратная связь).
def kb_nav_only() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_BACK), KeyboardButton(text=BTN_MAIN_MENU)]],
        resize_keyboard=True,
    )


# Reply-клавиатура раздела «Настройки».
# Используется show_settings.
def kb_settings() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_SET_PROFILE)],
            [KeyboardButton(text=BTN_SET_DAY_HOUR)],
            [KeyboardButton(text=BTN_SET_REMINDERS)],
            [KeyboardButton(text=BTN_SET_EXPORT)],
            [KeyboardButton(text=BTN_SET_HELP)],
            [KeyboardButton(text=BTN_SET_FEEDBACK)],
            [KeyboardButton(text=BTN_BACK), KeyboardButton(text=BTN_MAIN_MENU)],
        ],
        resize_keyboard=True,
    )


# Reply-клавиатура FAQ: обзор + темы с подробными ответами.
# Используется show_help и хендлерами BTN_HELP_*.
def kb_help() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_HELP_OVERVIEW)],
            [KeyboardButton(text=BTN_HELP_RECOGNIZE)],
            [KeyboardButton(text=BTN_HELP_DIARY)],
            [KeyboardButton(text=BTN_HELP_REMINDERS)],
            [KeyboardButton(text=BTN_HELP_EXPORT)],
            [KeyboardButton(text=BTN_HELP_DAY_HOUR)],
            [KeyboardButton(text=BTN_BACK), KeyboardButton(text=BTN_MAIN_MENU)],
        ],
        resize_keyboard=True,
    )


# Reply-клавиатура выбора типа обратной связи.
# Используется show_feedback_menu.
def kb_feedback() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_FEEDBACK_BUG)],
            [KeyboardButton(text=BTN_FEEDBACK_IDEA)],
            [KeyboardButton(text=BTN_BACK), KeyboardButton(text=BTN_MAIN_MENU)],
        ],
        resize_keyboard=True,
    )


# Reply-клавиатура раздела «Напоминания и Витамины».
# Используется show_reminders.
def kb_reminders() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_REM_ADD)],
            [KeyboardButton(text=BTN_REM_LIST)],
            [KeyboardButton(text=BTN_REM_USAGE)],
            [KeyboardButton(text=BTN_BACK), KeyboardButton(text=BTN_MAIN_MENU)],
        ],
        resize_keyboard=True,
    )


# Reply-клавиатура экрана вкл/выкл usage-reminder.
# Используется show_usage_reminder_settings.
def kb_usage_reminder_settings() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_REM_TOGGLE)],
            [KeyboardButton(text=BTN_BACK), KeyboardButton(text=BTN_MAIN_MENU)],
        ],
        resize_keyboard=True,
    )


# Inline «🟩 Хорошо» под текстом про usage-reminder после опроса.
# Используется _on_survey_complete.
def kb_survey_usage_ok() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🟩 Хорошо",
                    callback_data=CALLBACK_SURVEY_USAGE_OK,
                )
            ]
        ]
    )


# Reply-клавиатура выбора временного окна напоминания.
# Используется флоу «➕ Добавить напоминание» (шаг окна).
def kb_reminder_windows() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_REM_WINDOW_BREAKFAST)],
            [KeyboardButton(text=BTN_REM_WINDOW_LUNCH)],
            [KeyboardButton(text=BTN_REM_WINDOW_DINNER)],
            [KeyboardButton(text=BTN_BACK), KeyboardButton(text=BTN_MAIN_MENU)],
        ],
        resize_keyboard=True,
    )


# Reply-клавиатура порога калорий для срабатывания напоминания.
# Используется флоу «➕ Добавить напоминание» (шаг «реагировать на»).
def kb_reminder_min_cal() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_REM_ANY_FOOD)],
            [KeyboardButton(text=BTN_REM_HEARTY)],
            [KeyboardButton(text=BTN_BACK), KeyboardButton(text=BTN_MAIN_MENU)],
        ],
        resize_keyboard=True,
    )


# Reply-клавиатура действий с одним напоминанием (вкл/выкл / удалить).
# Используется карточкой выбранного напоминания.
def kb_reminder_item() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_REM_TOGGLE)],
            [KeyboardButton(text=BTN_REM_DELETE)],
            [KeyboardButton(text=BTN_BACK), KeyboardButton(text=BTN_MAIN_MENU)],
        ],
        resize_keyboard=True,
    )


# Reply-клавиатура подтверждения удаления напоминания.
# Используется флоу «🗑 Удалить» → reminders_delete_confirm.
def kb_confirm_delete_reminder() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_REM_DELETE_YES)],
            [KeyboardButton(text=BTN_REM_DELETE_NO)],
            [KeyboardButton(text=BTN_BACK), KeyboardButton(text=BTN_MAIN_MENU)],
        ],
        resize_keyboard=True,
    )


# Inline под уведомлением о напоминании: перенос на следующую еду / ок.
# Используется notify_reminders_after_food.
def kb_reminder_notify(reminder_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⏰ На следующую еду",
                    callback_data=f"{CALLBACK_REM_SNOOZE_PREFIX}{reminder_id}",
                ),
                InlineKeyboardButton(
                    text="✅ Понятно",
                    callback_data=f"{CALLBACK_REM_OK_PREFIX}{reminder_id}",
                ),
            ]
        ]
    )


# Reply-клавиатура «Изменить данные профиля» (опрос / цель / ккал).
# Используется show_profile и возвратами из подпунктов профиля.
def kb_profile() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_UPDATE_PROFILE)],
            [KeyboardButton(text=BTN_SET_GOAL)],
            [KeyboardButton(text=BTN_SET_CALORIES)],
            [KeyboardButton(text=BTN_BACK), KeyboardButton(text=BTN_MAIN_MENU)],
        ],
        resize_keyboard=True,
    )


# Reply-клавиатура подтверждения перезапуска первоначального опроса.
# Используется флоу BTN_UPDATE_PROFILE.
def kb_confirm_update_profile() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_CONFIRM_UPDATE_YES)],
            [KeyboardButton(text=BTN_CONFIRM_UPDATE_NO)],
            [KeyboardButton(text=BTN_BACK), KeyboardButton(text=BTN_MAIN_MENU)],
        ],
        resize_keyboard=True,
    )


# Reply-клавиатура выбора типа отслеживания.
# Используется флоу BTN_SET_GOAL.
def kb_goal() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_GOAL_LOSS)],
            [KeyboardButton(text=BTN_GOAL_GAIN)],
            [KeyboardButton(text=BTN_GOAL_MAINTAIN)],
            [KeyboardButton(text=BTN_BACK), KeyboardButton(text=BTN_MAIN_MENU)],
        ],
        resize_keyboard=True,
    )


# Reply-клавиатура: пересчитать целевые ккал после смены типа отслеживания?
# Используется флоу settings_goal → settings_goal_recalc.
def kb_confirm_recalc_calories() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_CONFIRM_RECALC_YES)],
            [KeyboardButton(text=BTN_CONFIRM_RECALC_NO)],
            [KeyboardButton(text=BTN_BACK), KeyboardButton(text=BTN_MAIN_MENU)],
        ],
        resize_keyboard=True,
    )


# Inline «Вчера / Завтра» под сообщением дневника.
# «Завтра» скрыта на текущем дне (offset >= 0), чтобы не уходить в будущее.
# Используется show_diary.
def kb_diary_nav(offset: int = 0) -> InlineKeyboardMarkup:
    row: list[InlineKeyboardButton] = [
        InlineKeyboardButton(text="◀️ Вчера", callback_data=CALLBACK_DIARY_PREV),
    ]
    if offset < 0:
        row.append(
            InlineKeyboardButton(text="▶️ Завтра", callback_data=CALLBACK_DIARY_NEXT)
        )
    return InlineKeyboardMarkup(inline_keyboard=[row])
#endregion

#region UI: экраны меню
# Reply-кнопки → новое сообщение; если есть «Выберите действие:» — правим его в новый
# экран (как «✨ Анализирую…» → превью), без delete и анимации пыли.
# Inline под карточкой дневника → edit_text.
UI_MESSAGE_ID_KEY = "ui_message_id"
UI_ACTION_MSG_IDS_KEY = "ui_action_msg_ids"


# Забирает id служебных «Выберите действие:» из FSM (список в state очищается).
# Используется replace_ui / show_diary / show_main_menu перед показом нового экрана.
async def pop_action_prompt_ids(state: FSMContext) -> list[int]:
    data = await state.get_data()
    ids: list[int] = list(data.get(UI_ACTION_MSG_IDS_KEY) or [])
    if ids:
        await state.update_data(**{UI_ACTION_MSG_IDS_KEY: []})
    return ids


# Скрывает лишние служебные сообщения через edit в невидимый символ (без анимации пыли).
# delete в Telegram всегда с «распадом»; edit — нет. Используется для хвостов после morph.
async def dismiss_action_prompts(
    bot: Bot, chat_id: int, message_ids: list[int]
) -> None:
    for mid in message_ids:
        try:
            await bot.edit_message_text(
                text="\u2060",
                chat_id=chat_id,
                message_id=mid,
            )
        except Exception:
            pass


# Ставит Reply-клавиатуру коротким служебным сообщением и сразу удаляет его.
# Используется после edit «Выберите действие:» → новый экран (edit не умеет ReplyKeyboard).
async def push_reply_keyboard(
    message: Message, reply_markup: ReplyKeyboardMarkup
) -> None:
    try:
        stub = await message.answer("\u2060", reply_markup=reply_markup)
        await stub.delete()
    except Exception:
        pass


# Экран по Reply-кнопке. Если есть «Выберите действие:» — превращаем его в новый текст
# (без пыли, как статус анализа); иначе шлём новое сообщение.
# Используется show_* и промптами подменю. parse_mode — для HTML (FAQ и т.п.).
async def replace_ui(
    message: Message,
    state: FSMContext,
    text: str,
    reply_markup: ReplyKeyboardMarkup | InlineKeyboardMarkup | None = None,
    parse_mode: str | None = None,
) -> Message:
    stale_ids = await pop_action_prompt_ids(state)
    if stale_ids:
        try:
            edited = await message.bot.edit_message_text(
                text=text,
                chat_id=message.chat.id,
                message_id=stale_ids[0],
                parse_mode=parse_mode,
            )
            if isinstance(reply_markup, ReplyKeyboardMarkup):
                await push_reply_keyboard(message, reply_markup)
            elif isinstance(reply_markup, InlineKeyboardMarkup):
                await message.bot.edit_message_reply_markup(
                    chat_id=message.chat.id,
                    message_id=stale_ids[0],
                    reply_markup=reply_markup,
                )
            mid = (
                edited.message_id
                if isinstance(edited, Message)
                else stale_ids[0]
            )
            await state.update_data(**{UI_MESSAGE_ID_KEY: mid})
            if len(stale_ids) > 1:
                await dismiss_action_prompts(
                    message.bot, message.chat.id, stale_ids[1:]
                )
            return edited if isinstance(edited, Message) else message
        except Exception:
            pass

    sent = await message.answer(
        text, reply_markup=reply_markup, parse_mode=parse_mode
    )
    await state.update_data(**{UI_MESSAGE_ID_KEY: sent.message_id})
    if stale_ids:
        await dismiss_action_prompts(message.bot, message.chat.id, stale_ids)
    return sent
#endregion

#region Показ экранов
# Показывает главное меню: блюда логического «сегодня» + прогресс + клавиатура.
# Используется /start, 🏠, «Назад» из разделов.
async def show_main_menu(
    message: Message, state: FSMContext, user_id: int | None = None
) -> None:
    stale_ids = await pop_action_prompt_ids(state)
    await state.clear()
    await state.update_data(diary_offset=0, export_return="main", menu_screen="main")
    uid = user_id if user_id is not None else (message.from_user.id if message.from_user else 0)
    user = get_user(uid)
    user_id = uid
    today = logical_today(user)
    logs = get_food_logs_for_date(user_id, today)
    text = format_day_card(
        user,
        today,
        logs,
        is_today=True,
        title="🏠 <b>Главный экран | Сегодня</b>",
    )
    if stale_ids:
        try:
            edited = await message.bot.edit_message_text(
                text=text,
                chat_id=message.chat.id,
                message_id=stale_ids[0],
                parse_mode="HTML",
            )
            await push_reply_keyboard(message, kb_main_menu())
            mid = (
                edited.message_id
                if isinstance(edited, Message)
                else stale_ids[0]
            )
            await state.update_data(**{UI_MESSAGE_ID_KEY: mid})
            if len(stale_ids) > 1:
                await dismiss_action_prompts(
                    message.bot, message.chat.id, stale_ids[1:]
                )
            return
        except Exception:
            pass

    sent = await message.answer(
        text, reply_markup=kb_main_menu(), parse_mode="HTML"
    )
    await state.update_data(**{UI_MESSAGE_ID_KEY: sent.message_id})
    if stale_ids:
        await dismiss_action_prompts(message.bot, message.chat.id, stale_ids)


# Показывает дневник за дату с diary_offset + inline-навигацию.
# Используется кнопкой «Дневник» и callback вчера/завтра.
# user_id обязателен из callback: у callback.message.from_user — бот, не пользователь.
# edit_message: только для inline «Вчера/Завтра» — правим карточку на месте.
async def show_diary(
    message: Message,
    state: FSMContext,
    user_id: int | None = None,
    edit_message: Message | None = None,
) -> None:
    data = await state.get_data()
    offset = int(data.get("diary_offset", 0))
    await state.set_state(None)
    await state.update_data(
        export_return="diary",
        diary_offset=offset,
        menu_screen="diary",
    )
    uid = user_id if user_id is not None else (message.from_user.id if message.from_user else 0)
    user = get_user(uid)
    user_id = uid
    logged_date = logical_date_with_offset(user, offset)
    logs = get_food_logs_for_date(user_id, logged_date)
    is_today = offset == 0
    title = f"📒 <b>Дневник питания</b> — {logged_date}"
    text = format_day_card(
        user,
        logged_date,
        logs,
        is_today=is_today,
        title=title,
        show_item_macros=True,
    )
    nav = kb_diary_nav(offset)

    # Inline-навигация: только правка карточки, «Выберите действие:» не трогаем.
    if edit_message is not None:
        try:
            await edit_message.edit_text(text, reply_markup=nav, parse_mode="HTML")
            await state.update_data(**{UI_MESSAGE_ID_KEY: edit_message.message_id})
            return
        except Exception:
            pass

    # Reply-вход: новая карточка + «Выберите действие:»; старое служебное — edit без пыли.
    stale_ids = await pop_action_prompt_ids(state)
    card = await message.answer(text, reply_markup=nav, parse_mode="HTML")
    actions = await message.answer("Выберите действие:", reply_markup=kb_diary())
    await state.update_data(
        **{
            UI_MESSAGE_ID_KEY: card.message_id,
            UI_ACTION_MSG_IDS_KEY: [actions.message_id],
        }
    )
    if stale_ids:
        await dismiss_action_prompts(message.bot, message.chat.id, stale_ids)


# Памятка «Распознать» без запуска анализа.
# Используется кнопкой 🔍 Распознать и финалом первичного опроса.
async def show_recognize(message: Message, state: FSMContext) -> None:
    await state.set_state(None)
    await state.update_data(menu_screen="recognize")
    await replace_ui(
        message, state, RECOGNIZE_HINT_TEXT, reply_markup=kb_recognize()
    )


# Экран настроек.
# Используется кнопкой ⚙️ Настройки и возвратами из подменю.
async def show_settings(message: Message, state: FSMContext) -> None:
    await state.set_state(None)
    await state.update_data(export_return="settings", menu_screen="settings")
    user_id = message.from_user.id if message.from_user else 0
    # user и reminders независимы по user_id — один roundtrip-стенд вместо двух.
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_user = pool.submit(get_user, user_id)
        fut_rems = pool.submit(get_reminders, user_id)
        user = fut_user.result()
        rem_count = len(fut_rems.result())
    text = (
        "⚙️ Настройки\n"
        "\n"
        f"Смена суток: {user['day_change_hour']:02d}:00 "
        f"({user['timezone']})\n"
        f"Цель: {goal_label(user['goal'])}\n"
        f"Норма: {user['daily_calories']} ккал/сутки\n"
        f"Напоминания: {rem_count}"
    )
    await replace_ui(message, state, text, reply_markup=kb_settings())


# Подменю обратной связи: ошибка или предложение.
# Используется кнопкой BTN_SET_FEEDBACK и «Назад» из ввода отзыва.
async def show_feedback_menu(message: Message, state: FSMContext) -> None:
    await state.set_state(None)
    await state.update_data(menu_screen="feedback", feedback_kind=None)
    await replace_ui(
        message,
        state,
        "💬 Обратная связь\n"
        "\n"
        "Выберите, что хотите отправить:",
        reply_markup=kb_feedback(),
    )


# Экран FAQ: общий обзор возможностей + кнопки тем.
# Используется кнопкой BTN_SET_HELP, «Обзор возможностей» и «Назад» из настроек.
async def show_help(message: Message, state: FSMContext) -> None:
    await state.set_state(None)
    await state.update_data(menu_screen="help")
    await replace_ui(
        message,
        state,
        HELP_OVERVIEW_TEXT,
        reply_markup=kb_help(),
        parse_mode="HTML",
    )


# Показать текст одной темы FAQ, оставаясь в разделе инструкций.
# Используется хендлерами кнопок BTN_HELP_* (кроме обзора).
async def show_help_topic(message: Message, state: FSMContext, topic_btn: str) -> None:
    text = HELP_TOPIC_TEXTS.get(topic_btn)
    if text is None:
        await show_help(message, state)
        return
    await state.set_state(None)
    await state.update_data(menu_screen="help")
    await replace_ui(
        message,
        state,
        text,
        reply_markup=kb_help(),
        parse_mode="HTML",
    )


# Подпись порога калорий напоминания для UI.
# Используется списками и карточкой напоминания.
def format_reminder_min_cal(min_calories: int) -> str:
    if int(min_calories or 0) <= 0:
        return "любая еда"
    return f"сытный приём (>{int(min_calories)} ккал)"


# Одна строка напоминания в нумерованном списке.
# Используется format_reminders_list и экраном «Мои напоминания».
def format_reminder_list_item(index: int, row: dict[str, Any]) -> str:
    status = "✅" if row.get("is_active") else "⏸"
    return (
        f"{index}. {status} {row['title']}\n"
        f"⠀⠀⠀{row['time_start']}–{row['time_end']} · "
        f"{format_reminder_min_cal(int(row.get('min_calories') or 0))}"
    )


# Нумерованный список напоминаний пользователя.
# Используется экраном «📋 Мои напоминания».
def format_reminders_list(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "Пока нет ни одного напоминания"
    return "\n\n".join(
        format_reminder_list_item(i, row) for i, row in enumerate(rows, start=1)
    )


# Текст карточки одного напоминания (детали + статус).
# Используется экраном управления выбранным напоминанием.
def format_reminder_card(row: dict[str, Any]) -> str:
    status = "включено" if row.get("is_active") else "выключено"
    return (
        f"🔔 {row['title']}\n"
        "\n"
        f"Окно: {row['time_start']}–{row['time_end']}\n"
        f"Реагирует на: {format_reminder_min_cal(int(row.get('min_calories') or 0))}\n"
        f"Статус: {status}"
    )


# Экран «🔔 Напоминания и Витамины»: описание + добавить / список / usage.
# Используется пунктом настроек и возвратами из подфлоу напоминаний.
async def show_reminders(message: Message, state: FSMContext) -> None:
    await state.set_state(None)
    await state.update_data(menu_screen="reminders")
    text = (
        "🔔 Напоминания и Витамины\n"
        "\n"
        "Бот напомнит о витаминах или другом деле перед едой: "
        "как только вы запишете приём пищи в выбранном окне времени "
        "(завтрак / обед / ужин), вам сразу придёт уведомление.\n"
        "\n"
        "Можно реагировать на любую еду или только на сытный приём "
        f"(>{REMINDER_HEARTY_MIN_KCAL} ккал). "
        "Напоминания работают каждый день; если не заходить в бота "
        f"больше {REMINDER_FREEZE_AFTER_DAYS} дней — они замораживаются.\n"
        "\n"
        "Отдельно можно включить напоминание об использовании бота "
        f"(если до {USAGE_REMINDER_HOUR:02d}:00 не было фиксаций еды).\n"
        "\n"
        "Выберите действие:"
    )
    await replace_ui(message, state, text, reply_markup=kb_reminders())


# Экран вкл/выкл напоминания «нет еды до 13:00».
# Используется кнопкой BTN_REM_USAGE и после toggle.
async def show_usage_reminder_settings(
    message: Message, state: FSMContext
) -> None:
    await state.set_state(None)
    await state.update_data(menu_screen="usage_reminder")
    user_id = message.from_user.id if message.from_user else 0
    user = get_user(user_id)
    enabled = bool(user.get("usage_reminder_enabled", True))
    status = "включено ✅" if enabled else "выключено ⏸"
    text = (
        "📲 Напоминание использования бота\n"
        "\n"
        f"Если до {USAGE_REMINDER_HOUR:02d}:00 по вашему часовому поясу "
        "не будет ни одной фиксации еды — бот пришлёт мягкое напоминание. "
        "Работает каждый день.\n"
        "\n"
        f"Сейчас: {status}"
    )
    await replace_ui(
        message, state, text, reply_markup=kb_usage_reminder_settings()
    )


# Шлёт уведомления по сработавшим reminders после сохранения еды.
# Используется колбэком on_food_saved из food_recognition.
async def notify_reminders_after_food(
    user_id: int,
    calories: int,
    bot: Bot,
    chat_id: int,
) -> None:
    triggered = trigger_reminders_for_food(user_id, calories)
    for row in triggered:
        await bot.send_message(
            chat_id,
            f"🔔 Нужно: {row['title']}",
            reply_markup=kb_reminder_notify(int(row["id"])),
        )


# Экран «Изменить данные профиля»: сводка + кнопки опроса/цели/ккал.
# Используется пунктом настроек и возвратами из подпунктов профиля.
async def show_profile(message: Message, state: FSMContext) -> None:
    await state.set_state(None)
    await state.update_data(menu_screen="profile")
    user_id = message.from_user.id if message.from_user else 0
    user = get_user(user_id)
    text = (
        "👤 Данные профиля\n"
        "\n"
        f"{format_profile_summary(user)}\n"
        "\n"
        "Выберите, что изменить:"
    )
    await replace_ui(message, state, text, reply_markup=kb_profile())


# Подменю выбора периода выгрузки.
# Используется настройкой «Сделать выгрузку журнала».
async def show_export_menu(message: Message, state: FSMContext) -> None:
    await state.set_state(None)
    await state.update_data(menu_screen="export")
    await replace_ui(
        message,
        state,
        "📤 Выгрузка журнала\n"
        "\n"
        "Это файл со всеми записями из дневника питания за выбранный "
        "период: блюда, ккал, БЖУ и точная дата со временем сохранения.\n"
        "\n"
        "Зачем это нужно: можно сохранить историю для себя или вставить "
        "целиком в ChatGPT / другую нейросеть. В начало файла уже "
        "добавлены ваши характеристики и короткий промпт — так проще "
        "получить разбор рациона и рекомендации по питанию.\n"
        "\n"
        "Выберите период — придёт файл .txt",
        reply_markup=kb_export(),
    )


# Нумерованный список блюд текущего просматриваемого дня для выбора.
# Используется флоу изменить/удалить.
def format_numbered_logs(
    user: dict[str, Any], logs: list[dict[str, Any]]
) -> str:
    lines: list[str] = []
    for i, row in enumerate(logs, start=1):
        t = format_log_time(row["created_at"], user["timezone"])
        emoji = format_log_emoji(row)
        lines.append(
            f"{i}. {t} — {emoji} {row['title']} ({row['calories']} ккал)"
        )
    return "\n".join(lines)


# Отправляет .txt выгрузки пользователю.
# Используется хендлерами периодов экспорта.
async def send_export_file(
    message: Message,
    user: dict[str, Any],
    logs: list[dict[str, Any]],
    period_title: str,
    filename: str,
) -> None:
    # Данные уже получены get_*; здесь только сборка файла.
    content = build_export_txt(user, logs, period_title)
    document = BufferedInputFile(content.encode("utf-8"), filename=filename)
    await message.answer_document(
        document,
        caption=f"Выгрузка: {period_title}\n\nВ начало документа добавлены ваши характеристики и простой промпт для запуска анализа - так весь документ можно вставить в ChatGPT и он даст рекомендации по улучшению питания",
    )
#endregion

#region Router меню и инфраструктура
menu_router = Router(name="main_menu")

storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# chat_id → monotonic time последнего STALE_RECOVERY_TEXT (антиспам при catch-up).
_stale_recovery_notified_at: dict[int, float] = {}


# True, если message.date старше STALE_MESSAGE_MAX_AGE_SEC (очередь после даунтайма).
# Используется DropStaleMessagesMiddleware.
def _is_stale_message(message: Message) -> bool:
    if message.date is None:
        return False
    age_sec = time.time() - message.date.timestamp()
    return age_sec > STALE_MESSAGE_MAX_AGE_SEC


# Один раз за cooldown пишет STALE_RECOVERY_TEXT в чат (пачка старых апдейтов).
# Используется DropStaleMessagesMiddleware при отбрасывании устаревшего сообщения.
async def _maybe_notify_stale_recovery(message: Message) -> None:
    chat = message.chat
    if chat is None:
        return
    chat_id = chat.id
    now = time.monotonic()
    last = _stale_recovery_notified_at.get(chat_id, 0.0)
    if now - last < STALE_RECOVERY_NOTIFY_COOLDOWN_SEC:
        return
    _stale_recovery_notified_at[chat_id] = now
    # Подчистка устаревших ключей, чтобы dict не рос бесконечно.
    cutoff = now - STALE_RECOVERY_NOTIFY_COOLDOWN_SEC
    stale_keys = [cid for cid, ts in _stale_recovery_notified_at.items() if ts < cutoff]
    for cid in stale_keys:
        if cid != chat_id:
            _stale_recovery_notified_at.pop(cid, None)
    try:
        await message.answer(STALE_RECOVERY_TEXT)
    except Exception as e:
        report_console_error(
            f"stale recovery notify failed chat_id={chat_id}: {e}",
            exc=e,
        )


# Outer middleware: не обрабатывает сообщения старше 10 мин; пишет «снова онлайн».
# Регистрируется на dp.message; ловит очередь Telegram после падения/рестарта бота.
class DropStaleMessagesMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message) and _is_stale_message(event):
            await _maybe_notify_stale_recovery(event)
            return None
        return await handler(event, data)


dp.message.outer_middleware(DropStaleMessagesMiddleware())
# Колбэк после подтверждения еды: INSERT food_logs → reminders.
# Передаётся в food_recognition.setup_food_recognition(on_food_saved=...).
async def _on_food_saved(
    user_id: int,
    result: Any,
    bot: Bot,
    chat_id: int,
) -> None:
    try:
        user = get_user(user_id)
        logged_date = logical_today(user)
        insert_food_log_from_result(user_id, result, logged_date)
    except Exception as e:
        is_ext = report_error_auto(
            f"food_logs insert failed user_id={user_id}: {e}",
            exc=e,
        )
        if is_ext:
            await notify_user_tech_issues(bot=bot, chat_id=chat_id)
        return
    calories = int(getattr(result, "calories", 0) or 0)
    await notify_reminders_after_food(user_id, calories, bot, chat_id)


# Колбэк после первичного опроса: текст про usage-reminder + параллельный upsert.
# Пока пользователь читает и жмёт «🟩 Хорошо», set_profile уже идёт в фоне.
# Передаётся в initial_survey.setup_initial_survey(on_complete=...).
async def _on_survey_complete(
    message: Message,
    state: FSMContext,
    profile: dict[str, Any],
) -> None:
    # Из callback.message.from_user — бот; опрос кладёт реальный id в profile["user_id"].
    user_id = int(
        profile.get("user_id")
        or (message.from_user.id if message.from_user else 0)
    )

    async def _save_profile() -> None:
        await asyncio.to_thread(
            set_profile,
            user_id,
            first_name=str(profile["first_name"]),
            gender=str(profile["gender"]),
            age=int(profile["age"]),
            height=float(profile["height"]),
            weight=float(profile["weight"]),
            activity_level=float(profile["activity_level"]),
            goal=str(profile["goal"]),
            timezone=str(profile["timezone"]),
            daily_calories=int(profile["daily_calories"]),
        )

    prev = _survey_profile_saves.pop(user_id, None)
    if prev is not None and not prev.done():
        prev.cancel()
    _survey_profile_saves[user_id] = asyncio.create_task(
        _save_profile(),
        name=f"survey-save-{user_id}",
    )
    await state.clear()
    await message.answer(
        SURVEY_USAGE_REMINDER_TEXT,
        reply_markup=kb_survey_usage_ok(),
    )


# После «🟩 Хорошо»: дождаться upsert профиля → «всё настроено» + Распознать.
# Используется inline-кнопкой под SURVEY_USAGE_REMINDER_TEXT.
async def _finish_survey_after_usage_ok(
    message: Message,
    state: FSMContext,
    *,
    user_id: int,
) -> None:
    task = _survey_profile_saves.get(user_id)
    wait_msg: Message | None = None

    if task is None:
        # Повторный клик / рестарт: профиль уже мог сохраниться в фоне.
        try:
            get_user(user_id)
        except UserNotRegisteredError:
            await message.answer(
                "Не удалось сохранить профиль. Нажмите /start и попробуйте ещё раз"
            )
            return
        await state.clear()
        await message.answer(
            "Отлично, всё настроено! ✅\n"
            "\n"
            "Теперь бот полностью готов к работе. Попробуй отправить фото "
            "или описание еды в чат прямо сейчас"
        )
        await show_recognize(message, state)
        return

    if not task.done():
        wait_msg = await message.answer("Секунду...")
    try:
        await task
    except Exception as e:
        _survey_profile_saves.pop(user_id, None)
        is_ext = report_error_auto(
            f"survey set_profile failed user_id={user_id}: {e}",
            exc=e,
        )
        fail_text = (
            TECH_ISSUES_USER_TEXT
            if is_ext
            else "Не удалось сохранить профиль. Нажмите /start и попробуйте ещё раз"
        )
        if wait_msg is not None:
            try:
                await wait_msg.edit_text(fail_text)
                return
            except Exception:
                pass
        await message.answer(fail_text)
        return
    finally:
        _survey_profile_saves.pop(user_id, None)

    await state.clear()
    done_text = (
        "Отлично, всё настроено! ✅\n"
        "\n"
        "Теперь бот полностью готов к работе. Попробуй отправить фото "
        "или описание еды в чат прямо сейчас"
    )
    if wait_msg is not None:
        try:
            await wait_msg.edit_text(done_text)
        except Exception:
            await message.answer(done_text)
    else:
        await message.answer(done_text)
    await show_recognize(message, state)


dp.include_router(menu_router)
dp.include_router(setup_initial_survey(on_complete=_on_survey_complete))
dp.include_router(
    setup_food_recognition(
        storage,
        menu_button_texts=MENU_BUTTON_TEXTS,
        main_menu_button_text=BTN_MAIN_MENU,
        on_food_saved=_on_food_saved,
    )
)
#endregion

#region Хендлеры: корень и навигация
# Маршрутизация как у /start: нет профиля → опрос, иначе главное меню.
# user_id нужен из callback (у callback.message.from_user — бот).
# Используется CommandStart и error-handler UserNotRegisteredError.
async def dispatch_start(
    message: Message, state: FSMContext, *, user_id: int | None = None
) -> None:
    uid = user_id if user_id is not None else (
        message.from_user.id if message.from_user else 0
    )
    if INITIAL_SURVEY_ENABLED:
        await start_initial_survey(message, state)
        return
    try:
        get_user(uid)
    except UserNotRegisteredError:
        await start_initial_survey(message, state)
        return
    await state.clear()
    await show_main_menu(message, state, user_id=uid)


# /start — нет профиля в NocoDB → опрос; иначе главное меню.
# Регистрируется на dp (не на menu_router), чтобы всегда быть доступным.
# INITIAL_SURVEY_ENABLED=True принудительно всегда открывает опрос (отладка UI).
@dp.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await dispatch_start(message, state)


# Нет записи в users (кнопка/сообщение до регистрации) → та же ветка, что /start.
# Используется глобально для menu / food_recognition / любых get_user.
@dp.error(ExceptionTypeFilter(UserNotRegisteredError))
async def on_user_not_registered(event: ErrorEvent, state: FSMContext) -> bool:
    update = event.update
    exc = event.exception
    fallback_uid = exc.user_id if isinstance(exc, UserNotRegisteredError) else 0
    if update.callback_query is not None:
        cq = update.callback_query
        try:
            await cq.answer()
        except Exception:
            pass
        if cq.message is not None:
            uid = cq.from_user.id if cq.from_user else fallback_uid
            await dispatch_start(cq.message, state, user_id=uid)
        return True
    if update.message is not None:
        msg = update.message
        uid = msg.from_user.id if msg.from_user else fallback_uid
        await dispatch_start(msg, state, user_id=uid)
        return True
    return True


# Необработанные ошибки update: внешние сервисы → чат + 🟧🍎; иначе 🟨⬛🍎.
# UserNotRegisteredError обрабатывается отдельным хендлером выше.
@dp.error()
async def on_unhandled_update_error(event: ErrorEvent) -> bool:
    exc = event.exception
    if isinstance(exc, UserNotRegisteredError):
        return False
    base_exc = exc if isinstance(exc, BaseException) else None
    ctx = f"Unhandled update error: {type(exc).__name__}: {exc}"
    if is_external_service_error(base_exc):
        report_service_problem(ctx, exc=base_exc)
        await notify_user_tech_issues_from_event(event)
    else:
        report_console_error(ctx, exc=base_exc)
    return True


# Возврат в главное меню из любого раздела по кнопке 🏠.
@menu_router.message(F.text == BTN_MAIN_MENU)
async def on_main_menu(message: Message, state: FSMContext) -> None:
    await show_main_menu(message, state)


# Открыть дневник питания (offset сбрасывается в 0).
@menu_router.message(F.text == BTN_DIARY)
async def on_diary(message: Message, state: FSMContext) -> None:
    await state.update_data(diary_offset=0)
    await show_diary(message, state)


# Экран-памятка «Распознать».
@menu_router.message(F.text == BTN_RECOGNIZE)
async def on_recognize(message: Message, state: FSMContext) -> None:
    await show_recognize(message, state)


# Открыть настройки.
@menu_router.message(F.text == BTN_SETTINGS)
async def on_settings(message: Message, state: FSMContext) -> None:
    await show_settings(message, state)


# «Назад»: из подменю — на уровень выше; из корня разделов — в главное меню.
@menu_router.message(F.text == BTN_BACK)
async def on_back(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    data = await state.get_data()
    export_return = data.get("export_return", "main")

    if current == MenuFlow.export_month_pick.state:
        await show_export_menu(message, state)
        return
    if current in (
        MenuFlow.diary_pick_edit.state,
        MenuFlow.diary_pick_delete.state,
    ):
        await show_diary(message, state)
        return
    if current in (
        MenuFlow.settings_calories.state,
        MenuFlow.settings_goal.state,
        MenuFlow.settings_goal_recalc.state,
    ):
        await show_profile(message, state)
        return
    if current == MenuFlow.settings_day_hour.state:
        await show_settings(message, state)
        return
    if current == MenuFlow.feedback_wait.state:
        await show_feedback_menu(message, state)
        return
    if current == MenuFlow.reminders_add_window.state:
        await state.set_state(MenuFlow.reminders_add_title)
        await state.update_data(menu_screen="reminders")
        await replace_ui(
            message,
            state,
            "➕ Добавить напоминание\n"
            "\n"
            "Введите название — например, «Выпить Омега-3» или «Витамин D»",
            reply_markup=kb_nav_only(),
        )
        return
    if current == MenuFlow.reminders_add_min_cal.state:
        await state.set_state(MenuFlow.reminders_add_window)
        await state.update_data(menu_screen="reminders")
        await replace_ui(
            message,
            state,
            "➕ Добавить напоминание\n"
            "\n"
            "Выберите окно времени — напоминание сработает "
            "при первой подходящей еде в этом интервале:",
            reply_markup=kb_reminder_windows(),
        )
        return
    if current == MenuFlow.reminders_delete_confirm.state:
        # Назад с подтверждения удаления → снова карточка напоминания.
        rem_id = int(data.get("rem_edit_id") or 0)
        user_id = message.from_user.id if message.from_user else 0
        row = get_reminder(user_id, rem_id)
        if row is None:
            await show_reminders(message, state)
            return
        await state.set_state(MenuFlow.reminders_item_action)
        await state.update_data(menu_screen="reminders")
        await replace_ui(
            message,
            state,
            format_reminder_card(row),
            reply_markup=kb_reminder_item(),
        )
        return
    if current in (
        MenuFlow.reminders_add_title.state,
        MenuFlow.reminders_list_pick.state,
        MenuFlow.reminders_item_action.state,
    ):
        await show_reminders(message, state)
        return

    # menu_screen в FSM: из выгрузки — назад в diary/settings; из разделов — в корень.
    screen = data.get("menu_screen", "main")
    if screen == "export":
        if export_return == "settings":
            await show_settings(message, state)
        elif export_return == "diary":
            await show_diary(message, state)
        else:
            await show_main_menu(message, state)
        return
    if screen == "profile_confirm":
        await show_profile(message, state)
        return
    if screen == "profile_goal_recalc":
        await show_profile(message, state)
        return
    if screen == "profile":
        await show_settings(message, state)
        return
    if screen == "reminders":
        await show_settings(message, state)
        return
    if screen == "usage_reminder":
        await show_reminders(message, state)
        return
    if screen == "feedback":
        await show_settings(message, state)
        return
    if screen == "help":
        await show_settings(message, state)
        return
    await show_main_menu(message, state)
#endregion

#region Хендлеры: дневник
# Inline: день раньше — правим карточку дневника на месте.
@menu_router.callback_query(F.data == CALLBACK_DIARY_PREV)
async def on_diary_prev(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    offset = int(data.get("diary_offset", 0)) - 1
    await state.update_data(diary_offset=offset, menu_screen="diary")
    await callback.answer()
    if callback.message:
        await show_diary(
            callback.message,
            state,
            user_id=callback.from_user.id,
            edit_message=callback.message,
        )


# Inline: день позже (только из прошлого; на «сегодня» кнопки нет).
@menu_router.callback_query(F.data == CALLBACK_DIARY_NEXT)
async def on_diary_next(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    offset = int(data.get("diary_offset", 0))
    if offset >= 0:
        await callback.answer()
        return
    offset += 1
    await state.update_data(diary_offset=offset, menu_screen="diary")
    await callback.answer()
    if callback.message:
        await show_diary(
            callback.message,
            state,
            user_id=callback.from_user.id,
            edit_message=callback.message,
        )


# Памятка «Добавить блюдо» — анализ без кнопки.
@menu_router.message(F.text == BTN_ADD_DISH)
async def on_add_dish(message: Message, state: FSMContext) -> None:
    await state.update_data(menu_screen="diary")
    await state.set_state(None)
    await replace_ui(
        message,
        state,
        "➕ Добавить блюдо\n"
        "\n"
        "Просто пришлите фото блюда (можно с подписью) или напишите текстом, "
        "что съели — бот оценит ккал и БЖУ",
        reply_markup=kb_diary(),
    )


# Старт «Изменить блюдо»: reply-кнопки с номерами → ожидание выбора.
@menu_router.message(F.text == BTN_EDIT_DISH)
async def on_edit_dish(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id if message.from_user else 0
    user = get_user(user_id)
    data = await state.get_data()
    offset = int(data.get("diary_offset", 0))
    logged_date = logical_date_with_offset(user, offset)
    logs = get_food_logs_for_date(user_id, logged_date)
    await state.update_data(
        menu_screen="diary",
        pick_logs=[r["id"] for r in logs],
        pick_log_rows=logs,
        pick_page=0,
    )
    if not logs:
        await replace_ui(
            message,
            state,
            "За этот день записей нет — менять нечего",
            reply_markup=kb_diary(),
        )
        return
    await state.set_state(MenuFlow.diary_pick_edit)
    await replace_ui(
        message,
        state,
        format_dish_pick_prompt(mode="edit", user=user, logs=logs, page=0),
        reply_markup=kb_pick_dish(len(logs), page=0),
    )


# 🎈 Выбор номера для изменения → заглушка (без формы полей / UPDATE).
@menu_router.message(MenuFlow.diary_pick_edit, F.text, ~F.text.in_(MENU_BUTTON_TEXTS))
async def on_edit_dish_pick(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    data = await state.get_data()
    pick_ids: list[int] = list(data.get("pick_logs") or [])
    if not text.isdigit():
        await message.answer("Выберите номер блюда кнопкой на клавиатуре")
        return
    idx = int(text)
    if idx < 1 or idx > len(pick_ids):
        await message.answer(f"Нужен номер от 1 до {len(pick_ids)}")
        return
    log_id = pick_ids[idx - 1]
    await state.set_state(None)
    # 🎈 UPDATE food_logs ... — форма редактирования появится позже
    await replace_ui(
        message,
        state,
        f"Выбрано блюдо #{idx} (id={log_id}).\n"
        "✏️ Сохранение изменений в БД скоро — форма редактирования появится позже",
        reply_markup=kb_diary(),
    )


# Старт «Удалить блюдо»: reply-кнопки с номерами → ожидание выбора.
@menu_router.message(F.text == BTN_DELETE_DISH)
async def on_delete_dish(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id if message.from_user else 0
    user = get_user(user_id)
    data = await state.get_data()
    offset = int(data.get("diary_offset", 0))
    logged_date = logical_date_with_offset(user, offset)
    logs = get_food_logs_for_date(user_id, logged_date)
    await state.update_data(
        menu_screen="diary",
        pick_logs=[r["id"] for r in logs],
        pick_log_rows=logs,
        pick_page=0,
    )
    if not logs:
        await replace_ui(
            message,
            state,
            "За этот день записей нет — удалять нечего",
            reply_markup=kb_diary(),
        )
        return
    await state.set_state(MenuFlow.diary_pick_delete)
    await replace_ui(
        message,
        state,
        format_dish_pick_prompt(mode="delete", user=user, logs=logs, page=0),
        reply_markup=kb_pick_dish(len(logs), page=0),
    )


# Выбор номера для удаления → delete food_logs + обновление дневника.
@menu_router.message(MenuFlow.diary_pick_delete, F.text, ~F.text.in_(MENU_BUTTON_TEXTS))
async def on_delete_dish_pick(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    data = await state.get_data()
    pick_ids: list[int] = list(data.get("pick_logs") or [])
    if not text.isdigit():
        await message.answer("Выберите номер блюда кнопкой на клавиатуре")
        return
    idx = int(text)
    if idx < 1 or idx > len(pick_ids):
        await message.answer(f"Нужен номер от 1 до {len(pick_ids)}")
        return
    log_id = pick_ids[idx - 1]
    user_id = message.from_user.id if message.from_user else 0
    # id из своего FSM pick_logs — без лишнего GET владельца.
    delete_food_log(user_id, log_id, check_owner=False)
    await state.set_state(None)
    await show_diary(message, state)


# Перелистывание страницы номеров блюд (Далее / Ранее) в флоу изменить/удалить.
@menu_router.message(
    MenuFlow.diary_pick_edit,
    F.text.in_({BTN_PICK_PAGE_NEXT, BTN_PICK_PAGE_PREV}),
)
@menu_router.message(
    MenuFlow.diary_pick_delete,
    F.text.in_({BTN_PICK_PAGE_NEXT, BTN_PICK_PAGE_PREV}),
)
async def on_dish_pick_page(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    pick_ids: list[int] = list(data.get("pick_logs") or [])
    total = len(pick_ids)
    if total <= 0:
        await show_diary(message, state)
        return
    max_page = max(0, (total - 1) // PICK_PAGE_SIZE)
    page = int(data.get("pick_page", 0))
    if message.text == BTN_PICK_PAGE_NEXT:
        page = min(max_page, page + 1)
    else:
        page = max(0, page - 1)
    await state.update_data(pick_page=page)

    user_id = message.from_user.id if message.from_user else 0
    user = get_user(user_id)
    cached_rows: list[dict[str, Any]] = list(data.get("pick_log_rows") or [])
    if cached_rows:
        id_to_log = {r["id"]: r for r in cached_rows}
        ordered = [id_to_log[i] for i in pick_ids if i in id_to_log]
    else:
        offset = int(data.get("diary_offset", 0))
        logged_date = logical_date_with_offset(user, offset)
        logs = get_food_logs_for_date(user_id, logged_date)
        id_to_log = {r["id"]: r for r in logs}
        ordered = [id_to_log[i] for i in pick_ids if i in id_to_log]
        await state.update_data(pick_log_rows=logs)
    # Сохраняем порядок/состав pick_logs; для текста берём записи из FSM-кэша.
    if len(ordered) != total:
        ordered = cached_rows if cached_rows else ordered
        await state.update_data(pick_logs=[r["id"] for r in ordered])
        total = len(ordered)

    current = await state.get_state()
    mode = "edit" if current == MenuFlow.diary_pick_edit.state else "delete"
    await replace_ui(
        message,
        state,
        format_dish_pick_prompt(mode=mode, user=user, logs=ordered, page=page),
        reply_markup=kb_pick_dish(total, page=page),
    )


#endregion

#region Хендлеры: выгрузка
# Выгрузка за текущий логический день.
@menu_router.message(F.text == BTN_EXPORT_TODAY)
async def on_export_today(message: Message, state: FSMContext) -> None:
    await state.update_data(menu_screen="export")
    user_id = message.from_user.id if message.from_user else 0
    user = get_user(user_id)
    day = logical_today(user)
    logs = get_food_logs_for_date(user_id, day)
    await send_export_file(
        message, user, logs, f"текущий день ({day})", f"diary_{day}.txt"
    )


# Выгрузка за прошлый логический день.
@menu_router.message(F.text == BTN_EXPORT_YESTERDAY)
async def on_export_yesterday(message: Message, state: FSMContext) -> None:
    await state.update_data(menu_screen="export")
    user_id = message.from_user.id if message.from_user else 0
    user = get_user(user_id)
    day = logical_date_with_offset(user, -1)
    logs = get_food_logs_for_date(user_id, day)
    await send_export_file(
        message, user, logs, f"прошлый день ({day})", f"diary_{day}.txt"
    )


# Выгрузка за 7 логических дней (включая сегодня).
@menu_router.message(F.text == BTN_EXPORT_WEEK)
async def on_export_week(message: Message, state: FSMContext) -> None:
    await state.update_data(menu_screen="export")
    user_id = message.from_user.id if message.from_user else 0
    user = get_user(user_id)
    date_to = logical_today(user)
    date_from = logical_date_with_offset(user, -6)
    logs = get_food_logs_range(user_id, date_from, date_to)
    await send_export_file(
        message,
        user,
        logs,
        f"прошедшая неделя ({date_from} … {date_to})",
        f"diary_week_{date_from}_{date_to}.txt",
    )


# Выбор окна «месяц» (три диапазона по 30 дней в пределах 90).
@menu_router.message(F.text == BTN_EXPORT_MONTH)
async def on_export_month(message: Message, state: FSMContext) -> None:
    await state.set_state(MenuFlow.export_month_pick)
    await state.update_data(menu_screen="export_month")
    await replace_ui(
        message,
        state,
        "🗂 Выгрузка за 30 дней\n"
        "\n"
        "Данные хранятся не дольше 90 дней. Какой период нужен?",
        reply_markup=kb_export_month(),
    )


# Выгрузка одного из окон 0–30 / 30–60 / 60–90.
@menu_router.message(MenuFlow.export_month_pick, F.text.in_({
    BTN_MONTH_0_30, BTN_MONTH_30_60, BTN_MONTH_60_90,
}))
async def on_export_month_pick(message: Message, state: FSMContext) -> None:
    text = message.text or ""
    user_id = message.from_user.id if message.from_user else 0
    user = get_user(user_id)
    if text == BTN_MONTH_0_30:
        start_off, end_off = -29, 0
        title = "последние 30 дней"
    elif text == BTN_MONTH_30_60:
        start_off, end_off = -59, -30
        title = "от 30 до 60 дней назад"
    else:
        start_off, end_off = -89, -60
        title = "от 60 до 90 дней назад"
    date_from = logical_date_with_offset(user, start_off)
    date_to = logical_date_with_offset(user, end_off)
    logs = get_food_logs_range(user_id, date_from, date_to)
    await state.set_state(None)
    await state.update_data(menu_screen="export")
    await send_export_file(
        message,
        user,
        logs,
        f"{title} ({date_from} … {date_to})",
        f"diary_month_{date_from}_{date_to}.txt",
    )
    await replace_ui(message, state, "Готово", reply_markup=kb_export())
#endregion

#region Хендлеры: настройки
# Открыть экран данных профиля (сводка + цель/ккал/опрос).
@menu_router.message(F.text == BTN_SET_PROFILE)
async def on_set_profile(message: Message, state: FSMContext) -> None:
    await show_profile(message, state)


# Предупреждение перед перезапуском первоначального опроса.
@menu_router.message(F.text == BTN_UPDATE_PROFILE)
async def on_update_profile_ask(message: Message, state: FSMContext) -> None:
    await state.set_state(None)
    await state.update_data(menu_screen="profile_confirm")
    await replace_ui(
        message,
        state,
        "🔄 Обновить данные пользователя\n"
        "\n"
        "Эта кнопка запустит заново первоначальный опрос. "
        "В нём вы сможете поменять рост, вес, пол и другие характеристики, "
        "если они изменились.\n"
        "\n"
        "После прохождения опроса мы автоматически рассчитаем новые значения "
        "ккал на сутки.\n"
        "\n"
        "Продолжить?",
        reply_markup=kb_confirm_update_profile(),
    )


# 🎈 Согласие на перезапуск опроса → заглушка (онбординг подключим позже).
@menu_router.message(F.text == BTN_CONFIRM_UPDATE_YES)
async def on_update_profile_yes(message: Message, state: FSMContext) -> None:
    await state.set_state(None)
    await state.update_data(menu_screen="profile")
    await replace_ui(
        message,
        state,
        "🔜 Перекидываем вас на первоначальный опрос…\n"
        "\n"
        "🎈 Заглушка: сам опрос подключим позже. "
        "Сейчас вы остаётесь в разделе данных профиля",
        reply_markup=kb_profile(),
    )


# Отказ от перезапуска опроса → назад к сводке профиля.
@menu_router.message(F.text == BTN_CONFIRM_UPDATE_NO)
async def on_update_profile_no(message: Message, state: FSMContext) -> None:
    await show_profile(message, state)


# Смена часа перехода суток — запрос ввода.
@menu_router.message(F.text == BTN_SET_DAY_HOUR)
async def on_set_day_hour(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id if message.from_user else 0
    user = get_user(user_id)
    await state.set_state(MenuFlow.settings_day_hour)
    await state.update_data(menu_screen="settings")
    await replace_ui(
        message,
        state,
        "🕓 Время смены суток\n"
        "\n"
        "В боте новый день начинается не в 00:00, а в выбранный час. "
        "Всё, что вы съели до этого времени, относится к предыдущему дню "
        "(удобно, если ужинаете или перекусываете после полуночи).\n"
        "\n"
        f"Сейчас: {user['day_change_hour']:02d}:00 "
        f"(часовой пояс {user['timezone']}).\n"
        "Введите час от 0 до 23 (например, 4)",
        reply_markup=kb_nav_only(),
    )


# Сохранение day_change_hour в NocoDB.
@menu_router.message(MenuFlow.settings_day_hour, F.text, ~F.text.in_(MENU_BUTTON_TEXTS))
async def on_set_day_hour_value(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit() or not (0 <= int(text) <= 23):
        await message.answer("Введите целое число часа от 0 до 23")
        return
    user_id = message.from_user.id if message.from_user else 0
    set_day_change_hour(user_id, int(text))
    await state.set_state(None)
    await replace_ui(
        message,
        state,
        f"✅ Смена суток установлена на {int(text):02d}:00",
        reply_markup=kb_settings(),
    )


# Выгрузка из настроек.
@menu_router.message(F.text == BTN_SET_EXPORT)
async def on_settings_export(message: Message, state: FSMContext) -> None:
    await state.update_data(menu_screen="export", export_return="settings")
    await show_export_menu(message, state)


# Открыть FAQ с обзором возможностей бота.
@menu_router.message(F.text == BTN_SET_HELP)
async def on_settings_help(message: Message, state: FSMContext) -> None:
    await show_help(message, state)


# Вернуться к обзору FAQ из любой темы.
@menu_router.message(F.text == BTN_HELP_OVERVIEW)
async def on_help_overview(message: Message, state: FSMContext) -> None:
    await show_help(message, state)


# Тема FAQ: распознавание еды.
@menu_router.message(F.text == BTN_HELP_RECOGNIZE)
async def on_help_recognize(message: Message, state: FSMContext) -> None:
    await show_help_topic(message, state, BTN_HELP_RECOGNIZE)


# Тема FAQ: дневник питания.
@menu_router.message(F.text == BTN_HELP_DIARY)
async def on_help_diary(message: Message, state: FSMContext) -> None:
    await show_help_topic(message, state, BTN_HELP_DIARY)


# Тема FAQ: напоминания и витамины.
@menu_router.message(F.text == BTN_HELP_REMINDERS)
async def on_help_reminders(message: Message, state: FSMContext) -> None:
    await show_help_topic(message, state, BTN_HELP_REMINDERS)


# Тема FAQ: выгрузка журнала.
@menu_router.message(F.text == BTN_HELP_EXPORT)
async def on_help_export(message: Message, state: FSMContext) -> None:
    await show_help_topic(message, state, BTN_HELP_EXPORT)


# Тема FAQ: смена суток.
@menu_router.message(F.text == BTN_HELP_DAY_HOUR)
async def on_help_day_hour(message: Message, state: FSMContext) -> None:
    await show_help_topic(message, state, BTN_HELP_DAY_HOUR)


# Открыть подменю выбора типа обратной связи.
@menu_router.message(F.text == BTN_SET_FEEDBACK)
async def on_feedback_start(message: Message, state: FSMContext) -> None:
    await show_feedback_menu(message, state)


# Старт ввода: багрепорт — ждём текст и/или один скриншот.
@menu_router.message(F.text == BTN_FEEDBACK_BUG)
async def on_feedback_bug(message: Message, state: FSMContext) -> None:
    await state.set_state(MenuFlow.feedback_wait)
    await state.update_data(menu_screen="feedback", feedback_kind="bug")
    await replace_ui(
        message,
        state,
        "🐞 Сообщить об ошибке\n"
        "\n"
        "Опишите проблему текстом. Можно прикрепить один скриншот "
        "(удобнее всего — фото с подписью).\n",
        reply_markup=kb_nav_only(),
    )


# Старт ввода: предложение по улучшению — ждём текст и/или один скриншот.
@menu_router.message(F.text == BTN_FEEDBACK_IDEA)
async def on_feedback_idea(message: Message, state: FSMContext) -> None:
    await state.set_state(MenuFlow.feedback_wait)
    await state.update_data(menu_screen="feedback", feedback_kind="idea")
    await replace_ui(
        message,
        state,
        "💡 Предложение по улучшению функционала\n"
        "\n"
        "Напишите идею текстом. Можно прикрепить один скриншот "
        "(удобнее всего — фото с подписью).\n",
        reply_markup=kb_nav_only(),
    )


# Приём текстового отзыва → письмо на FEEDBACK_TO_EMAIL.
@menu_router.message(MenuFlow.feedback_wait, F.text, ~F.text.in_(MENU_BUTTON_TEXTS))
async def on_feedback_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    kind = str(data.get("feedback_kind") or "idea")
    user_id = message.from_user.id if message.from_user else 0
    username = message.from_user.username if message.from_user else None
    text = (message.text or "").strip()
    try:
        await send_feedback_email(
            user_id=user_id,
            username=username,
            kind=kind,
            text=text,
        )
    except Exception as exc:
        report_console_error(f"feedback email error: {exc}", exc=exc)
        await message.answer(
            "Не удалось отправить сообщение. Попробуйте позже "
            "или напишите разработчику напрямую"
        )
        return
    await state.set_state(None)
    await state.update_data(menu_screen="settings", feedback_kind=None)
    await replace_ui(
        message,
        state,
        "✅ Спасибо! Сообщение отправлено разработчику",
        reply_markup=kb_settings(),
    )


# Приём отзыва с одним скриншотом (подпись опциональна) → письмо.
@menu_router.message(MenuFlow.feedback_wait, F.photo)
async def on_feedback_photo(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    kind = str(data.get("feedback_kind") or "idea")
    user_id = message.from_user.id if message.from_user else 0
    username = message.from_user.username if message.from_user else None
    text = (message.caption or "").strip()
    photo = message.photo[-1]
    try:
        photo_bytes = await download_photo_bytes(message.bot, photo.file_id)
        await send_feedback_email(
            user_id=user_id,
            username=username,
            kind=kind,
            text=text,
            photo_bytes=photo_bytes,
        )
    except Exception as exc:
        report_console_error(f"feedback email error: {exc}", exc=exc)
        await message.answer(
            "Не удалось отправить сообщение. Попробуйте позже "
            "или напишите разработчику напрямую"
        )
        return
    await state.set_state(None)
    await state.update_data(menu_screen="settings", feedback_kind=None)
    await replace_ui(
        message,
        state,
        "✅ Спасибо! Сообщение отправлено разработчику",
        reply_markup=kb_settings(),
    )


# Выбор типа отслеживания — показать текущий тип и кнопки целей (из экрана профиля).
@menu_router.message(F.text == BTN_SET_GOAL)
async def on_set_goal(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id if message.from_user else 0
    user = get_user(user_id)
    await state.set_state(MenuFlow.settings_goal)
    await state.update_data(menu_screen="profile")
    await replace_ui(
        message,
        state,
        "🎯 Тип отслеживания\n"
        "\n"
        f"Сейчас: {goal_label(user.get('goal', ''))}.\n"
        "\n"
        "Выберите направление:",
        reply_markup=kb_goal(),
    )


# Сохранение goal в NocoDB. Если тип изменился — спросить про пересчёт целевых ккал.
@menu_router.message(MenuFlow.settings_goal, F.text.in_(set(GOAL_BY_BTN)))
async def on_set_goal_value(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id if message.from_user else 0
    user = get_user(user_id)
    old_goal = user.get("goal", "")
    new_goal = GOAL_BY_BTN[message.text or ""]

    if new_goal == old_goal:
        await show_profile(message, state)
        return

    set_goal(user_id, new_goal)
    await state.set_state(MenuFlow.settings_goal_recalc)
    await state.update_data(menu_screen="profile_goal_recalc")
    await replace_ui(
        message,
        state,
        "✅ Данные успешно обновлены.\n"
        "\n"
        f"Тип отслеживания: {goal_label(old_goal)} → {goal_label(new_goal)}.\n"
        "\n"
        "Пересчитать целевое количество ккал в сутки под новый тип отслеживания?",
        reply_markup=kb_confirm_recalc_calories(),
    )


# 🎈 Согласие на пересчёт ккал после смены типа → заглушка (формулу подключим позже).
@menu_router.message(MenuFlow.settings_goal_recalc, F.text == BTN_CONFIRM_RECALC_YES)
async def on_goal_recalc_yes(message: Message, state: FSMContext) -> None:
    await state.set_state(None)
    await state.update_data(menu_screen="profile")
    await replace_ui(
        message,
        state,
        "🔜 Пересчёт целевых ккал…\n"
        "\n"
        "🎈 Заглушка: формулу пересчёта подключим позже. "
        "Сейчас вы остаётесь в разделе данных профиля",
        reply_markup=kb_profile(),
    )


# Отказ от пересчёта ккал после смены типа → назад к сводке профиля.
@menu_router.message(MenuFlow.settings_goal_recalc, F.text == BTN_CONFIRM_RECALC_NO)
async def on_goal_recalc_no(message: Message, state: FSMContext) -> None:
    await show_profile(message, state)


# Запрос целевых ккал (из экрана профиля).
@menu_router.message(F.text == BTN_SET_CALORIES)
async def on_set_calories(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id if message.from_user else 0
    user = get_user(user_id)
    await state.set_state(MenuFlow.settings_calories)
    await state.update_data(menu_screen="profile")
    await replace_ui(
        message,
        state,
        "🔥 Целевые ккал в сутки\n"
        "\n"
        f"Сейчас: {user['daily_calories']} ккал.\n"
        "Введите новое целое число (например, 2000)",
        reply_markup=kb_nav_only(),
    )


# Сохранение daily_calories в NocoDB → обновлённая сводка профиля.
@menu_router.message(MenuFlow.settings_calories, F.text, ~F.text.in_(MENU_BUTTON_TEXTS))
async def on_set_calories_value(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit() or int(text) <= 0:
        await message.answer("Введите целое положительное число ккал")
        return
    user_id = message.from_user.id if message.from_user else 0
    set_daily_calories(user_id, int(text))
    await show_profile(message, state)
#endregion

#region Хендлеры: напоминания
# Открыть раздел «Напоминания и Витамины».
@menu_router.message(F.text == BTN_SET_REMINDERS)
async def on_set_reminders(message: Message, state: FSMContext) -> None:
    touch_user_activity(message.from_user.id if message.from_user else 0)
    await show_reminders(message, state)


# Экран настройки напоминания «нет еды до 13:00».
@menu_router.message(F.text == BTN_REM_USAGE)
async def on_rem_usage_open(message: Message, state: FSMContext) -> None:
    await show_usage_reminder_settings(message, state)


# Вкл/выкл usage-reminder. StateFilter(None) — не перехватывать
# BTN_REM_TOGGLE на карточке обычного напоминания (reminders_item_action).
@menu_router.message(StateFilter(None), F.text == BTN_REM_TOGGLE)
async def on_usage_reminder_toggle(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if data.get("menu_screen") != "usage_reminder":
        return
    user_id = message.from_user.id if message.from_user else 0
    user = get_user(user_id)
    new_enabled = not bool(user.get("usage_reminder_enabled", True))
    try:
        await asyncio.to_thread(set_usage_reminder_enabled, user_id, new_enabled)
    except Exception as e:
        is_ext = report_error_auto(
            f"set_usage_reminder_enabled failed user_id={user_id}: {e}",
            exc=e,
        )
        await message.answer(
            TECH_ISSUES_USER_TEXT if is_ext else "Не удалось сохранить настройку"
        )
        return
    await show_usage_reminder_settings(message, state)


# Старт добавления: запрос названия.
@menu_router.message(F.text == BTN_REM_ADD)
async def on_rem_add_start(message: Message, state: FSMContext) -> None:
    await state.set_state(MenuFlow.reminders_add_title)
    await state.update_data(menu_screen="reminders", rem_draft={})
    await replace_ui(
        message,
        state,
        "➕ Добавить напоминание\n"
        "\n"
        "Введите название — например, «Выпить Омега-3» или «Витамин D»",
        reply_markup=kb_nav_only(),
    )


# Шаг 1: название → выбор окна.
@menu_router.message(
    MenuFlow.reminders_add_title, F.text, ~F.text.in_(MENU_BUTTON_TEXTS)
)
async def on_rem_add_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if not title:
        await message.answer("Название не может быть пустым — введите текст")
        return
    if len(title) > 255:
        await message.answer("Слишком длинное название — до 255 символов")
        return
    await state.update_data(rem_draft={"title": title})
    await state.set_state(MenuFlow.reminders_add_window)
    await replace_ui(
        message,
        state,
        "➕ Добавить напоминание\n"
        "\n"
        f"Название: {title}\n"
        "\n"
        "Выберите окно времени — напоминание сработает "
        "при первой подходящей еде в этом интервале:",
        reply_markup=kb_reminder_windows(),
    )


# Шаг 2: окно → порог калорий.
@menu_router.message(
    MenuFlow.reminders_add_window, F.text.in_(set(REMINDER_WINDOWS))
)
async def on_rem_add_window(message: Message, state: FSMContext) -> None:
    window = REMINDER_WINDOWS[message.text or ""]
    data = await state.get_data()
    draft = dict(data.get("rem_draft") or {})
    draft["time_start"] = window[0]
    draft["time_end"] = window[1]
    await state.update_data(rem_draft=draft)
    await state.set_state(MenuFlow.reminders_add_min_cal)
    await replace_ui(
        message,
        state,
        "➕ Добавить напоминание\n"
        "\n"
        f"Название: {draft.get('title', '')}\n"
        f"Окно: {window[0]}–{window[1]}\n"
        "\n"
        "Реагировать на:",
        reply_markup=kb_reminder_min_cal(),
    )


# Шаг 3: порог → INSERT reminder в NocoDB.
@menu_router.message(
    MenuFlow.reminders_add_min_cal,
    F.text.in_({BTN_REM_ANY_FOOD, BTN_REM_HEARTY}),
)
async def on_rem_add_min_cal(message: Message, state: FSMContext) -> None:
    min_cal = 0 if message.text == BTN_REM_ANY_FOOD else REMINDER_HEARTY_MIN_KCAL
    data = await state.get_data()
    draft = dict(data.get("rem_draft") or {})
    title = str(draft.get("title") or "").strip()
    time_start = str(draft.get("time_start") or "")
    time_end = str(draft.get("time_end") or "")
    if not title or not time_start or not time_end:
        await show_reminders(message, state)
        return
    user_id = message.from_user.id if message.from_user else 0
    row = add_reminder(user_id, title, time_start, time_end, min_cal)
    await state.set_state(None)
    await state.update_data(rem_draft={}, menu_screen="reminders")
    await replace_ui(
        message,
        state,
        "✅ Напоминание добавлено\n"
        "\n"
        f"{format_reminder_card(row)}\n"
        "\n"
        "Оно сработает при следующей подходящей еде в этом окне",
        reply_markup=kb_reminders(),
    )


# Список напоминаний → выбор номера.
@menu_router.message(F.text == BTN_REM_LIST)
async def on_rem_list(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id if message.from_user else 0
    rows = get_reminders(user_id)
    if not rows:
        await replace_ui(
            message,
            state,
            "📋 Мои напоминания\n"
            "\n"
            "Пока пусто — добавьте первое через «➕ Добавить напоминание»",
            reply_markup=kb_reminders(),
        )
        await state.set_state(None)
        await state.update_data(menu_screen="reminders")
        return
    await state.set_state(MenuFlow.reminders_list_pick)
    await state.update_data(
        menu_screen="reminders",
        rem_pick_ids=[r["id"] for r in rows],
        rem_rows=rows,
        pick_page=0,
    )
    await replace_ui(
        message,
        state,
        "📋 Мои напоминания\n"
        "\n"
        f"{format_reminders_list(rows)}\n"
        "\n"
        "Выберите номер, чтобы отключить или удалить:",
        reply_markup=kb_pick_dish(len(rows), page=0),
    )


# Пагинация списка напоминаний (▶️ / ◀️), как у выбора блюд.
@menu_router.message(
    MenuFlow.reminders_list_pick,
    F.text.in_({BTN_PICK_PAGE_NEXT, BTN_PICK_PAGE_PREV}),
)
async def on_rem_list_page(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    pick_ids: list[int] = list(data.get("rem_pick_ids") or [])
    page = int(data.get("pick_page", 0))
    total = len(pick_ids)
    max_page = max(0, (total - 1) // PICK_PAGE_SIZE)
    if message.text == BTN_PICK_PAGE_NEXT and page < max_page:
        page += 1
    elif message.text == BTN_PICK_PAGE_PREV and page > 0:
        page -= 1
    await state.update_data(pick_page=page)
    rows: list[dict[str, Any]] = list(data.get("rem_rows") or [])
    if not rows:
        user_id = message.from_user.id if message.from_user else 0
        rows = get_reminders(user_id)
        await state.update_data(rem_rows=rows)
    await replace_ui(
        message,
        state,
        "📋 Мои напоминания\n"
        "\n"
        f"{format_reminders_list(rows)}\n"
        "\n"
        "Выберите номер, чтобы отключить или удалить:",
        reply_markup=kb_pick_dish(total, page=page),
    )


# Выбор номера напоминания → карточка с действиями.
@menu_router.message(
    MenuFlow.reminders_list_pick, F.text, ~F.text.in_(MENU_BUTTON_TEXTS)
)
async def on_rem_list_pick(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    data = await state.get_data()
    pick_ids: list[int] = list(data.get("rem_pick_ids") or [])
    if not text.isdigit():
        await message.answer("Выберите номер напоминания кнопкой на клавиатуре")
        return
    idx = int(text)
    if idx < 1 or idx > len(pick_ids):
        await message.answer(f"Нужен номер от 1 до {len(pick_ids)}")
        return
    user_id = message.from_user.id if message.from_user else 0
    rem_id = pick_ids[idx - 1]
    cached: list[dict[str, Any]] = list(data.get("rem_rows") or [])
    row = next((r for r in cached if int(r.get("id") or 0) == rem_id), None)
    if row is None:
        row = get_reminder(user_id, rem_id)
    if row is None:
        await show_reminders(message, state)
        return
    await state.set_state(MenuFlow.reminders_item_action)
    await state.update_data(
        rem_edit_id=row["id"],
        rem_edit_row=row,
        menu_screen="reminders",
    )
    await replace_ui(
        message,
        state,
        format_reminder_card(row),
        reply_markup=kb_reminder_item(),
    )


# Переключить is_active выбранного напоминания.
@menu_router.message(MenuFlow.reminders_item_action, F.text == BTN_REM_TOGGLE)
async def on_rem_toggle(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    rem_id = int(data.get("rem_edit_id") or 0)
    user_id = message.from_user.id if message.from_user else 0
    base = dict(data.get("rem_edit_row") or {})
    new_active = not bool(base.get("is_active", True))
    # id из FSM — один PATCH без GET владельца и без повторного GET карточки.
    row = set_reminder_active(
        user_id, rem_id, new_active, check_owner=False
    )
    if row is None:
        await show_reminders(message, state)
        return
    if base:
        merged = dict(base)
        merged["is_active"] = bool(row.get("is_active", new_active))
        row = merged
    rem_rows: list[dict[str, Any]] = list(data.get("rem_rows") or [])
    rem_rows = [
        ({**r, "is_active": row["is_active"]} if int(r.get("id") or 0) == rem_id else r)
        for r in rem_rows
    ]
    await state.update_data(rem_edit_row=row, rem_rows=rem_rows)
    await replace_ui(
        message,
        state,
        f"{'▶️ Включено' if row['is_active'] else '⏸ Выключено'}\n"
        "\n"
        f"{format_reminder_card(row)}",
        reply_markup=kb_reminder_item(),
    )


# Запрос подтверждения перед удалением напоминания.
@menu_router.message(MenuFlow.reminders_item_action, F.text == BTN_REM_DELETE)
async def on_rem_delete_ask(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    rem_id = int(data.get("rem_edit_id") or 0)
    row = dict(data.get("rem_edit_row") or {})
    if not row:
        user_id = message.from_user.id if message.from_user else 0
        fetched = get_reminder(user_id, rem_id)
        row = dict(fetched or {})
    if not row:
        await show_reminders(message, state)
        return
    title = str(row.get("title") or "напоминание")
    await state.set_state(MenuFlow.reminders_delete_confirm)
    await state.update_data(menu_screen="reminders", rem_edit_row=row)
    await replace_ui(
        message,
        state,
        "🗑 Удалить напоминание?\n"
        "\n"
        f"«{title}»\n"
        "\n"
        "Точно удалить? Это действие нельзя отменить",
        reply_markup=kb_confirm_delete_reminder(),
    )


# Подтверждение удаления → DELETE reminder в NocoDB.
@menu_router.message(MenuFlow.reminders_delete_confirm, F.text == BTN_REM_DELETE_YES)
async def on_rem_delete_yes(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    rem_id = int(data.get("rem_edit_id") or 0)
    user_id = message.from_user.id if message.from_user else 0
    row = dict(data.get("rem_edit_row") or {})
    title = str(row.get("title") or "")
    # id из FSM — один DELETE без GET.
    delete_reminder(user_id, rem_id, check_owner=False)
    await state.set_state(None)
    await state.update_data(
        menu_screen="reminders", rem_edit_id=None, rem_edit_row=None
    )
    await replace_ui(
        message,
        state,
        f"🗑 Удалено: {title or 'напоминание'}",
        reply_markup=kb_reminders(),
    )


# Отказ от удаления → назад к карточке напоминания.
@menu_router.message(MenuFlow.reminders_delete_confirm, F.text == BTN_REM_DELETE_NO)
async def on_rem_delete_no(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    rem_id = int(data.get("rem_edit_id") or 0)
    row = dict(data.get("rem_edit_row") or {})
    if not row:
        user_id = message.from_user.id if message.from_user else 0
        fetched = get_reminder(user_id, rem_id)
        row = dict(fetched or {})
    if not row:
        await show_reminders(message, state)
        return
    await state.set_state(MenuFlow.reminders_item_action)
    await state.update_data(menu_screen="reminders")
    await replace_ui(
        message,
        state,
        format_reminder_card(row),
        reply_markup=kb_reminder_item(),
    )


# Inline: перенести напоминание на следующую еду (сброс is_triggered_today).
@menu_router.callback_query(F.data.startswith(CALLBACK_REM_SNOOZE_PREFIX))
async def on_rem_snooze(callback: CallbackQuery, state: FSMContext) -> None:
    raw = (callback.data or "").removeprefix(CALLBACK_REM_SNOOZE_PREFIX)
    if not raw.isdigit():
        await callback.answer()
        return
    user_id = callback.from_user.id
    ok = snooze_reminder(user_id, int(raw))
    await callback.answer("Перенесено на следующую еду" if ok else "Уже недоступно")
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        if ok:
            try:
                await callback.message.edit_text(
                    f"{callback.message.text or ''}\n\n⏰ Перенесено на следующую еду"
                )
            except Exception:
                pass


# Inline: подтвердить уведомление (кнопки убираем).
@menu_router.callback_query(F.data.startswith(CALLBACK_REM_OK_PREFIX))
async def on_rem_ok(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Ок")
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass


# Inline после опроса: «🟩 Хорошо» → дождаться create/upsert → Распознать.
@menu_router.callback_query(F.data == CALLBACK_SURVEY_USAGE_OK)
async def on_survey_usage_ok(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    user_id = callback.from_user.id if callback.from_user else 0
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await _finish_survey_after_usage_ok(
            callback.message, state, user_id=user_id
        )
#endregion

#region Usage-reminder (фон: нет еды до 13:00)
# Кандидаты на напоминание: enabled, локальное время ≥ 13:00, ещё не слали
# сегодня, food_logs за логический день пусты. Sync — вызывается из to_thread.
# Используется usage_reminder_loop.
def _collect_usage_reminder_targets() -> list[tuple[int, str]]:
    targets: list[tuple[int, str]] = []
    try:
        users = db.list_users_with_usage_reminder()
    except Exception as e:
        report_error_auto(f"list_users_with_usage_reminder failed: {e}", exc=e)
        return []
    for user in users:
        try:
            tz_name = str(user.get("timezone") or "Europe/Moscow")
            tz = ZoneInfo(tz_name)
            now = datetime.now(tz)
            if now.hour < USAGE_REMINDER_HOUR:
                continue
            today = logical_today(user)
            if str(user.get("usage_reminder_sent_on") or "") == today:
                continue
            logs = get_food_logs_for_date(int(user["id"]), today)
            if logs:
                continue
            targets.append((int(user["id"]), today))
        except Exception as e:
            report_console_error(
                f"usage reminder target check user_id={user.get('id')}: {e}",
                exc=e,
            )
    return targets


# Фоновый цикл: раз в минуту ищет пользователей без еды после 13:00 и пишет им.
# Используется main() рядом с polling.
async def usage_reminder_loop(bot: Bot) -> None:
    await asyncio.sleep(15)
    while True:
        try:
            targets = await asyncio.to_thread(_collect_usage_reminder_targets)
            for user_id, today in targets:
                try:
                    await bot.send_message(
                        user_id,
                        get_usage_reminder_notify_text(),
                        parse_mode="HTML",
                    )
                    await asyncio.to_thread(mark_usage_reminder_sent, user_id, today)
                except Exception as e:
                    report_console_error(
                        f"usage reminder send failed user_id={user_id}: {e}",
                        exc=e,
                    )
        except Exception as e:
            report_console_error(f"usage_reminder_loop tick failed: {e}", exc=e)
        await asyncio.sleep(USAGE_REMINDER_CHECK_INTERVAL_SEC)
#endregion

#region Запуск
# Точка входа: проверка ключей и long-polling.
# Telegram-сессия: при TELEGRAM_PROXY/OUTBOUND_HTTPS_PROXY — через локальный
# mihomo (VPS); иначе напрямую (локальная разработка). Фото/getFile — та же сессия.
async def main() -> None:
    install_error_email_hooks()
    attach_asyncio_error_handler(asyncio.get_running_loop())

    if not BOT_TOKEN:
        raise SystemExit(
            "TELEGRAM_BOT_API_KEY не найден. Добавь его в .env и перезапусти скрипт."
        )
    if not GEMINI_API_KEY:
        raise SystemExit(
            "GEMINI_API_KEY не найден. Добавь его в .env и перезапусти скрипт."
        )

    if not SMTP_PASSWORD:
        print(
            "🟧 SMTP_PASSWORD не задан в .env — отзывы и письма об ошибках "
            "на почту не будут работать, пока не добавите пароль приложения Yandex",
            flush=True,
        )

    telegram_proxy = get_telegram_proxy()
    session: AiohttpSession | None = None
    if telegram_proxy:
        # Нужен пакет aiohttp-socks (см. requirements.txt).
        session = AiohttpSession(proxy=telegram_proxy)
        print(f"🟦 Telegram через прокси: {telegram_proxy}", flush=True)
    else:
        print("🟦 Telegram напрямую (прокси не задан)", flush=True)

    bot = Bot(token=BOT_TOKEN, session=session) if session else Bot(token=BOT_TOKEN)
    print("🟩 Бот @nutrisnap_ultra_bot запущен. Нажми Ctrl+C для остановки", flush=True)
    usage_task = asyncio.create_task(
        usage_reminder_loop(bot), name="usage-reminder-loop"
    )
    try:
        await dp.start_polling(bot)
    finally:
        usage_task.cancel()
        try:
            await usage_task
        except asyncio.CancelledError:
            pass


# Запуск: python main.py
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🟧 Бот остановлен", flush=True)
#endregion
