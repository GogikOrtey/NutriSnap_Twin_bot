"""
main.py — точка входа Telegram-бота NutriSnap (@nutrisnap_ultra_bot).

Зачем нужен файл
----------------
Запуск бота (long polling), инфраструктура (Bot, Dispatcher, MemoryStorage),
главное меню (дневник, распознать, настройки, выгрузка) и /start.
Распознавание еды — в food_recognition.py (отдельный Router).

Как устроен файл
----------------
1. Импорты, .env, константы кнопок, MenuFlow.
2. Stub-хранилище и 🔰-хелперы (заглушки вместо SQL).
3. Форматтеры экранов и Reply/Inline-клавиатуры.
4. UI-хелперы: удаление/замена предыдущих сообщений меню (без спама в чат).
5. Router меню + хендлеры; /start открывает главное меню.
6. main() — старт polling.
"""

from __future__ import annotations

import asyncio
import os
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

#region Конфиг и тексты кнопок
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Кнопка возврата в корень — есть в основных разделах.
BTN_MAIN_MENU = "🏠 Главное меню"

BTN_DIARY = "📒 Дневник питания"
BTN_RECOGNIZE = "🔍 Распознать"
BTN_SETTINGS = "⚙️ Настройки"

BTN_ADD_DISH = "➕ Добавить блюдо"
BTN_EDIT_DISH = "✏️ Изменить блюдо"
BTN_DELETE_DISH = "🗑 Удалить блюдо"
BTN_EXTRA = "📎 Дополнительно"
BTN_BACK = "⬅️ Назад"

BTN_EXPORT_TODAY = "📅 Текущий день"
BTN_EXPORT_YESTERDAY = "📆 Прошлый день"
BTN_EXPORT_WEEK = "🗓 Прошедшую неделю"
BTN_EXPORT_MONTH = "🗂 Месяц (30 дней)"

BTN_MONTH_0_30 = "1️⃣ Последние 30 дней"
BTN_MONTH_30_60 = "2️⃣ От 30 до 60 дней назад"
BTN_MONTH_60_90 = "3️⃣ От 60 до 90 дней назад"

BTN_SET_DAY_HOUR = "🕓 Время смены суток"
BTN_SET_EXPORT = "📤 Сделать выгрузку журнала"
BTN_SET_FEEDBACK = "💬 Отправить отзыв"
BTN_SET_GOAL = "🎯 Тип отслеживания"
BTN_SET_CALORIES = "🔥 Целевые ккал в сутки"

BTN_GOAL_LOSS = "📉 Похудение"
BTN_GOAL_GAIN = "📈 Набор веса"
BTN_GOAL_MAINTAIN = "⚖️ Просто отслеживание"

CALLBACK_DIARY_PREV = "diary:prev"
CALLBACK_DIARY_NEXT = "diary:next"

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
        BTN_EXPORT_TODAY,
        BTN_EXPORT_YESTERDAY,
        BTN_EXPORT_WEEK,
        BTN_EXPORT_MONTH,
        BTN_MONTH_0_30,
        BTN_MONTH_30_60,
        BTN_MONTH_60_90,
        BTN_SET_DAY_HOUR,
        BTN_SET_EXPORT,
        BTN_SET_FEEDBACK,
        BTN_SET_GOAL,
        BTN_SET_CALORIES,
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
    feedback_wait = State()
    export_month_pick = State()
#endregion

#region Stub-хранилище (вместо БД)
# In-memory профили и записи на время сессии процесса — пока нет SQL.
_stub_profiles: dict[int, dict[str, Any]] = {}
_stub_food_logs: dict[int, list[dict[str, Any]]] = {}


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
        }
    return _stub_profiles[user_id]


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


# 🔰 Заглушка отправки отзыва разработчику (вместо email/SMTP).
# Используется флоу «Отправить отзыв».
def stub_send_feedback(user_id: int, text: str, has_photo: bool) -> None:
    # 🔰 В будущем: письмо/тикет. Сейчас — лог в консоль.
    print(
        f"🔰 feedback user_id={user_id} has_photo={has_photo} text={text!r}",
        flush=True,
    )
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


