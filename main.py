"""
main.py — точка входа Telegram-бота NutriSnap (@nutrisnap_ultra_bot).

Зачем нужен файл
----------------
Запуск бота (long polling), инфраструктура (Bot, Dispatcher, MemoryStorage),
главное меню (дневник, распознать, настройки, напоминания, выгрузка) и /start.
Распознавание еды — в food_recognition.py (отдельный Router).
Первичный опрос — в initial_survey.py (флаг INITIAL_SURVEY_ENABLED).

Как устроен файл
----------------
1. Импорты, .env, константы кнопок, MenuFlow.
2. Stub-хранилище и 🔰-хелперы (заглушки вместо SQL: users / food_logs / reminders).
3. Форматтеры экранов и Reply/Inline-клавиатуры.
4. UI-хелперы: Reply → новое сообщение; Inline дневника → edit; чистка «Выберите действие:».
5. Router меню + хендлеры; /start → опрос (если флаг) или главное меню; on_food_saved → reminders.
6. main() — старт polling.
"""

from __future__ import annotations

import asyncio
import html
import os
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from dotenv import load_dotenv

from food_recognition import setup_food_recognition
from initial_survey import setup_initial_survey, start_initial_survey

#region Конфиг и тексты кнопок
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Если True — /start всегда кидает на первичный опрос (для разработки UI опроса).
# Позже: выключить и проверять в БД, прошёл ли пользователь опрос при первом запуске.
INITIAL_SURVEY_ENABLED = True

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
BTN_SET_FEEDBACK = "💬 Отправить отзыв"
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
        BTN_SET_FEEDBACK,
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

#region Stub-хранилище (вместо БД)
# In-memory профили и записи на время сессии процесса — пока нет SQL.
_stub_profiles: dict[int, dict[str, Any]] = {}
_stub_food_logs: dict[int, list[dict[str, Any]]] = {}
_stub_reminders: dict[int, list[dict[str, Any]]] = {}
_stub_reminder_id_seq = 0


# 🔰 Профиль пользователя (users). Создаёт тестовый профиль при первом обращении.
# Используется экранами меню, выгрузкой и расчётом логической даты.
def stub_get_user(user_id: int) -> dict[str, Any]:
    # 🔰 SELECT * FROM users WHERE id = ?
    if user_id not in _stub_profiles:
        _stub_profiles[user_id] = {
            "id": user_id,
            "first_name": "Тест",
            "gender": "male",
            "age": 28,
            "height": 178.0,
            "weight": 75.0,
            "activity_level": 1.375,
            "goal": "weight_loss",
            "daily_calories": 2200,
            "timezone": "Europe/Moscow",
            "day_change_hour": 4,
            "last_active_at": int(time.time()),
        }
    return _stub_profiles[user_id]


# 🔰 Обновляет users.last_active_at (для заморозки напоминаний через 3 дня).
# Используется при активности в боте и перед проверкой триггеров.
def stub_touch_user_activity(user_id: int) -> None:
    # 🔰 UPDATE users SET last_active_at = ? WHERE id = ?
    user = stub_get_user(user_id)
    user["last_active_at"] = int(time.time())


