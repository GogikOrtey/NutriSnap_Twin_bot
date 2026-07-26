"""
initial_survey.py — первичный опрос (онбординг) NutriSnap.

Зачем нужен файл
----------------
FSM-флоу стартового опроса: знакомство, категория учёта, профиль, цель и т.д.
Подключается из main.py через setup_initial_survey(). При активном флаге
INITIAL_SURVEY_ENABLED /start ведёт сюда вместо главного меню.

Пока — заглушка («Первый вопрос…»). Позже: проверка в БД, прошёл ли пользователь опрос.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, ReplyKeyboardRemove

#region FSM и Router
# Состояния первичного опроса (пока только заглушка первого шага).
class SurveyFlow(StatesGroup):
    q1 = State()


router = Router(name="initial_survey")
#endregion


#region Публичный API
# Запускает первичный опрос: сброс FSM и показ заглушки первого вопроса.
# Используется в main.py из /start (при INITIAL_SURVEY_ENABLED) и позже — при
# «Обновить данные пользователя» / первом запуске по флагу в БД.
async def start_initial_survey(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(SurveyFlow.q1)
    await message.answer(
        "Первый вопрос…",
        reply_markup=ReplyKeyboardRemove(),
    )


# Подключает router первичного опроса. Используется в main.py.
def setup_initial_survey() -> Router:
    return router
#endregion