# Текстовый прогресс-бар ккал: [████░░░░░░░░] 62%.
# Используется карточкой главного меню и дневника.
def format_calorie_progress(eaten: int, target: int) -> str:
    if target <= 0:
        return "[░░░░░░░░░░░░] —"
    pct = min(100, int(round(100 * eaten / target)))
    filled = min(12, int(round(12 * eaten / target)))
    bar = "█" * filled + "░" * (12 - filled)
    return f"[{bar}] {pct}%"


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


# Текст карточки дня: список блюд + прогресс (или пустой день).
# Используется главным меню и экраном дневника.
def format_day_card(
    user: dict[str, Any],
    logged_date: str,
    logs: list[dict[str, Any]],
    *,
    is_today: bool,
    title: str,
) -> str:
    lines = [title, ""]
    if not logs:
        if is_today:
            lines.append(
                "За сегодня записей нет. Отправь фото или описание блюда, "
                "чтобы зафиксировать прием пищи!"
            )
        else:
            lines.append("За этот день записей нет.")
    else:
        for row in logs:
            t = format_log_time(row["created_at"], user["timezone"])
            lines.append(f"• {t} — {row['title']} ({row['calories']} ккал)")
        eaten = sum(int(r["calories"] or 0) for r in logs)
        target = int(user["daily_calories"])
        lines.append("")
        lines.append(f"Итого: {eaten} / {target} ккал")
        lines.append(format_calorie_progress(eaten, target))
    return "\n".join(lines)


# Человекочитаемая подпись цели (weight_loss → Похудение).
# Используется настройками и шапкой выгрузки.
def goal_label(goal: str) -> str:
    return GOAL_LABELS.get(goal, goal)


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
                f"{dt} | {row['title']}\n"
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
            [KeyboardButton(text=BTN_EXTRA)],
            [KeyboardButton(text=BTN_BACK), KeyboardButton(text=BTN_MAIN_MENU)],
        ],
        resize_keyboard=True,
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


# Reply-клавиатура экрана «Распознать».
# Используется show_recognize.
def kb_recognize() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_MAIN_MENU)]],
        resize_keyboard=True,
    )


# Reply-клавиатура раздела «Настройки» (без напоминаний).
# Используется show_settings.
def kb_settings() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_SET_DAY_HOUR)],
            [KeyboardButton(text=BTN_SET_EXPORT)],
            [KeyboardButton(text=BTN_SET_FEEDBACK)],
            [KeyboardButton(text=BTN_SET_GOAL)],
            [KeyboardButton(text=BTN_SET_CALORIES)],
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

#region UI: одно «живое» сообщение меню
# Ключ FSM: id сообщений бота текущего экрана меню (удаляем/редактируем при навигации).
UI_MESSAGE_IDS_KEY = "ui_message_ids"


# Удаляет предыдущие сообщения UI-меню в чате.
# Используется перед показом нового экрана и при /start.
async def clear_ui_messages(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    ids: list[int] = list(data.get(UI_MESSAGE_IDS_KEY) or [])
    if not ids:
        return
    bot = message.bot
    chat_id = message.chat.id
    for mid in ids:
        try:
            await bot.delete_message(chat_id, mid)
        except Exception:
            pass
    await state.update_data(**{UI_MESSAGE_IDS_KEY: []})


# Запоминает id сообщений текущего экрана для последующей замены.
# Используется replace_ui и show_diary.
async def remember_ui_messages(state: FSMContext, *messages: Message) -> None:
    await state.update_data(
        **{UI_MESSAGE_IDS_KEY: [m.message_id for m in messages]}
    )


# Удаляет старый UI и отправляет новый экран (текст + Reply/Inline-клавиатура).
# Используется show_* и промптами подменю: Telegram не даёт менять ReplyKeyboard через edit.
async def replace_ui(
    message: Message,
    state: FSMContext,
    text: str,
    reply_markup: ReplyKeyboardMarkup | InlineKeyboardMarkup | None = None,
) -> Message:
    await clear_ui_messages(message, state)
    sent = await message.answer(text, reply_markup=reply_markup)
    await remember_ui_messages(state, sent)
    return sent
#endregion

#region Показ экранов
# Показывает главное меню: блюда логического «сегодня» + прогресс + клавиатура.
# Используется /start, 🏠, «Назад» из разделов.
async def show_main_menu(
    message: Message, state: FSMContext, user_id: int | None = None
) -> None:
    await clear_ui_messages(message, state)
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
        title="🏠 Главное меню",
    )
    sent = await message.answer(text, reply_markup=kb_main_menu())
    await remember_ui_messages(state, sent)