# 🔰 Инициализация тестовых food_logs на «сегодня» (если ещё нет записей).
# Используется stub_get_food_logs_for_date при первом запросе дневника.
def _ensure_stub_food_logs(user_id: int) -> None:
    # 🔰 SELECT COUNT(*) FROM food_logs WHERE user_id = ?
    if user_id in _stub_food_logs:
        return
    user = stub_get_user(user_id)
    today = logical_today(user)
    tz = ZoneInfo(user["timezone"])
    base = datetime.now(tz).replace(second=0, microsecond=0)
    _stub_food_logs[user_id] = [
        {
            "id": 1,
            "user_id": user_id,
            "emoji": "🍳",
            "title": "Овсянка с бананом",
            "calories": 420,
            "proteins": 14.0,
            "fats": 9.0,
            "carbs": 68.0,
            "portion_g": 300.0,
            "logged_date": today,
            "created_at": int(base.replace(hour=8, minute=30).timestamp()),
        },
        {
            "id": 2,
            "user_id": user_id,
            "emoji": "🍕",
            "title": "Куриная грудка с рисом",
            "calories": 650,
            "proteins": 48.0,
            "fats": 12.0,
            "carbs": 70.0,
            "portion_g": 400.0,
            "logged_date": today,
            "created_at": int(base.replace(hour=13, minute=15).timestamp()),
        },
        {
            "id": 3,
            "user_id": user_id,
            "emoji": "☕️",
            "title": "Греческий йогурт",
            "calories": 180,
            "proteins": 15.0,
            "fats": 5.0,
            "carbs": 12.0,
            "portion_g": 150.0,
            "logged_date": today,
            "created_at": int(base.replace(hour=16, minute=40).timestamp()),
        },
    ]


# 🔰 Записи дневника за логическую дату YYYY-MM-DD.
# Используется главным меню, дневником, удалением и выгрузкой.
def stub_get_food_logs_for_date(user_id: int, logged_date: str) -> list[dict[str, Any]]:
    # 🔰 SELECT * FROM food_logs WHERE user_id = ? AND logged_date = ?
    #    ORDER BY created_at ASC  (индекс idx_food_logs_user_date)
    _ensure_stub_food_logs(user_id)
    rows = [r for r in _stub_food_logs[user_id] if r["logged_date"] == logged_date]
    return sorted(rows, key=lambda r: r["created_at"])


# 🔰 Записи за диапазон дат [date_from, date_to] включительно.
# Используется выгрузкой журнала за неделю/месяц.
def stub_get_food_logs_range(
    user_id: int, date_from: str, date_to: str
) -> list[dict[str, Any]]:
    # 🔰 SELECT * FROM food_logs
    #    WHERE user_id = ? AND logged_date BETWEEN ? AND ?
    #    ORDER BY logged_date, created_at
    _ensure_stub_food_logs(user_id)
    rows = [
        r
        for r in _stub_food_logs[user_id]
        if date_from <= r["logged_date"] <= date_to
    ]
    return sorted(rows, key=lambda r: (r["logged_date"], r["created_at"]))


# 🔰 Удаление записи food_logs по id (только свои).
# Используется флоу «Удалить блюдо» в дневнике.
def stub_delete_food_log(user_id: int, log_id: int) -> bool:
    # 🔰 DELETE FROM food_logs WHERE id = ? AND user_id = ?
    _ensure_stub_food_logs(user_id)
    before = len(_stub_food_logs[user_id])
    _stub_food_logs[user_id] = [
        r for r in _stub_food_logs[user_id] if not (r["id"] == log_id and r["user_id"] == user_id)
    ]
    return len(_stub_food_logs[user_id]) < before


# 🔰 Обновление day_change_hour в users.
# Используется настройкой «Время смены суток».
def stub_set_day_change_hour(user_id: int, hour: int) -> None:
    # 🔰 UPDATE users SET day_change_hour = ? WHERE id = ?
    user = stub_get_user(user_id)
    user["day_change_hour"] = hour


# 🔰 Обновление goal в users.
# Используется настройкой «Тип отслеживания».
def stub_set_goal(user_id: int, goal: str) -> None:
    # 🔰 UPDATE users SET goal = ? WHERE id = ?
    user = stub_get_user(user_id)
    user["goal"] = goal


# 🔰 Обновление daily_calories в users.
# Используется настройкой «Целевые ккал».
def stub_set_daily_calories(user_id: int, calories: int) -> None:
    # 🔰 UPDATE users SET daily_calories = ? WHERE id = ?
    user = stub_get_user(user_id)
    user["daily_calories"] = calories


