"""
initial_survey.py — первичный опрос (онбординг) NutriSnap.

Зачем нужен файл
----------------
FSM-флоу стартового опроса: приветствие → категория → профиль → цель →
часовой пояс → подтверждение целевых ккал → on_complete (экран «Распознать»).
Подключается из main.py через setup_initial_survey(on_complete=...).
При активном флаге INITIAL_SURVEY_ENABLED /start ведёт сюда вместо главного меню.

Позже: проверка в БД, прошёл ли пользователь опрос.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from dotenv import load_dotenv
from geopy.geocoders import Nominatim
from google import genai
from pydantic import BaseModel, Field
from timezonefinder import TimezoneFinder

#region Константы кнопок и тексты
load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
REQUEST_TIMEOUT_MS = 10_000
MODELS_QUEUE = [
    "gemini-flash-latest",
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash",
]

BTN_START_SURVEY = "🚀 Начать короткий опрос"
CALLBACK_START_SURVEY = "survey:start"
CALLBACK_KCAL_OK = "survey:kcal_ok"
CALLBACK_KCAL_EDIT = "survey:kcal_edit"

BTN_CAT_PEOPLE = "👤 Люди (ккал)"
BTN_CAT_ROBOTS = "🤖 Роботы (Вт/ч)"
BTN_CAT_NEURAL = "🧠 Нейросети (токены)"

BTN_GENDER_MALE = "👨 Мужской"
BTN_GENDER_FEMALE = "👩 Женский"

BTN_ACT_SEDENTARY = "🪑 Сидячий образ жизни"
BTN_ACT_LIGHT = "🚶 Лёгкая активность"
BTN_ACT_MODERATE = "🏃 Умеренная активность"
BTN_ACT_HIGH = "🏋️ Высокая активность"
BTN_ACT_VERY_HIGH = "⚡ Очень высокая активность"

BTN_GOAL_LOSS = "📉 Похудение"
BTN_GOAL_GAIN = "📈 Набор веса"
BTN_GOAL_MAINTAIN = "⚖️ Просто отслеживание"

BTN_TZ_LOCATION = "📍 Поделиться локацией"
BTN_TZ_MOSCOW = "🏙 Москва / СПб (UTC+3)"
BTN_TZ_EKAT = "🏔 Екатеринбург (UTC+5)"
BTN_TZ_VLAD = "🌊 Владивосток (UTC+10)"
BTN_TZ_OTHER = "🌍 Другой город..."

BTN_KCAL_OK = "✅ Подтвердить"
BTN_KCAL_EDIT = "✏️ Редактировать"

GENDER_BY_BTN: dict[str, str] = {
    BTN_GENDER_MALE: "male",
    BTN_GENDER_FEMALE: "female",
}

ACTIVITY_BY_BTN: dict[str, float] = {
    BTN_ACT_SEDENTARY: 1.2,
    BTN_ACT_LIGHT: 1.375,
    BTN_ACT_MODERATE: 1.55,
    BTN_ACT_HIGH: 1.725,
    BTN_ACT_VERY_HIGH: 1.9,
}

GOAL_BY_BTN: dict[str, str] = {
    BTN_GOAL_LOSS: "weight_loss",
    BTN_GOAL_GAIN: "muscle_gain",
    BTN_GOAL_MAINTAIN: "maintain",
}

TIMEZONE_BY_BTN: dict[str, str] = {
    BTN_TZ_MOSCOW: "Europe/Moscow",
    BTN_TZ_EKAT: "Asia/Yekaterinburg",
    BTN_TZ_VLAD: "Asia/Vladivostok",
}

GOAL_KCAL_FACTOR: dict[str, float] = {
    "weight_loss": 0.85,
    "muscle_gain": 1.15,
    "maintain": 1.0,
}

KCAL_FORMULA_MIN = 1200
KCAL_FORMULA_MAX = 5000
KCAL_EDIT_MIN = 800
KCAL_EDIT_MAX = 10_000
KCAL_FALLBACK_DEFAULT = 2000

WELCOME_TEXT = (
    "👋 Привет! Я NutriClick — твой помощник по учёту калорий.\n"
    "\n"
    "Помогаю легко следить за тем, что ты ешь за день: сколько уже съедено, "
    "сколько осталось до цели и из чего складывается рацион.\n"
    "\n"
    "<b>✨ Что умею</b>\n"
    "\n"
    "<b>🍽 Быстрый учёт еды</b>\n"
    "\n"
    "Пришли фото блюда или просто напиши, что съел — я распознаю еду "
    "и посчитаю калории и БЖУ.\n"
    "\n"
    "<b>📒 Дневник питания</b>\n"
    "\n"
    "Все приёмы пищи за день — в одном удобном месте. Можно смотреть прогресс, "
    "менять и удалять записи.\n"
    "\n"
    "<b>🔔 Напоминания</b>\n"
    "\n"
    "Напомню позавтракать, пообедать или принять витамины — "
    "чтобы ничего важного не выпало из дня.\n"
    "\n"
    "<b>📊 Прогресс за день</b>\n"
    "\n"
    "Сразу видно: сколько калорий съедено, сколько осталось до цели "
    "и баланс белков, жиров и углеводов.\n"
    "\n"
    "<b>🚀 С чего начать</b>\n"
    "\n"
    "Просто отправь фото еды или напиши название блюда — "
    "и запись появится в дневнике.\n"
    "\n"
    "<i>Приятного учёта! 💚</i>\n"
    "\n"
    "Для старта пройди короткий опрос — подберём целевые ккал "
    "под твои актуальные цели"
)

TIMEZONE_PROMPT_TEXT = (
    "🌍 Укажи свой часовой пояс:\n"
    "Это нужно, чтобы бот точно знал, когда у тебя наступает новый день "
    "(в 4:00 утра) и присылал напоминания вовремя.\n"
    "\n"
    "📍 Поделиться локацией — быстро и просто. "
    "Координаты не храним: они нужны только чтобы определить часовой пояс"
)

# Колбэк после успешного опроса: (message, state, profile_dict) → сохранить + «Распознать».
OnSurveyCompleteCallback = Callable[
    [Message, FSMContext, dict[str, Any]],
    Awaitable[None],
]
#endregion

#region FSM, Gemini, гео
# Состояния первичного опроса (приветствие → профиль → timezone → ккал).
class SurveyFlow(StatesGroup):
    welcome = State()
    category = State()
    name = State()
    gender = State()
    height = State()
    weight = State()
    age = State()
    activity = State()
    goal = State()
    timezone = State()
    timezone_city = State()
    calories_confirm = State()
    calories_edit = State()


# Схема ответа Gemini для fallback-расчёта целевых ккал.
class DailyCaloriesResult(BaseModel):
    daily_calories: int = Field(description="Целевые ккал в сутки")


router = Router(name="initial_survey")
_on_survey_complete: OnSurveyCompleteCallback | None = None
_gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
_timezone_finder = TimezoneFinder()
_geolocator = Nominatim(user_agent="NutriClickBot/1.0")
#endregion

#region Клавиатуры
# Inline-кнопка старта опроса под приветственным сообщением.
def kb_start_survey() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=BTN_START_SURVEY, callback_data=CALLBACK_START_SURVEY)]
        ]
    )


# Выбор категории учёта (Люди / Роботы / Нейросети).
def kb_category() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_CAT_PEOPLE)],
            [KeyboardButton(text=BTN_CAT_ROBOTS)],
            [KeyboardButton(text=BTN_CAT_NEURAL)],
        ],
        resize_keyboard=True,
    )


# Выбор пола.
def kb_gender() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_GENDER_MALE)],
            [KeyboardButton(text=BTN_GENDER_FEMALE)],
        ],
        resize_keyboard=True,
    )


# Выбор уровня физической активности.
def kb_activity() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_ACT_SEDENTARY)],
            [KeyboardButton(text=BTN_ACT_LIGHT)],
            [KeyboardButton(text=BTN_ACT_MODERATE)],
            [KeyboardButton(text=BTN_ACT_HIGH)],
            [KeyboardButton(text=BTN_ACT_VERY_HIGH)],
        ],
        resize_keyboard=True,
    )


# Выбор направления отслеживания (цель).
def kb_goal() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_GOAL_LOSS)],
            [KeyboardButton(text=BTN_GOAL_GAIN)],
            [KeyboardButton(text=BTN_GOAL_MAINTAIN)],
        ],
        resize_keyboard=True,
    )


# Выбор часового пояса: локация первой, затем популярные регионы и город.
# Используется шагом SurveyFlow.timezone.
def kb_timezone() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_TZ_LOCATION, request_location=True)],
            [KeyboardButton(text=BTN_TZ_MOSCOW), KeyboardButton(text=BTN_TZ_EKAT)],
            [KeyboardButton(text=BTN_TZ_VLAD), KeyboardButton(text=BTN_TZ_OTHER)],
        ],
        resize_keyboard=True,
    )


# Inline «Подтвердить / Редактировать» под сообщением с рассчитанными ккал.
# Используется шагом SurveyFlow.calories_confirm.
def kb_calories_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=BTN_KCAL_EDIT, callback_data=CALLBACK_KCAL_EDIT),
                InlineKeyboardButton(text=BTN_KCAL_OK, callback_data=CALLBACK_KCAL_OK),
            ]
        ]
    )
#endregion

#region Валидация и хелперы шагов
# Парсит положительное число (рост/вес); возвращает float или None.
# Используется хендлерами height / weight.
def parse_positive_float(text: str) -> float | None:
    raw = (text or "").strip().replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        return None
    if value <= 0:
        return None
    return value


# Парсит положительный целый возраст; возвращает int или None.
# Используется хендлером age.
def parse_positive_int(text: str) -> int | None:
    raw = (text or "").strip()
    if not raw.isdigit():
        return None
    value = int(raw)
    if value <= 0:
        return None
    return value


# Собирает dict профиля из FSM-данных для set_profile / on_complete.
# Используется при завершении опроса (после подтверждения ккал).
def profile_from_state_data(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "first_name": data["survey_first_name"],
        "gender": data["survey_gender"],
        "height": data["survey_height"],
        "weight": data["survey_weight"],
        "age": data["survey_age"],
        "activity_level": data["survey_activity_level"],
        "goal": data["survey_goal"],
        "timezone": data["survey_timezone"],
        "daily_calories": data["survey_daily_calories"],
    }


# IANA timezone по координатам (координаты никуда не сохраняются).
# Используется обработчиком локации и resolve_timezone_from_city.
def resolve_timezone_from_coords(lat: float, lon: float) -> str | None:
    try:
        tz_name = _timezone_finder.timezone_at(lat=lat, lng=lon)
    except Exception:
        return None
    return tz_name or None


# IANA timezone по названию города через Nominatim + timezonefinder (sync).
# Используется resolve_timezone_from_city_async.
def resolve_timezone_from_city(city_name: str) -> str | None:
    try:
        location = _geolocator.geocode(city_name, language="ru", timeout=10)
    except Exception:
        return None
    if location is None:
        return None
    return resolve_timezone_from_coords(float(location.latitude), float(location.longitude))


# Async-обёртка geocode, чтобы не блокировать event loop.
# Используется шагом SurveyFlow.timezone_city.
async def resolve_timezone_from_city_async(city_name: str) -> str | None:
    return await asyncio.to_thread(resolve_timezone_from_city, city_name)


# Mifflin–St Jeor + коэффициент активности + корректировка по цели.
# Используется перед шагом calories_confirm; при сбое/выходе за диапазон — Gemini.
def calculate_daily_calories(
    gender: str,
    age: int,
    height_cm: float,
    weight_kg: float,
    activity_level: float,
    goal: str,
) -> int:
    if gender == "male":
        bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age + 5
    else:
        bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age - 161
    tdee = bmr * float(activity_level)
    factor = GOAL_KCAL_FACTOR.get(goal, 1.0)
    calories = int(round(tdee * factor / 10.0) * 10)
    return max(calories, 1)


# Fallback через Gemini, если формула дала неразумный результат.
# В промпте: при явно неверных входах вернуть среднее нормальное (~2000).
def estimate_daily_calories_gemini(
    gender: str,
    age: int,
    height_cm: float,
    weight_kg: float,
    activity_level: float,
    goal: str,
) -> int | None:
    if _gemini_client is None:
        return None
    prompt = (
        "Оцени целевые калории в сутки для пользователя по профилю.\n"
        f"gender={gender}, age={age}, height_cm={height_cm}, weight_kg={weight_kg}, "
        f"activity_level={activity_level}, goal={goal}.\n"
        "Верни JSON строго по схеме с полем daily_calories (целое).\n"
        "Если входные значения явно неверные или абсурдные — верни среднее "
        "нормальное значение ккал (около 2000), а не экстремум."
    )
    for model_name in MODELS_QUEUE:
        try:
            response = _gemini_client.models.generate_content(
                model=model_name,
                contents=[prompt],
                config={
                    "response_mime_type": "application/json",
                    "response_schema": DailyCaloriesResult,
                    "http_options": {
                        "timeout": REQUEST_TIMEOUT_MS,
                        "retry_options": {"attempts": 1},
                    },
                },
            )
            raw = response.text or ""
            parsed = json.loads(raw)
            value = int(parsed.get("daily_calories", 0))
            if value > 0:
                return value
        except Exception as e:
            print(f"survey kcal Gemini fallback ({model_name}): {e}")
    return None


# Считает ккал формулой; вне 1200–5000 или при ошибке — Gemini, иначе ~2000.
# Используется proceed_to_calories_step.
def resolve_recommended_calories(data: dict[str, Any]) -> int:
    gender = str(data["survey_gender"])
    age = int(data["survey_age"])
    height_cm = float(data["survey_height"])
    weight_kg = float(data["survey_weight"])
    activity_level = float(data["survey_activity_level"])
    goal = str(data["survey_goal"])
    try:
        calories = calculate_daily_calories(
            gender, age, height_cm, weight_kg, activity_level, goal
        )
        if KCAL_FORMULA_MIN <= calories <= KCAL_FORMULA_MAX:
            return calories
    except Exception as e:
        print(f"survey kcal formula failed: {e}")
        calories = None
    gemini_value = estimate_daily_calories_gemini(
        gender, age, height_cm, weight_kg, activity_level, goal
    )
    if gemini_value is not None and gemini_value > 0:
        return gemini_value
    return KCAL_FALLBACK_DEFAULT


# Сохраняет timezone и переходит к шагу подтверждения ккал.
# Используется хендлерами локации / популярных поясов / города.
async def proceed_to_calories_step(
    message: Message,
    state: FSMContext,
    timezone_name: str,
) -> None:
    await state.update_data(survey_timezone=timezone_name)
    data = await state.get_data()
    # Формула быстрая; Gemini-fallback — sync HTTP, не блокируем loop.
    calories = await asyncio.to_thread(resolve_recommended_calories, data)
    await state.update_data(survey_daily_calories=calories)
    await state.set_state(SurveyFlow.calories_confirm)
    # Снимаем reply-клавиатуру отдельным сообщением (inline нельзя совместить с Remove).
    stub = await message.answer("\u2060", reply_markup=ReplyKeyboardRemove())
    try:
        await stub.delete()
    except Exception:
        pass
    await message.answer(
        f"Рассчитали, что для вас наиболее подходящим будет значение "
        f"<b>{calories}</b> ккал в сутки\n"
        "\n"
        "Подтверждаете?",
        parse_mode="HTML",
        reply_markup=kb_calories_confirm(),
    )


# Завершает опрос через on_complete с собранным профилем.
# user_id нужен из callback (у callback.message.from_user — бот).
# Используется после подтверждения или ручного ввода ккал.
async def finish_survey(
    message: Message,
    state: FSMContext,
    *,
    user_id: int | None = None,
) -> None:
    data = await state.get_data()
    profile = profile_from_state_data(data)
    uid = user_id if user_id is not None else (
        message.from_user.id if message.from_user else 0
    )
    profile["user_id"] = uid
    if _on_survey_complete is None:
        await state.clear()
        await message.answer(
            "Опрос завершён. Главное меню пока не подключено",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    await _on_survey_complete(message, state, profile)
#endregion

#region Публичный API
# Запускает первичный опрос: приветствие + кнопка «Начать короткий опрос».
# Используется в main.py из /start (при INITIAL_SURVEY_ENABLED) и позже — при
# «Обновить данные пользователя» / первом запуске по флагу в БД.
async def start_initial_survey(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(SurveyFlow.welcome)
    # Снимаем старую Reply-клавиатуру (inline её не убирает).
    stub = await message.answer("\u2060", reply_markup=ReplyKeyboardRemove())
    try:
        await stub.delete()
    except Exception:
        pass
    await message.answer(
        WELCOME_TEXT,
        parse_mode="HTML",
        reply_markup=kb_start_survey(),
    )


# Подключает колбэк завершения опроса и возвращает router.
# Используется в main.py: setup_initial_survey(on_complete=...).
def setup_initial_survey(
    on_complete: OnSurveyCompleteCallback | None = None,
) -> Router:
    global _on_survey_complete
    _on_survey_complete = on_complete
    return router
#endregion

#region Хендлеры шагов
# Старт опроса по inline-кнопке под приветствием → категория учёта.
@router.callback_query(SurveyFlow.welcome, F.data == CALLBACK_START_SURVEY)
async def on_start_survey(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    await state.set_state(SurveyFlow.category)
    target = callback.message
    if target is None:
        return
    await target.answer(
        "Выберите свою категорию учёта",
        reply_markup=kb_category(),
    )


# Категория «Люди» → переход к созданию аккаунта (имя).
@router.message(SurveyFlow.category, F.text == BTN_CAT_PEOPLE)
async def on_category_people(message: Message, state: FSMContext) -> None:
    await state.set_state(SurveyFlow.name)
    await message.answer(
        "Как вас зовут? Напишите имя",
        reply_markup=ReplyKeyboardRemove(),
    )


# Категории «Роботы» / «Нейросети» — пока недоступны, остаёмся на шаге.
@router.message(SurveyFlow.category, F.text.in_({BTN_CAT_ROBOTS, BTN_CAT_NEURAL}))
async def on_category_stub(message: Message, state: FSMContext) -> None:
    await message.answer(
        "Извините, данный функционал ещё находится в разработке",
        reply_markup=kb_category(),
    )


# Имя (текст) → вопрос про пол.
@router.message(SurveyFlow.name, F.text)
async def on_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Напишите имя текстом")
        return
    if len(name) > 100:
        await message.answer("Имя слишком длинное — до 100 символов")
        return
    await state.update_data(survey_first_name=name)
    await state.set_state(SurveyFlow.gender)
    await message.answer(
        "Укажите <b>пол</b>",
        reply_markup=kb_gender(),
        parse_mode="HTML",
    )


# Пол → вопрос про рост.
@router.message(SurveyFlow.gender, F.text.in_(set(GENDER_BY_BTN)))
async def on_gender(message: Message, state: FSMContext) -> None:
    gender = GENDER_BY_BTN[message.text or ""]
    await state.update_data(survey_gender=gender)
    await state.set_state(SurveyFlow.height)
    await message.answer(
        "Укажите <b>рост</b> в сантиметрах (например, 178)",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="HTML",
    )


# Рост → вопрос про вес.
@router.message(SurveyFlow.height, F.text)
async def on_height(message: Message, state: FSMContext) -> None:
    height = parse_positive_float(message.text or "")
    if height is None or height < 50 or height > 300:
        await message.answer(
            "Введите <b>рост</b> числом в см (например, 178)",
            parse_mode="HTML",
        )
        return
    await state.update_data(survey_height=height)
    await state.set_state(SurveyFlow.weight)
    await message.answer(
        "Укажите <b>вес</b> в килограммах (например, 75)",
        parse_mode="HTML",
    )


# Вес → вопрос про возраст.
@router.message(SurveyFlow.weight, F.text)
async def on_weight(message: Message, state: FSMContext) -> None:
    weight = parse_positive_float(message.text or "")
    if weight is None or weight < 20 or weight > 400:
        await message.answer(
            "Введите <b>вес</b> числом в кг (например, 75)",
            parse_mode="HTML",
        )
        return
    await state.update_data(survey_weight=weight)
    await state.set_state(SurveyFlow.age)
    await message.answer(
        "Укажите <b>возраст</b> (полных лет)",
        parse_mode="HTML",
    )


# Возраст → вопрос про активность.
@router.message(SurveyFlow.age, F.text)
async def on_age(message: Message, state: FSMContext) -> None:
    age = parse_positive_int(message.text or "")
    if age is None or age < 10 or age > 120:
        await message.answer("Введите возраст целым числом (например, 28)")
        return
    await state.update_data(survey_age=age)
    await state.set_state(SurveyFlow.activity)
    await message.answer(
        "Какой у вас уровень физической активности?",
        reply_markup=kb_activity(),
    )


# Активность → вопрос про цель отслеживания.
@router.message(SurveyFlow.activity, F.text.in_(set(ACTIVITY_BY_BTN)))
async def on_activity(message: Message, state: FSMContext) -> None:
    level = ACTIVITY_BY_BTN[message.text or ""]
    await state.update_data(survey_activity_level=level)
    await state.set_state(SurveyFlow.goal)
    await message.answer(
        "Выберите направление отслеживания",
        reply_markup=kb_goal(),
    )


# Цель → bridge-текст и шаг часового пояса (on_complete ещё не вызываем).
@router.message(SurveyFlow.goal, F.text.in_(set(GOAL_BY_BTN)))
async def on_goal(message: Message, state: FSMContext) -> None:
    goal = GOAL_BY_BTN[message.text or ""]
    await state.update_data(survey_goal=goal)
    await state.set_state(SurveyFlow.timezone)
    await message.answer("Отлично, ещё пара вопросов:")
    await message.answer(
        TIMEZONE_PROMPT_TEXT,
        reply_markup=kb_timezone(),
    )


# Популярный регион → IANA timezone → шаг ккал.
@router.message(SurveyFlow.timezone, F.text.in_(set(TIMEZONE_BY_BTN)))
async def on_timezone_preset(message: Message, state: FSMContext) -> None:
    tz_name = TIMEZONE_BY_BTN[message.text or ""]
    await proceed_to_calories_step(message, state, tz_name)


# «Другой город...» → ждём название города текстом.
@router.message(SurveyFlow.timezone, F.text == BTN_TZ_OTHER)
async def on_timezone_other(message: Message, state: FSMContext) -> None:
    await state.set_state(SurveyFlow.timezone_city)
    await message.answer(
        "Напиши название своего города (например: Самара или Тбилиси)",
        reply_markup=ReplyKeyboardRemove(),
    )


# Локация → timezonefinder (координаты не сохраняем) → шаг ккал.
@router.message(SurveyFlow.timezone, F.location)
async def on_timezone_location(message: Message, state: FSMContext) -> None:
    loc = message.location
    if loc is None:
        await message.answer(
            "Не удалось прочитать локацию — выберите кнопку или город",
            reply_markup=kb_timezone(),
        )
        return
    tz_name = resolve_timezone_from_coords(float(loc.latitude), float(loc.longitude))
    if not tz_name:
        await message.answer(
            "Не удалось определить пояс по локации — выберите регион или город",
            reply_markup=kb_timezone(),
        )
        return
    await proceed_to_calories_step(message, state, tz_name)


# Название города → geocode + timezonefinder → шаг ккал.
@router.message(SurveyFlow.timezone_city, F.text)
async def on_timezone_city(message: Message, state: FSMContext) -> None:
    city = (message.text or "").strip()
    if not city:
        await message.answer("Напишите название города текстом")
        return
    if len(city) > 100:
        await message.answer("Слишком длинное название — до 100 символов")
        return
    searching = await message.answer("Ищем город…")
    tz_name = await resolve_timezone_from_city_async(city)
    if not tz_name:
        await state.set_state(SurveyFlow.timezone)
        try:
            await searching.delete()
        except Exception:
            pass
        await message.answer(
            "Не нашли такой город — попробуйте ещё раз или выберите кнопку",
            reply_markup=kb_timezone(),
        )
        return
    try:
        await searching.edit_text("Часовой пояс установлен ✅")
    except Exception:
        await message.answer("Часовой пояс установлен ✅")
    await proceed_to_calories_step(message, state, tz_name)


# Inline «Подтвердить» рассчитанные ккал → завершение опроса.
@router.callback_query(SurveyFlow.calories_confirm, F.data == CALLBACK_KCAL_OK)
async def on_kcal_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    target = callback.message
    if target is None:
        return
    try:
        await target.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    uid = callback.from_user.id if callback.from_user else 0
    await finish_survey(target, state, user_id=uid)


# Inline «Редактировать» → ручной ввод целевых ккал.
@router.callback_query(SurveyFlow.calories_confirm, F.data == CALLBACK_KCAL_EDIT)
async def on_kcal_edit(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    target = callback.message
    if target is None:
        return
    try:
        await target.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await state.set_state(SurveyFlow.calories_edit)
    await target.answer("Введите своё целое число ккал в сутки (например, 2000)")


# Ручной ввод ккал → валидация → завершение опроса.
@router.message(SurveyFlow.calories_edit, F.text)
async def on_kcal_edit_value(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Введите целое положительное число ккал")
        return
    value = int(raw)
    if value < KCAL_EDIT_MIN or value > KCAL_EDIT_MAX:
        await message.answer(
            f"Введите число от {KCAL_EDIT_MIN} до {KCAL_EDIT_MAX}"
        )
        return
    await state.update_data(survey_daily_calories=value)
    await finish_survey(message, state)


# Фото во время любого шага опроса — не запускаем распознавание.
@router.message(StateFilter(SurveyFlow), F.photo)
async def on_survey_photo(message: Message, state: FSMContext) -> None:
    await message.answer("Сначала закончите короткий опрос")


# Неверный ввод на шагах с кнопками — короткая подсказка.
@router.message(SurveyFlow.welcome, F.text)
async def on_welcome_other(message: Message, state: FSMContext) -> None:
    await message.answer(
        "Нажмите кнопку под приветствием, чтобы начать короткий опрос"
    )


@router.message(SurveyFlow.category, F.text)
async def on_category_other(message: Message, state: FSMContext) -> None:
    await message.answer(
        "Выберите категорию кнопкой ниже",
        reply_markup=kb_category(),
    )


@router.message(SurveyFlow.gender, F.text)
async def on_gender_other(message: Message, state: FSMContext) -> None:
    await message.answer(
        "Выберите <b>пол</b> кнопкой ниже",
        reply_markup=kb_gender(),
        parse_mode="HTML",
    )


@router.message(SurveyFlow.activity, F.text)
async def on_activity_other(message: Message, state: FSMContext) -> None:
    await message.answer(
        "Выберите уровень активности кнопкой ниже",
        reply_markup=kb_activity(),
    )


@router.message(SurveyFlow.goal, F.text)
async def on_goal_other(message: Message, state: FSMContext) -> None:
    await message.answer(
        "Выберите направление кнопкой ниже",
        reply_markup=kb_goal(),
    )


@router.message(SurveyFlow.timezone, F.text)
async def on_timezone_other_text(message: Message, state: FSMContext) -> None:
    await message.answer(
        "Выберите кнопку ниже, поделитесь локацией или укажите другой город",
        reply_markup=kb_timezone(),
    )


@router.message(SurveyFlow.calories_confirm, F.text)
async def on_calories_confirm_text(message: Message, state: FSMContext) -> None:
    await message.answer(
        "Нажмите кнопку под сообщением: подтвердить или редактировать"
    )
#endregion