# Показывает дневник за дату с diary_offset + inline-навигацию.
# Используется кнопкой «Дневник» и callback вчера/завтра.
# user_id обязателен из callback: у callback.message.from_user — бот, не пользователь.
# edit_message: при листании дней — правим карточку на месте, без новых сообщений.
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
    title = f"📒 Дневник питания — {logged_date}"
    text = format_day_card(user, logged_date, logs, is_today=is_today, title=title)
    nav = kb_diary_nav(offset)

    if edit_message is not None:
        try:
            await edit_message.edit_text(text, reply_markup=nav)
            return
        except Exception:
            pass

    # Два сообщения: карточка+inline и Reply-клавиатура раздела
    # (у одного сообщения Telegram не смешивает Reply и Inline).
    await clear_ui_messages(message, state)
    card = await message.answer(text, reply_markup=nav)
    actions = await message.answer("Выберите действие:", reply_markup=kb_diary())
    await remember_ui_messages(state, card, actions)


# Памятка «Распознать» без запуска анализа.
# Используется кнопкой 🔍 Распознать.
async def show_recognize(message: Message, state: FSMContext) -> None:
    await state.set_state(None)
    await state.update_data(menu_screen="recognize")
    text = (
        "🔍 Распознать\n"
        "\n"
        "💡 Отправлять фото или текст можно в любой момент — кнопка не обязательна\n"
        "\n"
        "✨ Что умеет бот:\n"
        "• 📸 Оценить блюдо по фото (можно с подписью)\n"
        "• 📝 Разобрать текстовое описание / ккал\n"
        "• 🏷️ Прочитать этикетку с пищевой ценностью\n"
        "\n"
        "📋 После оценки появится превью — подтвердите или поправьте результат\n"
        "\n"
        "🚀 Можешь начинать распознавание прямо сейчас — отправь в чат фото или текст описания еды"
    )
    await replace_ui(message, state, text, reply_markup=kb_recognize())


# Экран настроек (без пункта напоминаний).
# Используется кнопкой ⚙️ Настройки и возвратами из подменю.
async def show_settings(message: Message, state: FSMContext) -> None:
    await state.set_state(None)
    await state.update_data(export_return="settings", menu_screen="settings")
    user_id = message.from_user.id if message.from_user else 0
    user = stub_get_user(user_id)
    text = (
        "⚙️ Настройки\n"
        "\n"
        f"Смена суток: {user['day_change_hour']:02d}:00 "
        f"({user['timezone']})\n"
        f"Цель: {goal_label(user['goal'])}\n"
        f"Норма: {user['daily_calories']} ккал/сутки"
    )
    await replace_ui(message, state, text, reply_markup=kb_settings())


# Подменю выбора периода выгрузки.
# Используется «Дополнительно» и «Сделать выгрузку журнала».
async def show_export_menu(message: Message, state: FSMContext) -> None:
    await state.set_state(None)
    await state.update_data(menu_screen="export")
    await replace_ui(
        message,
        state,
        "📤 Выгрузка журнала\n"
        "\n"
        "Выберите период. Файл придёт в формате .txt "
        "(удобно вставить в нейросеть для рекомендаций).",
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
        lines.append(f"{i}. {t} — {row['title']} ({row['calories']} ккал)")
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
        caption=f"Выгрузка: {period_title}",
    )
#endregion

#region Router меню и инфраструктура
menu_router = Router(name="main_menu")

storage = MemoryStorage()
dp = Dispatcher(storage=storage)
dp.include_router(menu_router)
dp.include_router(setup_food_recognition(storage, menu_button_texts=MENU_BUTTON_TEXTS))
#endregion