# 🔰 Запись полей профиля из первичного опроса (включая timezone и daily_calories).
# Используется on_survey_complete после прохождения initial_survey.
def stub_set_profile(
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
    # 🔰 UPDATE users SET first_name=?, gender=?, age=?, height=?, weight=?,
    #    activity_level=?, goal=?, timezone=?, daily_calories=? WHERE id=?
    user = stub_get_user(user_id)
    user["first_name"] = first_name
    user["gender"] = gender
    user["age"] = age
    user["height"] = height
    user["weight"] = weight
    user["activity_level"] = activity_level
    user["goal"] = goal
    user["timezone"] = timezone
    user["daily_calories"] = daily_calories
    user["last_active_at"] = int(time.time())


# 🔰 Заглушка отправки отзыва разработчику (вместо email/SMTP).
# Используется флоу «Отправить отзыв».
def stub_send_feedback(user_id: int, text: str, has_photo: bool) -> None:
    # 🔰 В будущем: письмо/тикет. Сейчас — лог в консоль.
    print(
        f"🔰 feedback user_id={user_id} has_photo={has_photo} text={text!r}",
        flush=True,
    )


# 🔰 Список напоминаний пользователя (reminders), от новых к старым по id.
# Используется экранами «Напоминания» / «Мои напоминания».
def stub_get_reminders(user_id: int) -> list[dict[str, Any]]:
    # 🔰 SELECT * FROM reminders WHERE user_id = ? ORDER BY id
    return list(_stub_reminders.get(user_id, []))


# 🔰 Одно напоминание по id (только своё).
# Используется карточкой напоминания, toggle/delete и snooze.
def stub_get_reminder(user_id: int, reminder_id: int) -> dict[str, Any] | None:
    # 🔰 SELECT * FROM reminders WHERE id = ? AND user_id = ?
    for row in _stub_reminders.get(user_id, []):
        if row["id"] == reminder_id:
            return row
    return None


# 🔰 Создание напоминания (reminders).
# Используется флоу «➕ Добавить напоминание».
def stub_add_reminder(
    user_id: int,
    title: str,
    time_start: str,
    time_end: str,
    min_calories: int,
) -> dict[str, Any]:
    # 🔰 INSERT INTO reminders (user_id, title, time_start, time_end, min_calories)
    #    VALUES (?, ?, ?, ?, ?) RETURNING *
    global _stub_reminder_id_seq
    _stub_reminder_id_seq += 1
    row = {
        "id": _stub_reminder_id_seq,
        "user_id": user_id,
        "title": title,
        "time_start": time_start,
        "time_end": time_end,
        "min_calories": int(min_calories),
        "is_triggered_today": False,
        "is_active": True,
        "_trigger_date": logical_today(stub_get_user(user_id)),
    }
    _stub_reminders.setdefault(user_id, []).append(row)
    return row


# 🔰 Вкл/выкл напоминания (reminders.is_active).
# Используется карточкой «Мои напоминания».
def stub_set_reminder_active(user_id: int, reminder_id: int, is_active: bool) -> bool:
    # 🔰 UPDATE reminders SET is_active = ? WHERE id = ? AND user_id = ?
    row = stub_get_reminder(user_id, reminder_id)
    if row is None:
        return False
    row["is_active"] = bool(is_active)
    return True


# 🔰 Удаление напоминания.
# Используется карточкой «Мои напоминания».
def stub_delete_reminder(user_id: int, reminder_id: int) -> bool:
    # 🔰 DELETE FROM reminders WHERE id = ? AND user_id = ?
    rows = _stub_reminders.get(user_id, [])
    before = len(rows)
    _stub_reminders[user_id] = [r for r in rows if r["id"] != reminder_id]
    return len(_stub_reminders.get(user_id, [])) < before


# 🔰 Сброс is_triggered_today при смене логических суток (заглушка вместо cron).
# Используется перед проверкой триггеров после сохранения еды.
def _stub_reset_reminder_triggers_if_new_day(user_id: int) -> None:
    # 🔰 UPDATE reminders SET is_triggered_today = FALSE
    #    WHERE user_id = ? AND <смена логических суток>
    user = stub_get_user(user_id)
    today = logical_today(user)
    for row in _stub_reminders.get(user_id, []):
        if row.get("_trigger_date") != today:
            row["is_triggered_today"] = False
            row["_trigger_date"] = today


# 🔰 Пользователь «заморожен» по last_active_at (нет активности > N дней).
# Используется proactive/missed-уведомлениями; food-триггер обычно идёт при визите.
def stub_reminders_frozen(user_id: int) -> bool:
    user = stub_get_user(user_id)
    last = int(user.get("last_active_at") or 0)
    if last <= 0:
        return False
    return (time.time() - last) > REMINDER_FREEZE_AFTER_DAYS * 86400


# 🔰 Перенос сработавшего напоминания на следующую еду (сброс is_triggered_today).
# Используется inline «⏰ На следующую еду» под уведомлением.
def stub_snooze_reminder(user_id: int, reminder_id: int) -> bool:
    # 🔰 UPDATE reminders SET is_triggered_today = FALSE WHERE id = ? AND user_id = ?
    row = stub_get_reminder(user_id, reminder_id)
    if row is None:
        return False
    row["is_triggered_today"] = False
    row["_trigger_date"] = logical_today(stub_get_user(user_id))
    return True


# 🔰 После INSERT в food_logs: активные reminders в текущем окне времени,
# calories >= min_calories, ещё не срабатывали сегодня → пометить и вернуть список.
# Используется колбэком on_food_saved из food_recognition.
def stub_trigger_reminders_for_food(
    user_id: int, calories: int
) -> list[dict[str, Any]]:
    # 🔰 SELECT * FROM reminders
    #    WHERE user_id = ? AND is_active AND NOT is_triggered_today
    #      AND time_start <= now_local <= time_end
    #      AND ? >= min_calories;
    # 🔰 затем UPDATE is_triggered_today = TRUE для найденных.
    # Заморозка (3 дня) на food-триггер не влияет — пользователь уже в боте.
    stub_touch_user_activity(user_id)
    _stub_reset_reminder_triggers_if_new_day(user_id)
    user = stub_get_user(user_id)
    now_hm = datetime.now(ZoneInfo(user["timezone"])).strftime("%H:%M")
    kcal = int(calories or 0)
    triggered: list[dict[str, Any]] = []
    for row in _stub_reminders.get(user_id, []):
        if not row.get("is_active"):
            continue
        if row.get("is_triggered_today"):
            continue
        if not (row["time_start"] <= now_hm <= row["time_end"]):
            continue
        if kcal < int(row.get("min_calories") or 0):
            continue
        row["is_triggered_today"] = True
        row["_trigger_date"] = logical_today(user)
        triggered.append(dict(row))
        print(
            f"🔰 reminder triggered user_id={user_id} id={row['id']} "
            f"title={row['title']!r} kcal={kcal}",
            flush=True,
        )
    return triggered


# 🔰 Заглушка проверки «окно закончилось, еду не залогировали» (будущий cron/job).
# Сейчас не вызывается — планировщик подключим отдельно.
def stub_check_missed_reminders(user_id: int) -> list[dict[str, Any]]:
    # 🔰 SELECT active reminders WHERE now > time_end AND NOT is_triggered_today
    #    AND user not frozen; затем уведомить «пропущено».
    if stub_reminders_frozen(user_id):
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
            [KeyboardButton(text=BTN_SET_FEEDBACK)],
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
            [KeyboardButton(text=BTN_BACK), KeyboardButton(text=BTN_MAIN_MENU)],
        ],
        resize_keyboard=True,
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
# Используется show_* и промптами подменю.
async def replace_ui(
    message: Message,
    state: FSMContext,
    text: str,
    reply_markup: ReplyKeyboardMarkup | InlineKeyboardMarkup | None = None,
) -> Message:
    stale_ids = await pop_action_prompt_ids(state)
    if stale_ids:
        try:
            edited = await message.bot.edit_message_text(
                text=text,
                chat_id=message.chat.id,
                message_id=stale_ids[0],
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

    sent = await message.answer(text, reply_markup=reply_markup)
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
    user = stub_get_user(uid)
    user_id = uid
    today = logical_today(user)
    logs = stub_get_food_logs_for_date(user_id, today)
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
    user = stub_get_user(uid)
    user_id = uid
    logged_date = logical_date_with_offset(user, offset)
    logs = stub_get_food_logs_for_date(user_id, logged_date)
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
    user = stub_get_user(user_id)
    rem_count = len(stub_get_reminders(user_id))
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


# Экран «🔔 Напоминания и Витамины»: описание + добавить / список.
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
        "Выберите действие:"
    )
    await replace_ui(message, state, text, reply_markup=kb_reminders())


