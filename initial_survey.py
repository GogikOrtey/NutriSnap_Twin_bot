"""
initial_survey.py — первичный опрос (онбординг) NutriSnap.

Зачем нужен файл
----------------
FSM-флоу стартового опроса: приветствие, категория учёта, профиль, цель.
Подключается из main.py через setup_initial_survey(on_complete=...).
При активном флаге INITIAL_SURVEY_ENABLED /start ведёт сюда вместо главного меню.

Позже: проверка в БД, прошёл ли пользователь опрос; расчёт целевых ккал.
"""

from __future__ import annotations

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

#region Константы кнопок и тексты
BTN_START_SURVEY = "🚀 Начать короткий опрос"
CALLBACK_START_SURVEY = "survey:start"

BTN_CAT_PEOPLE = "👤 Люди (ккал)"
BTN_CAT_ROBOTS = "🤖 Роботы (Вт/ч)"
BTN_CAT_NEURAL = "🧠 Нейросети (токены)"

BTN_GENDER_MALE = "👨 Мужской"
BTN_GENDER_FEMALE = "👩 Женский"

BTN_ACT_SEDENTARY = "Сидячий образ жизни"
BTN_ACT_LIGHT = "Лёгкая активность"
BTN_ACT_MODERATE = "Умеренная активность"
BTN_ACT_HIGH = "Высокая активность"
BTN_ACT_VERY_HIGH = "Очень высокая активность"

BTN_GOAL_LOSS = "📉 Похудение"
BTN_GOAL_GAIN = "📈 Набор веса"
BTN_GOAL_MAINTAIN = "⚖️ Просто отслеживание"

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

# Колбэк после успешного опроса: (message, state, profile_dict) → сохранить + «Распознать».
OnSurveyCompleteCallback = Callable[
    [Message, FSMContext, dict[str, Any]],
    Awaitable[None],
]
#endregion

#region FSM и Router
# Состояния первичного опроса (приветствие → профиль → цель).
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


router = Router(name="initial_survey")
_on_survey_complete: OnSurveyCompleteCallback | None = None
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


# Собирает dict профиля из FSM-данных для stub_set_profile / on_complete.
# Используется при завершении опроса (после выбора цели).
def profile_from_state_data(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "first_name": data["survey_first_name"],
        "gender": data["survey_gender"],
        "height": data["survey_height"],
        "weight": data["survey_weight"],
        "age": data["survey_age"],
        "activity_level": data["survey_activity_level"],
        "goal": data["survey_goal"],
    }
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
        "Укажите пол",
        reply_markup=kb_gender(),
    )


# Пол → вопрос про рост.
@router.message(SurveyFlow.gender, F.text.in_(set(GENDER_BY_BTN)))
async def on_gender(message: Message, state: FSMContext) -> None:
    gender = GENDER_BY_BTN[message.text or ""]
    await state.update_data(survey_gender=gender)
    await state.set_state(SurveyFlow.height)
    await message.answer(
        "Укажите рост в сантиметрах (например, 178)",
        reply_markup=ReplyKeyboardRemove(),
    )


# Рост → вопрос про вес.
@router.message(SurveyFlow.height, F.text)
async def on_height(message: Message, state: FSMContext) -> None:
    height = parse_positive_float(message.text or "")
    if height is None or height < 50 or height > 300:
        await message.answer("Введите рост числом в см (например, 178)")
        return
    await state.update_data(survey_height=height)
    await state.set_state(SurveyFlow.weight)
    await message.answer("Укажите вес в килограммах (например, 75)")


# Вес → вопрос про возраст.
@router.message(SurveyFlow.weight, F.text)
async def on_weight(message: Message, state: FSMContext) -> None:
    weight = parse_positive_float(message.text or "")
    if weight is None or weight < 20 or weight > 400:
        await message.answer("Введите вес числом в кг (например, 75)")
        return
    await state.update_data(survey_weight=weight)
    await state.set_state(SurveyFlow.age)
    await message.answer("Укажите возраст (полных лет)")


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


# Цель → сохранение профиля через on_complete (экран «Распознать», ждём фото/текст).
@router.message(SurveyFlow.goal, F.text.in_(set(GOAL_BY_BTN)))
async def on_goal(message: Message, state: FSMContext) -> None:
    goal = GOAL_BY_BTN[message.text or ""]
    await state.update_data(survey_goal=goal)
    data = await state.get_data()
    profile = profile_from_state_data(data)
    if _on_survey_complete is None:
        await state.clear()
        await message.answer(
            "Опрос завершён. Главное меню пока не подключено",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    await _on_survey_complete(message, state, profile)


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
        "Выберите пол кнопкой ниже",
        reply_markup=kb_gender(),
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
#endregion