#region Хендлеры: корень и навигация
# /start — сброс FSM и показ главного меню.
# Регистрируется на dp (не на menu_router), чтобы всегда быть доступным.
@dp.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
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
        MenuFlow.settings_day_hour.state,
        MenuFlow.settings_calories.state,
        MenuFlow.settings_goal.state,
        MenuFlow.feedback_wait.state,
    ):
        await show_settings(message, state)
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
        "что съели — бот оценит ккал и БЖУ. Кнопка меню для этого не нужна.",
        reply_markup=kb_diary(),
    )


# Старт «Изменить блюдо»: список номеров → ожидание номера.
@menu_router.message(F.text == BTN_EDIT_DISH)
async def on_edit_dish(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id if message.from_user else 0
    user = stub_get_user(user_id)
    data = await state.get_data()
    offset = int(data.get("diary_offset", 0))
    logged_date = logical_date_with_offset(user, offset)
    logs = stub_get_food_logs_for_date(user_id, logged_date)
    await state.update_data(menu_screen="diary", pick_logs=[r["id"] for r in logs])
    if not logs:
        await replace_ui(
            message,
            state,
            "За этот день записей нет — менять нечего.",
            reply_markup=kb_diary(),
        )
        return
    await state.set_state(MenuFlow.diary_pick_edit)
    await replace_ui(
        message,
        state,
        "✏️ Изменить блюдо\n"
        "\n"
        "Выберите номер блюда из списка:\n"
        f"{format_numbered_logs(user, logs)}\n"
        "\n"
        "Отправьте номер сообщением (или «⬅️ Назад»).",
        reply_markup=kb_diary(),
    )


# Выбор номера для изменения → заглушка (без формы полей).
@menu_router.message(MenuFlow.diary_pick_edit, F.text, ~F.text.in_(MENU_BUTTON_TEXTS))
async def on_edit_dish_pick(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    data = await state.get_data()
    pick_ids: list[int] = list(data.get("pick_logs") or [])
    if not text.isdigit():
        await message.answer("Введите номер блюда из списка (число).")
        return
    idx = int(text)
    if idx < 1 or idx > len(pick_ids):
        await message.answer(f"Нужен номер от 1 до {len(pick_ids)}.")
        return
    log_id = pick_ids[idx - 1]
    await state.set_state(None)
    # 🔰 UPDATE food_logs ... — пока не подключено
    await replace_ui(
        message,
        state,
        f"Выбрано блюдо #{idx} (id={log_id}).\n"
        "✏️ Сохранение изменений в БД скоро — форма редактирования появится позже.",
        reply_markup=kb_diary(),
    )


# Старт «Удалить блюдо»: список номеров → ожидание номера.
@menu_router.message(F.text == BTN_DELETE_DISH)
async def on_delete_dish(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id if message.from_user else 0
    user = stub_get_user(user_id)
    data = await state.get_data()
    offset = int(data.get("diary_offset", 0))
    logged_date = logical_date_with_offset(user, offset)
    logs = stub_get_food_logs_for_date(user_id, logged_date)
    await state.update_data(menu_screen="diary", pick_logs=[r["id"] for r in logs])
    if not logs:
        await replace_ui(
            message,
            state,
            "За этот день записей нет — удалять нечего.",
            reply_markup=kb_diary(),
        )
        return
    await state.set_state(MenuFlow.diary_pick_delete)
    await replace_ui(
        message,
        state,
        "🗑 Удалить блюдо\n"
        "\n"
        "Выберите номер блюда из списка:\n"
        f"{format_numbered_logs(user, logs)}\n"
        "\n"
        "Отправьте номер сообщением (или «⬅️ Назад»).",
        reply_markup=kb_diary(),
    )


# Выбор номера для удаления → 🔰 stub delete + обновление дневника.
@menu_router.message(MenuFlow.diary_pick_delete, F.text, ~F.text.in_(MENU_BUTTON_TEXTS))
async def on_delete_dish_pick(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    data = await state.get_data()
    pick_ids: list[int] = list(data.get("pick_logs") or [])
    if not text.isdigit():
        await message.answer("Введите номер блюда из списка (число).")
        return
    idx = int(text)
    if idx < 1 or idx > len(pick_ids):
        await message.answer(f"Нужен номер от 1 до {len(pick_ids)}.")
        return
    log_id = pick_ids[idx - 1]
    user_id = message.from_user.id if message.from_user else 0
    stub_delete_food_log(user_id, log_id)
    await state.set_state(None)
    await show_diary(message, state)


# «Дополнительно» → меню выгрузки.
@menu_router.message(F.text == BTN_EXTRA)
async def on_extra(message: Message, state: FSMContext) -> None:
    await state.update_data(menu_screen="export", export_return="diary")
    await show_export_menu(message, state)
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
    await replace_ui(message, state, "Готово.", reply_markup=kb_export())
#endregion

#region Хендлеры: настройки
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
        f"Сейчас: {user['day_change_hour']:02d}:00 "
        f"(часовой пояс {user['timezone']}).\n"
        "Введите час от 0 до 23 (например, 4).",
        reply_markup=kb_settings(),
    )


# Сохранение day_change_hour (🔰).
@menu_router.message(MenuFlow.settings_day_hour, F.text, ~F.text.in_(MENU_BUTTON_TEXTS))
async def on_set_day_hour_value(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit() or not (0 <= int(text) <= 23):
        await message.answer("Введите целое число часа от 0 до 23.")
        return
    user_id = message.from_user.id if message.from_user else 0
    stub_set_day_change_hour(user_id, int(text))
    await state.set_state(None)
    await replace_ui(
        message,
        state,
        f"✅ Смена суток установлена на {int(text):02d}:00 (🔰 stub).",
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
        "Чтобы отменить — «⬅️ Назад» или «🏠 Главное меню».",
        reply_markup=kb_settings(),
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
        "✅ Спасибо! Отзыв передан разработчику (🔰 stub, без email).",
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
        "✅ Спасибо! Отзыв передан разработчику (🔰 stub, без email).",
        reply_markup=kb_settings(),
    )


# Выбор типа отслеживания — показать кнопки целей.
@menu_router.message(F.text == BTN_SET_GOAL)
async def on_set_goal(message: Message, state: FSMContext) -> None:
    await state.set_state(MenuFlow.settings_goal)
    await state.update_data(menu_screen="goal")
    await replace_ui(
        message,
        state,
        "🎯 Тип отслеживания\n\nВыберите направление:",
        reply_markup=kb_goal(),
    )


# Сохранение goal (🔰).
@menu_router.message(MenuFlow.settings_goal, F.text.in_(set(GOAL_BY_BTN)))
async def on_set_goal_value(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id if message.from_user else 0
    goal = GOAL_BY_BTN[message.text or ""]
    stub_set_goal(user_id, goal)
    await state.set_state(None)
    await state.update_data(menu_screen="settings")
    await replace_ui(
        message,
        state,
        f"✅ Цель: {goal_label(goal)} (🔰 stub).",
        reply_markup=kb_settings(),
    )


# Запрос целевых ккал.
@menu_router.message(F.text == BTN_SET_CALORIES)
async def on_set_calories(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id if message.from_user else 0
    user = stub_get_user(user_id)
    await state.set_state(MenuFlow.settings_calories)
    await state.update_data(menu_screen="settings")
    await replace_ui(
        message,
        state,
        "🔥 Целевые ккал в сутки\n"
        "\n"
        f"Сейчас: {user['daily_calories']} ккал.\n"
        "Введите новое целое число (например, 2000).",
        reply_markup=kb_settings(),
    )


# Сохранение daily_calories (🔰).
@menu_router.message(MenuFlow.settings_calories, F.text, ~F.text.in_(MENU_BUTTON_TEXTS))
async def on_set_calories_value(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit() or int(text) <= 0:
        await message.answer("Введите целое положительное число ккал.")
        return
    user_id = message.from_user.id if message.from_user else 0
    stub_set_daily_calories(user_id, int(text))
    await state.set_state(None)
    await replace_ui(
        message,
        state,
        f"✅ Норма: {int(text)} ккал/сутки (🔰 stub).",
        reply_markup=kb_settings(),
    )
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