# Шлёт уведомления по сработавшим reminders после сохранения еды.
# Используется колбэком on_food_saved из food_recognition.
async def notify_reminders_after_food(
    user_id: int,
    calories: int,
    bot: Bot,
    chat_id: int,
) -> None:
    triggered = stub_trigger_reminders_for_food(user_id, calories)
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
    user = stub_get_user(user_id)
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
        "Выберите период. Файл придёт в формате .txt "
        "(удобно вставить в нейросеть для рекомендаций)",
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
    # 🔰 Данные уже получены stub_get_*; здесь только сборка файла.
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
# Колбэк после подтверждения еды: проверка reminders и рассылка уведомлений.
# Передаётся в food_recognition.setup_food_recognition(on_food_saved=...).
async def _on_food_saved(
    user_id: int,
    result: Any,
    bot: Bot,
    chat_id: int,
) -> None:
    calories = int(getattr(result, "calories", 0) or 0)
    await notify_reminders_after_food(user_id, calories, bot, chat_id)


# Колбэк после первичного опроса: stub-профиль → экран «Распознать» (ждём фото/текст).
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
    stub_set_profile(
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
    await state.clear()
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
# /start — при INITIAL_SURVEY_ENABLED → первичный опрос, иначе главное меню.
# Регистрируется на dp (не на menu_router), чтобы всегда быть доступным.
# Позже: вместо флага — проверка в БД (прошёл ли пользователь опрос).
@dp.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    if INITIAL_SURVEY_ENABLED:
        await start_initial_survey(message, state)
        return
    await state.clear()
    await show_main_menu(message, state)


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
    if current in (
        MenuFlow.settings_day_hour.state,
        MenuFlow.feedback_wait.state,
    ):
        await show_settings(message, state)
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
        row = stub_get_reminder(user_id, rem_id)
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
    user = stub_get_user(user_id)
    data = await state.get_data()
    offset = int(data.get("diary_offset", 0))
    logged_date = logical_date_with_offset(user, offset)
    logs = stub_get_food_logs_for_date(user_id, logged_date)
    await state.update_data(
        menu_screen="diary",
        pick_logs=[r["id"] for r in logs],
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


# Выбор номера для изменения → заглушка (без формы полей).
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
    # 🔰 UPDATE food_logs ... — пока не подключено
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
    user = stub_get_user(user_id)
    data = await state.get_data()
    offset = int(data.get("diary_offset", 0))
    logged_date = logical_date_with_offset(user, offset)
    logs = stub_get_food_logs_for_date(user_id, logged_date)
    await state.update_data(
        menu_screen="diary",
        pick_logs=[r["id"] for r in logs],
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


# Выбор номера для удаления → 🔰 stub delete + обновление дневника.
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
    stub_delete_food_log(user_id, log_id)
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
    user = stub_get_user(user_id)
    offset = int(data.get("diary_offset", 0))
    logged_date = logical_date_with_offset(user, offset)
    logs = stub_get_food_logs_for_date(user_id, logged_date)
    # Сохраняем порядок/состав pick_logs; для текста берём актуальные записи по id.
    id_to_log = {r["id"]: r for r in logs}
    ordered = [id_to_log[i] for i in pick_ids if i in id_to_log]
    if len(ordered) != total:
        ordered = logs
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
    user = stub_get_user(user_id)
    day = logical_today(user)
    logs = stub_get_food_logs_for_date(user_id, day)
    await send_export_file(
        message, user, logs, f"текущий день ({day})", f"diary_{day}.txt"
    )


# Выгрузка за прошлый логический день.
@menu_router.message(F.text == BTN_EXPORT_YESTERDAY)
async def on_export_yesterday(message: Message, state: FSMContext) -> None:
    await state.update_data(menu_screen="export")
    user_id = message.from_user.id if message.from_user else 0
    user = stub_get_user(user_id)
    day = logical_date_with_offset(user, -1)
    logs = stub_get_food_logs_for_date(user_id, day)
    await send_export_file(
        message, user, logs, f"прошлый день ({day})", f"diary_{day}.txt"
    )


# Выгрузка за 7 логических дней (включая сегодня).
@menu_router.message(F.text == BTN_EXPORT_WEEK)
async def on_export_week(message: Message, state: FSMContext) -> None:
    await state.update_data(menu_screen="export")
    user_id = message.from_user.id if message.from_user else 0
    user = stub_get_user(user_id)
    date_to = logical_today(user)
    date_from = logical_date_with_offset(user, -6)
    logs = stub_get_food_logs_range(user_id, date_from, date_to)
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
    user = stub_get_user(user_id)
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
    logs = stub_get_food_logs_range(user_id, date_from, date_to)
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


# Согласие на перезапуск опроса → заглушка (онбординг подключим позже).
@menu_router.message(F.text == BTN_CONFIRM_UPDATE_YES)
async def on_update_profile_yes(message: Message, state: FSMContext) -> None:
    await state.set_state(None)
    await state.update_data(menu_screen="profile")
    await replace_ui(
        message,
        state,
        "🔜 Перекидываем вас на первоначальный опрос…\n"
        "\n"
        "Заглушка: сам опрос подключим позже. "
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
    user = stub_get_user(user_id)
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


# Сохранение day_change_hour (🔰).
@menu_router.message(MenuFlow.settings_day_hour, F.text, ~F.text.in_(MENU_BUTTON_TEXTS))
async def on_set_day_hour_value(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit() or not (0 <= int(text) <= 23):
        await message.answer("Введите целое число часа от 0 до 23")
        return
    user_id = message.from_user.id if message.from_user else 0
    stub_set_day_change_hour(user_id, int(text))
    await state.set_state(None)
    await replace_ui(
        message,
        state,
        f"✅ Смена суток установлена на {int(text):02d}:00 (🔰 stub)",
        reply_markup=kb_settings(),
    )


# Выгрузка из настроек.
@menu_router.message(F.text == BTN_SET_EXPORT)
async def on_settings_export(message: Message, state: FSMContext) -> None:
    await state.update_data(menu_screen="export", export_return="settings")
    await show_export_menu(message, state)


# Старт отзыва: ждём текст и/или фото.
@menu_router.message(F.text == BTN_SET_FEEDBACK)
async def on_feedback_start(message: Message, state: FSMContext) -> None:
    await state.set_state(MenuFlow.feedback_wait)
    await state.update_data(menu_screen="settings")
    await replace_ui(
        message,
        state,
        "💬 Обратная связь\n"
        "\n"
        "Напишите текст отзыва. Можно прикрепить фото "
        "(подпись к фото тоже подойдёт).\n"
        "Чтобы отменить — «⬅️ Назад» или «🏠 Главное меню»",
        reply_markup=kb_nav_only(),
    )


# Приём отзыва (текст, не кнопка меню) → 🔰 stub.
@menu_router.message(MenuFlow.feedback_wait, F.text, ~F.text.in_(MENU_BUTTON_TEXTS))
async def on_feedback_text(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id if message.from_user else 0
    text = (message.text or "").strip()
    stub_send_feedback(user_id, text, has_photo=False)
    await state.set_state(None)
    await replace_ui(
        message,
        state,
        "✅ Спасибо! Отзыв передан разработчику (🔰 stub, без email)",
        reply_markup=kb_settings(),
    )


# Приём отзыва с фото (подпись опциональна) → 🔰 stub.
@menu_router.message(MenuFlow.feedback_wait, F.photo)
async def on_feedback_photo(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id if message.from_user else 0
    text = (message.caption or "").strip()
    stub_send_feedback(user_id, text or "(без текста)", has_photo=True)
    await state.set_state(None)
    await replace_ui(
        message,
        state,
        "✅ Спасибо! Отзыв передан разработчику (🔰 stub, без email)",
        reply_markup=kb_settings(),
    )


# Выбор типа отслеживания — показать текущий тип и кнопки целей (из экрана профиля).
@menu_router.message(F.text == BTN_SET_GOAL)
async def on_set_goal(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id if message.from_user else 0
    user = stub_get_user(user_id)
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


# Сохранение goal (🔰). Если тип изменился — спросить про пересчёт целевых ккал.
@menu_router.message(MenuFlow.settings_goal, F.text.in_(set(GOAL_BY_BTN)))
async def on_set_goal_value(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id if message.from_user else 0
    user = stub_get_user(user_id)
    old_goal = user.get("goal", "")
    new_goal = GOAL_BY_BTN[message.text or ""]

    if new_goal == old_goal:
        await show_profile(message, state)
        return

    stub_set_goal(user_id, new_goal)
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


# Согласие на пересчёт ккал после смены типа → заглушка (формулу подключим позже).
@menu_router.message(MenuFlow.settings_goal_recalc, F.text == BTN_CONFIRM_RECALC_YES)
async def on_goal_recalc_yes(message: Message, state: FSMContext) -> None:
    await state.set_state(None)
    await state.update_data(menu_screen="profile")
    await replace_ui(
        message,
        state,
        "🔜 Пересчёт целевых ккал…\n"
        "\n"
        "Заглушка: формулу пересчёта подключим позже. "
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
    user = stub_get_user(user_id)
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


# Сохранение daily_calories (🔰) → обновлённая сводка профиля.
@menu_router.message(MenuFlow.settings_calories, F.text, ~F.text.in_(MENU_BUTTON_TEXTS))
async def on_set_calories_value(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit() or int(text) <= 0:
        await message.answer("Введите целое положительное число ккал")
        return
    user_id = message.from_user.id if message.from_user else 0
    stub_set_daily_calories(user_id, int(text))
    await show_profile(message, state)
#endregion

#region Хендлеры: напоминания
# Открыть раздел «Напоминания и Витамины».
@menu_router.message(F.text == BTN_SET_REMINDERS)
async def on_set_reminders(message: Message, state: FSMContext) -> None:
    stub_touch_user_activity(message.from_user.id if message.from_user else 0)
    await show_reminders(message, state)


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


# Шаг 3: порог → 🔰 INSERT reminder.
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
    row = stub_add_reminder(user_id, title, time_start, time_end, min_cal)
    await state.set_state(None)
    await state.update_data(rem_draft={}, menu_screen="reminders")
    await replace_ui(
        message,
        state,
        "✅ Напоминание добавлено (🔰 stub)\n"
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
    rows = stub_get_reminders(user_id)
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
    user_id = message.from_user.id if message.from_user else 0
    rows = stub_get_reminders(user_id)
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
    row = stub_get_reminder(user_id, pick_ids[idx - 1])
    if row is None:
        await show_reminders(message, state)
        return
    await state.set_state(MenuFlow.reminders_item_action)
    await state.update_data(rem_edit_id=row["id"], menu_screen="reminders")
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
    row = stub_get_reminder(user_id, rem_id)
    if row is None:
        await show_reminders(message, state)
        return
    stub_set_reminder_active(user_id, rem_id, not bool(row.get("is_active")))
    row = stub_get_reminder(user_id, rem_id)
    assert row is not None
    await replace_ui(
        message,
        state,
        f"{'▶️ Включено' if row['is_active'] else '⏸ Выключено'} (🔰 stub)\n"
        "\n"
        f"{format_reminder_card(row)}",
        reply_markup=kb_reminder_item(),
    )


# Запрос подтверждения перед удалением напоминания.
@menu_router.message(MenuFlow.reminders_item_action, F.text == BTN_REM_DELETE)
async def on_rem_delete_ask(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    rem_id = int(data.get("rem_edit_id") or 0)
    user_id = message.from_user.id if message.from_user else 0
    row = stub_get_reminder(user_id, rem_id)
    if row is None:
        await show_reminders(message, state)
        return
    title = str(row.get("title") or "напоминание")
    await state.set_state(MenuFlow.reminders_delete_confirm)
    await state.update_data(menu_screen="reminders")
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


# Подтверждение удаления → 🔰 DELETE reminder.
@menu_router.message(MenuFlow.reminders_delete_confirm, F.text == BTN_REM_DELETE_YES)
async def on_rem_delete_yes(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    rem_id = int(data.get("rem_edit_id") or 0)
    user_id = message.from_user.id if message.from_user else 0
    title = ""
    row = stub_get_reminder(user_id, rem_id)
    if row:
        title = str(row.get("title") or "")
    stub_delete_reminder(user_id, rem_id)
    await state.set_state(None)
    await state.update_data(menu_screen="reminders", rem_edit_id=None)
    await replace_ui(
        message,
        state,
        f"🗑 Удалено: {title or 'напоминание'} (🔰 stub)",
        reply_markup=kb_reminders(),
    )


# Отказ от удаления → назад к карточке напоминания.
@menu_router.message(MenuFlow.reminders_delete_confirm, F.text == BTN_REM_DELETE_NO)
async def on_rem_delete_no(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    rem_id = int(data.get("rem_edit_id") or 0)
    user_id = message.from_user.id if message.from_user else 0
    row = stub_get_reminder(user_id, rem_id)
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


# Inline: перенести напоминание на следующую еду (сброс is_triggered_today).
@menu_router.callback_query(F.data.startswith(CALLBACK_REM_SNOOZE_PREFIX))
async def on_rem_snooze(callback: CallbackQuery, state: FSMContext) -> None:
    raw = (callback.data or "").removeprefix(CALLBACK_REM_SNOOZE_PREFIX)
    if not raw.isdigit():
        await callback.answer()
        return
    user_id = callback.from_user.id
    ok = stub_snooze_reminder(user_id, int(raw))
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
#endregion

#region Запуск
# Точка входа: проверка ключей и long-polling.
async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "TELEGRAM_BOT_API_KEY не найден. Добавь его в .env и перезапусти скрипт."
        )
    if not GEMINI_API_KEY:
        raise SystemExit(
            "GEMINI_API_KEY не найден. Добавь его в .env и перезапусти скрипт."
        )

    bot = Bot(token=BOT_TOKEN)
    print("🟩 Бот @nutrisnap_ultra_bot запущен. Нажми Ctrl+C для остановки", flush=True)
    await dp.start_polling(bot)


# Запуск: python main.py
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🟧 Бот остановлен", flush=True)
#endregion
