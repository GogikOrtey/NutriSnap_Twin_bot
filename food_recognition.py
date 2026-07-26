"""
food_recognition.py — FSM-флоу распознавания еды для NutriSnap.

Зачем нужен файл
----------------
Вся логика учёта калорий по фото/тексту: промпты и вызовы Gemini, нормализация
результата, confirm UI (✏️/✅/⚖️ + автоподтверждение), меню правок и связанные
хендлеры. Подключается из main.py через setup_food_recognition(storage).

Как устроен файл (блоки сверху вниз)
------------------------------------

1. Импорты и конфиг (таймауты, очередь моделей, callback_data, тексты кнопок).
2. Промпты PHOTO_PROMPT / TEXT_PROMPT.
3. FoodFlow (FSM), FoodResult (Pydantic-схема), клиент Gemini, Router.
4. Анализ через Gemini (_generate_with_fallback, analyze_food_photo/text).
5. Нормализация и форматирование результата.
6. Клавиатуры confirm / label / cancel / edit-menu (+ «🏠 Главное меню»).
7. Утилиты (save_to_console, persist_confirmed_food, download_photo_temp, parse_food_result).
8. Confirm UI (show/finalize/schedule/handle_ai_result).
9. Хендлеры: фото, подсказка, вес, текст, callbacks, меню правок.
10. setup_food_recognition — MemoryStorage + тексты меню / «🏠» + on_food_saved; возвращает router.
    Reply «🏠» скрыта с отправки фото/текста до ✅ или отмены после «✏️ Изменить».
    После ✅/авто-✅ вызывается on_food_saved (в main — триггер reminders).
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
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
from google import genai
from pydantic import BaseModel, Field

#region Конфиг
load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Таймаут одного запроса к модели (мс). При истечении — переход к следующей модели в очереди.
REQUEST_TIMEOUT_MS = 10_000

# Таймаут автоподтверждения результата пользователем (сек).
CONFIRM_TIMEOUT_SEC = 10

# Стандартная порция (г), если на этикетке не указан объём.
DEFAULT_PORTION_G = 100.0

# Мин. ширина превью (символов) с кнопками ✏️/✅ — иначе кнопки длиннее сообщения.
MIN_CONFIRM_MSG_WIDTH = 30
# Прозрачный символ-паддинг ширины (braille blank); при ✅ снимается вместе с кнопками.
WIDTH_PAD_CHAR = "⠀"

# Очередь моделей для попыток (fallback при ошибке/таймауте).
MODELS_QUEUE = [
    "gemini-flash-latest",
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash",
]

CALLBACK_CONFIRM = "food:confirm"
CALLBACK_EDIT = "food:edit"
CALLBACK_WEIGHT = "food:weight"
CALLBACK_CANCEL = "food:cancel"

# Тексты кнопок reply-клавиатуры меню «✏️ Изменить».
BTN_EDIT_WEIGHT = "✏️ Изменить вес порции"
BTN_EDIT_HINT = "➕ Дополнить или уточнить описание блюда"
BTN_EDIT_REPLACE = "🔄 Заменить описание или фото"
BTN_EDIT_DISCARD = "❌ Не сохранять этот результат"
#endregion

#region Промпты
PHOTO_PROMPT = """Ты анализируешь фото для учёта калорий.

Верни JSON строго по схеме. Выбери ровно один status:
- recognized — на фото обычная еда, уверенно оцени dish + calories + БЖУ (proteins/fats/carbs).
  portion_g — ВСЕГДА оцени примерный вес порции на фото в граммах (визуально).
  portion_known=false, is_label=false.
  emoji — один наиболее подходящий эмодзи для этого блюда (слева от названия).
- unclear — на фото похоже на еду, но уверенности нет: dish/calories/БЖУ/emoji можно оставить null.
- no_food — еды нет, или этикетка без читаемых данных о ккал.
- label — на фото этикетка/состав с читаемыми ккал (и опционально КБЖУ).
  is_label=true. Если указано «на 100 г» / «на порцию X г» — заполни portion_g и portion_known=true.
  Если объём не указан — portion_known=false, portion_g можно не заполнять.
  emoji — один эмодзи продукта/категории с этикетки (если dish известен).
- bad_desc — используй ТОЛЬКО если пользователь дал текстовую подсказку, и она явно
  противоречит фото / мешает классификации.

emoji: ровно один символ-эмодзи (например 🍳, 🥗, 🍕, ☕️), без текста и без пробелов.
  Обязателен при recognized/label, когда заполнен dish.

Числа: calories — целое ккал; proteins/fats/carbs — граммы (float); portion_g — граммы (float)."""

TEXT_PROMPT = """Пользователь описывает еду или калории текстом (без фото).

Верни JSON строго по схеме:
- status=recognized, если удалось извлечь/оценить калории (и по возможности dish + БЖУ).
- status=unclear, если текста недостаточно для оценки.
- status=bad_desc, если текст явно бессмысленный для учёта еды.
- status=no_food / label не используй для чистого текста.

Заполни calories (целое). dish и БЖУ — если возможно.
emoji — один наиболее подходящий эмодзи для блюда (слева от названия); обязателен,
если заполнен dish. Ровно один символ-эмодзи, без текста.
portion_g — ВСЕГДА: если пользователь указал вес — используй его; иначе оцени
типичную/среднюю порцию такого блюда в граммах.
is_label=false, portion_known=false."""
#endregion

#region Схема ответа и роутер
client = genai.Client(api_key=GEMINI_API_KEY)
router = Router(name="food_recognition")

# MemoryStorage из main.py — нужен таймеру автоподтверждения (schedule_auto_confirm).
_storage: MemoryStorage | None = None

# Тексты кнопок главного меню (из main.py) — не отправлять в Gemini как описание еды.
_menu_button_texts: frozenset[str] = frozenset()

# Текст «🏠 Главное меню» из main.py — скрываем на время анализа/confirm, показываем в «✏️ Изменить».
_main_menu_button_text: str | None = None

# Колбэк после подтверждения еды (main.py → триггер reminders). Сигнатура:
# async (user_id, FoodResult, bot, chat_id) -> None
OnFoodSavedCallback = Callable[[int, "FoodResult", Bot, int], Awaitable[None]]
_on_food_saved: OnFoodSavedCallback | None = None


# Фильтр: текст сообщения не является кнопкой главного меню (проверка на runtime).
# Используется в on_text_food, чтобы меню из main.py не уходило в Gemini.
class NotMenuButtonFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        text = (message.text or "").strip()
        return bool(text) and text not in _menu_button_texts


# FSM-состояния учёта еды: подтверждение, меню правок, подсказка, ввод веса.
# Используется в хендлерах фото/текста/callback и таймере автоподтверждения.
class FoodFlow(StatesGroup):
    confirming = State()
    editing_choice = State()
    waiting_hint = State()
    waiting_weight = State()


# Схема structured output Gemini: статус ветки + КБЖУ/порция.
# Используется в analyze_food_photo / analyze_food_text как response_schema.
class FoodResult(BaseModel):
    status: Literal["recognized", "unclear", "no_food", "label", "bad_desc"] = Field(
        description="Ветка результата распознавания"
    )
    dish: str | None = Field(default=None, description="Название блюда/продукта")
    emoji: str | None = Field(
        default=None,
        description="Один эмодзи, наиболее подходящий для блюда (слева от названия)",
    )
    calories: int | None = Field(default=None, description="Калорийность в ккал")
    proteins: float | None = Field(default=None, description="Белки в граммах")
    fats: float | None = Field(default=None, description="Жиры в граммах")
    carbs: float | None = Field(default=None, description="Углеводы в граммах")
    portion_g: float | None = Field(
        default=None,
        description="Вес порции в граммах (оценка с фото / типичная порция / с этикетки)",
    )
    portion_known: bool = Field(
        default=False, description="True, если объём явно указан на этикетке"
    )
    is_label: bool = Field(default=False, description="True, если это этикетка/состав")
#endregion

#region Анализ ч/з Gemini
# Общий вызов Gemini с JSON-схемой и fallback по MODELS_QUEUE.
# Используется в analyze_food_photo и analyze_food_text.
def _generate_with_fallback(contents: list[Any]) -> str | None:
    response_text = None
    skipped_models: set[str] = set()
    total_attempts = len(MODELS_QUEUE)

    for index, model_name in enumerate(MODELS_QUEUE, start=1):
        if model_name in skipped_models:
            print(
                f"\n[Попытка {index}/{total_attempts}] Пропуск {model_name}: "
                f"уже не ответила в лимите {REQUEST_TIMEOUT_MS // 1000} с."
            )
            continue

        try:
            print(
                f"\n[Попытка {index}/{total_attempts}] Отправка запроса к модели: {model_name} "
                f"(таймаут {REQUEST_TIMEOUT_MS // 1000} с)..."
            )
            response = client.models.generate_content(
                model=model_name,
                contents=contents,
                config={
                    "response_mime_type": "application/json",
                    "response_schema": FoodResult,
                    "http_options": {
                        "timeout": REQUEST_TIMEOUT_MS,
                        "retry_options": {"attempts": 1},
                    },
                },
            )
            response_text = response.text
            print(f"Успешно получено на модели {model_name}!")
            break
        except Exception as e:
            print(f"Попытка {index} не удалась. Ошибка: {e}")
            skipped_models.add(model_name)

    return response_text


# Анализ фото блюда/этикетки через Gemini Files API; опционально с текстовой подсказкой.
# Используется в обработчиках фото и повторного запроса после waiting_hint.
def analyze_food_photo(image_path: str, hint: str | None = None) -> str | None:
    print("Загрузка файла в Google Files API...")
    uploaded_file = client.files.upload(file=image_path)
    print(f"Файл успешно загружен. URI: {uploaded_file.uri}")

    contents: list[Any] = [uploaded_file]
    if hint:
        contents.append(f"Текстовая подсказка пользователя: {hint}")
    contents.append(PHOTO_PROMPT)

    try:
        return _generate_with_fallback(contents)
    finally:
        print("\nУдаление временного файла из Google Cloud...")
        try:
            client.files.delete(name=uploaded_file.name)
        except Exception as e:
            print(f"Не удалось удалить файл из Google Cloud: {e}")


# Анализ текстового описания еды / числа ккал через Gemini (без фото).
# Используется в обработчике текстовой ветки учёта еды.
def analyze_food_text(text: str) -> str | None:
    contents: list[Any] = [
        f"Сообщение пользователя: {text}",
        TEXT_PROMPT,
    ]
    return _generate_with_fallback(contents)
#endregion

#region Результат
# Нормализует результат этикетки: подставляет DEFAULT_PORTION_G, если объём неизвестен.
# Используется перед показом confirm UI для status=label.
def normalize_label_result(result: FoodResult) -> FoodResult:
    data = result.model_dump()
    data["is_label"] = True
    if not data.get("portion_known") or not data.get("portion_g"):
        data["portion_known"] = False
        data["portion_g"] = DEFAULT_PORTION_G
    return FoodResult.model_validate(data)


# Пересчитывает ккал/БЖУ пропорционально новому весу относительно portion_g.
# Используется после ввода точного веса (⚖️) в waiting_weight.
def recalc_by_weight(result: FoodResult, weight_g: float) -> FoodResult:
    base = result.portion_g or DEFAULT_PORTION_G
    if base <= 0:
        base = DEFAULT_PORTION_G
    factor = weight_g / base
    data = result.model_dump()
    if data.get("calories") is not None:
        data["calories"] = int(round(data["calories"] * factor))
    for key in ("proteins", "fats", "carbs"):
        if data.get(key) is not None:
            data[key] = round(float(data[key]) * factor, 1)
    data["portion_g"] = weight_g
    data["portion_known"] = True
    return FoodResult.model_validate(data)


# Эмодзи блюда из FoodResult (fallback 🍽).
# Используется превью подтверждения и отображением названия.
def format_result_emoji(result: FoodResult) -> str:
    emoji = (result.emoji or "").strip()
    return emoji or "🍽"


# Форматирует FoodResult в читаемый блок для пользователя (блюдо + порция + КБЖУ).
# HTML-разметка (<b>); вызывающий код должен слать с parse_mode="HTML".
# Используется при показе превью перед подтверждением.
def format_food_result(result: FoodResult) -> str:
    dish = html.escape(result.dish or "Блюдо")
    emoji = format_result_emoji(result)
    lines = [f"{emoji}  {dish}"]

    # Порция всегда сразу под названием: оценка с фото/типичная или с этикетки.
    portion = result.portion_g
    if result.is_label or result.status == "label":
        portion = portion or DEFAULT_PORTION_G
        if result.portion_known:
            lines.append(f"Порция: <b>{portion:g} г</b>")
        else:
            lines.append(f"Порция: <b>{portion:g} г</b> (стандартная)")
    elif portion is not None:
        lines.append(f"Примерная порция: <b>~{portion:g} г</b>")

    lines.extend(["", "📋 Пищевая ценность:"])
    if result.calories is not None:
        lines.append(f"• 🔥 Калорийность: {result.calories} ккал")
    if result.proteins is not None:
        lines.append(f"• 🥩 Белки: {result.proteins} г")
    if result.fats is not None:
        lines.append(f"• 🥑 Жиры: {result.fats} г")
    if result.carbs is not None:
        lines.append(f"• 🍞 Углеводы: {result.carbs} г")
    if result.is_label or result.status == "label":
        portion = result.portion_g or DEFAULT_PORTION_G
        if result.portion_known:
            lines.append("")
            lines.append(f"Значения рассчитаны на {portion:g} г (как на этикетке)")
        else:
            lines.append("")
            lines.append(
                f"Объём на этикетке не указан — значения для стандартной порции "
                f"{portion:g} г"
            )
    return "\n".join(lines)


# Добивает текст до min_width по самой длинной видимой строке (без HTML-тегов).
# Telegram рисует ширину пузыря по max-строке; если она короче кнопок ✏️/✅ —
# ряд выглядит шире сообщения. Паддинг — WIDTH_PAD_CHAR в первой строке.
# Используется в show_confirm_preview; при подтверждении снимается
# (finalize_confirmed_preview пишет format_food_result без добивки).
def ensure_min_message_width(
    text: str, min_width: int = MIN_CONFIRM_MSG_WIDTH
) -> str:
    lines = text.split("\n")
    if not lines:
        return WIDTH_PAD_CHAR * min_width
    visible_lens = [len(re.sub(r"<[^>]+>", "", line)) for line in lines]
    if max(visible_lens) >= min_width:
        return text
    pad = min_width - visible_lens[0]
    lines[0] = lines[0] + (WIDTH_PAD_CHAR * pad)
    return "\n".join(lines)
#endregion

#region Клавиатуры
# Клавиатура подтверждения обычной еды: ✏️ / ✅.
# Используется в show_confirm_preview для recognized / текстовой ветки.
def build_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✏️ Изменить", callback_data=CALLBACK_EDIT),
                InlineKeyboardButton(text="✅ Подтвердить", callback_data=CALLBACK_CONFIRM),
            ]
        ]
    )


# Клавиатура подтверждения этикетки: ✏️ / ✅ / ⚖️.
# Используется в show_confirm_preview для status=label.
def build_label_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✏️ Изменить", callback_data=CALLBACK_EDIT),
                InlineKeyboardButton(text="✅ Подтвердить", callback_data=CALLBACK_CONFIRM),
            ],
            [
                InlineKeyboardButton(
                    text="⚖️ Указать точный вес", callback_data=CALLBACK_WEIGHT
                ),
            ],
        ]
    )


# Клавиатура отмены ожидания текстовой подсказки.
# Используется при unclear / edit → waiting_hint.
def build_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Отмена", callback_data=CALLBACK_CANCEL)]
        ]
    )


# Reply-клавиатура только с «🏠 Главное меню» (после ✅ / отмены или вместо Remove).
# Используется при завершении флоу и во вводах после меню «✏️ Изменить».
def build_main_menu_only_keyboard() -> ReplyKeyboardMarkup | ReplyKeyboardRemove:
    if not _main_menu_button_text:
        return ReplyKeyboardRemove()
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=_main_menu_button_text)]],
        resize_keyboard=True,
    )


# Reply-клавиатура меню правок после нажатия «✏️ Изменить» (+ «🏠 Главное меню»).
# Используется в on_edit → FoodFlow.editing_choice.
def build_edit_menu_keyboard() -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = [
        [KeyboardButton(text=BTN_EDIT_WEIGHT)],
        [KeyboardButton(text=BTN_EDIT_HINT)],
        [KeyboardButton(text=BTN_EDIT_REPLACE)],
        [KeyboardButton(text=BTN_EDIT_DISCARD)],
    ]
    if _main_menu_button_text:
        rows.append([KeyboardButton(text=_main_menu_button_text)])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        one_time_keyboard=True,
    )


# Скрывает Reply-клавиатуру отдельным сообщением и сразу удаляет его.
# Нельзя вешать ReplyKeyboardRemove на статус «Анализирую…»: Telegram запрещает edit такого сообщения.
# Используется перед статусом анализа (фото/текст/уточнение).
async def hide_reply_keyboard(message: Message) -> None:
    try:
        stub = await message.answer("\u2060", reply_markup=ReplyKeyboardRemove())
        await stub.delete()
    except Exception:
        pass


# Завершает статус-сообщение текстом и при необходимости возвращает «🏠 Главное меню».
# Используется в ветках no_food / bad_desc / ошибках анализа (ReplyKeyboard — только новым сообщением).
async def end_with_status_text(
    message: Message,
    text: str,
    *,
    status_message: Message | None = None,
    restore_main_menu: bool = True,
) -> Message:
    markup: ReplyKeyboardMarkup | ReplyKeyboardRemove = (
        build_main_menu_only_keyboard() if restore_main_menu else ReplyKeyboardRemove()
    )
    if status_message is not None:
        try:
            await status_message.delete()
        except Exception:
            pass
    return await message.answer(text, reply_markup=markup)
#endregion

#region Утилиты
# Печатает итоговый JSON результата в консоль (заглушка вместо дневника/БД).
# Используется из persist_confirmed_food при ✅ / автотаймауте / после пересчёта веса.
def save_to_console(result: FoodResult) -> None:
    payload = result.model_dump()
    print(
        "\n===== SAVE (console) =====\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        "==========================\n",
        flush=True,
    )


# «Сохраняет» подтверждённый результат (консоль) и вызывает on_food_saved (reminders).
# Используется при ✅ / автотаймауте / после пересчёта веса этикетки.
async def persist_confirmed_food(
    result: FoodResult,
    *,
    user_id: int,
    bot: Bot,
    chat_id: int,
) -> None:
    save_to_console(result)
    if _on_food_saved is not None:
        await _on_food_saved(user_id, result, bot, chat_id)


# Скачивает Telegram-фото по file_id во временный .jpg и возвращает путь.
# Используется в on_photo и при повторном анализе после подсказки.
async def download_photo_temp(bot: Bot, file_id: str) -> Path:
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        temp_path = Path(tmp.name)
    await bot.download(file_id, destination=temp_path)
    return temp_path


# Парсит сырой JSON-ответ модели в FoodResult; при ошибке возвращает None.
# Используется после analyze_food_photo / analyze_food_text.
def parse_food_result(raw: str | None) -> FoodResult | None:
    if not raw:
        return None
    try:
        return FoodResult.model_validate(json.loads(raw))
    except Exception as e:
        print(f"Не удалось разобрать ответ модели: {e}")
        return None
#endregion

#region Confirm UI
# Убирает кнопки и переписывает превью без WIDTH_PAD_CHAR (обычная ширина пузыря).
# Используется при ✅ и автоподтверждении.
async def finalize_confirmed_preview(
    result: FoodResult,
    *,
    message: Message | None = None,
    bot: Bot | None = None,
    chat_id: int | None = None,
    message_id: int | None = None,
) -> None:
    text = format_food_result(result)
    try:
        if message is not None:
            await message.edit_text(text, reply_markup=None, parse_mode="HTML")
        elif bot is not None and chat_id is not None and message_id is not None:
            await bot.edit_message_text(
                text,
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=None,
                parse_mode="HTML",
            )
    except Exception:
        pass


# Запускает таймер автоподтверждения; срабатывает только если confirm_token актуален.
# Используется сразу после показа превью с кнопками.
async def schedule_auto_confirm(
    bot: Bot,
    chat_id: int,
    user_id: int,
    token: str,
    preview_message_id: int,
) -> None:
    await asyncio.sleep(CONFIRM_TIMEOUT_SEC)
    if _storage is None:
        return
    state = FSMContext(
        storage=_storage,
        key=StorageKey(bot_id=bot.id, chat_id=chat_id, user_id=user_id),
    )
    data = await state.get_data()
    if data.get("confirm_token") != token:
        return
    current = await state.get_state()
    if current != FoodFlow.confirming.state:
        return

    result_data = data.get("result")
    if not result_data:
        return

    result = FoodResult.model_validate(result_data)
    await persist_confirmed_food(result, user_id=user_id, bot=bot, chat_id=chat_id)
    await state.clear()
    await finalize_confirmed_preview(
        result, bot=bot, chat_id=chat_id, message_id=preview_message_id
    )
    await bot.send_message(
        chat_id, "Учтено ✅", reply_markup=build_main_menu_only_keyboard()
    )


# Показывает превью КБЖУ, ставит FSM confirming и запускает таймер 10с.
# Используется после успешного recognized/label/текстового анализа.
async def show_confirm_preview(
    message: Message,
    state: FSMContext,
    bot: Bot,
    result: FoodResult,
    *,
    file_id: str | None = None,
    edit_message: Message | None = None,
) -> None:
    if result.status == "label" or result.is_label:
        result = normalize_label_result(result)
        keyboard = build_label_keyboard()
    else:
        keyboard = build_confirm_keyboard()

    token = uuid.uuid4().hex
    text = ensure_min_message_width(format_food_result(result))
    preview: Message | None = None
    if edit_message is not None:
        try:
            preview = await edit_message.edit_text(
                text, reply_markup=keyboard, parse_mode="HTML"
            )
        except Exception:
            preview = None
    if preview is None:
        preview = await message.answer(
            text, reply_markup=keyboard, parse_mode="HTML"
        )

    await state.set_state(FoodFlow.confirming)
    await state.update_data(
        result=result.model_dump(),
        file_id=file_id,
        preview_message_id=preview.message_id,
        confirm_token=token,
    )
    asyncio.create_task(
        schedule_auto_confirm(
            bot=bot,
            chat_id=message.chat.id,
            user_id=message.from_user.id if message.from_user else message.chat.id,
            token=token,
            preview_message_id=preview.message_id,
        )
    )


# Обрабатывает ветки status после ответа ИИ (no_food / unclear / bad_desc / confirm).
# Используется из on_photo, waiting_hint и текстовой ветки.
async def handle_ai_result(
    message: Message,
    state: FSMContext,
    bot: Bot,
    result: FoodResult,
    *,
    file_id: str | None = None,
    status_message: Message | None = None,
) -> None:
    if result.status == "no_food":
        await end_with_status_text(
            message, "Еда на фото не обнаружена", status_message=status_message
        )
        await state.clear()
        return

    if result.status == "bad_desc":
        await end_with_status_text(
            message,
            "Не поняли описание. Сфотографируйте еду заново",
            status_message=status_message,
        )
        await state.clear()
        return

    if result.status == "unclear":
        # Клавиатуру не возвращаем — пользователь ещё в флоу до confirm/отмены.
        ask = (
            "На фото похоже на еду, но не удалось точно распознать.\n"
            "Опишите текстом, что за еда на фото — или нажмите «Отмена»"
        )
        if status_message is not None:
            await status_message.edit_text(ask, reply_markup=build_cancel_keyboard())
        else:
            await message.answer(
                ask,
                reply_markup=build_cancel_keyboard(),
            )
        await state.set_state(FoodFlow.waiting_hint)
        await state.update_data(file_id=file_id, result=None, confirm_token=None)
        return

    if result.status in ("recognized", "label"):
        if result.calories is None:
            await end_with_status_text(
                message,
                "Не удалось оценить калорийность. Попробуйте другое фото или уточните текстом",
                status_message=status_message,
            )
            await state.clear()
            return
        await show_confirm_preview(
            message,
            state,
            bot,
            result,
            file_id=file_id,
            edit_message=status_message,
        )
        return

    await end_with_status_text(
        message,
        "Не удалось обработать результат. Попробуйте ещё раз",
        status_message=status_message,
    )
    await state.clear()
#endregion

#region Обработчик фото
# Обработчик фото: анализ через Gemini, ветки status, confirm UI.
# StateFilter как у текста — не сбрасывать FSM опроса / других состояний.
# Регистрируется на router через декоратор.
@router.message(StateFilter(None, FoodFlow.confirming), F.photo)
async def on_photo(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    # Скрываем «🏠» отдельно: статус должен оставаться editable для превью.
    await hide_reply_keyboard(message)
    status_msg = await message.answer("✨ Анализирую фото…")
    photo = message.photo[-1]
    file_id = photo.file_id
    hint = (message.caption or "").strip() or None
    temp_path: Path | None = None

    try:
        temp_path = await download_photo_temp(bot, file_id)
        raw = await asyncio.to_thread(analyze_food_photo, str(temp_path), hint)
        result = parse_food_result(raw)
        if result is None:
            await end_with_status_text(
                message,
                "Не удалось проанализировать фото: модели сейчас недоступны "
                "или ответ не разобран. Попробуй ещё раз чуть позже",
                status_message=status_msg,
            )
            return
        await handle_ai_result(
            message,
            state,
            bot,
            result,
            file_id=file_id,
            status_message=status_msg,
        )
    except Exception as e:
        print(f"Ошибка при обработке фото: {e}")
        await end_with_status_text(
            message,
            "Произошла ошибка при обработке фото. Попробуй ещё раз",
            status_message=status_msg,
        )
        await state.clear()
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink(missing_ok=True)
#endregion

#region Подсказка к фото / уточнение текста
# Уточнение: повторный AI-запрос с тем же file_id или пересчёт по тексту.
# Используется в состоянии FoodFlow.waiting_hint (unclear / меню «✏️ Изменить»).
@router.message(FoodFlow.waiting_hint, F.text)
async def on_hint_text(message: Message, state: FSMContext, bot: Bot) -> None:
    hint = (message.text or "").strip()
    if not hint:
        await message.answer("Напишите текстовую подсказку или нажмите «Отмена»")
        return

    data = await state.get_data()
    file_id = data.get("file_id")

    # Текстовый результат без фото: уточняем через analyze_food_text.
    if not file_id:
        prev = data.get("result") or {}
        prev_dish = (prev.get("dish") or "").strip()
        query = (
            f"Ранее распознано: {prev_dish}. Уточнение пользователя: {hint}"
            if prev_dish
            else hint
        )
        await hide_reply_keyboard(message)
        status_msg = await message.answer("Уточняю по вашей подсказке…")
        try:
            raw = await asyncio.to_thread(analyze_food_text, query)
            result = parse_food_result(raw)
            if result is None:
                await end_with_status_text(
                    message,
                    "Не удалось уточнить результат. Попробуйте другое описание "
                    "или пришлите фото",
                    status_message=status_msg,
                )
                return
            if result.status == "label":
                result = result.model_copy(
                    update={"status": "recognized", "is_label": False}
                )
            await handle_ai_result(
                message,
                state,
                bot,
                result,
                file_id=None,
                status_message=status_msg,
            )
        except Exception as e:
            print(f"Ошибка при уточнении текстового результата: {e}")
            await end_with_status_text(
                message,
                "Ошибка при уточнении. Попробуйте ещё раз",
                status_message=status_msg,
            )
        return

    await hide_reply_keyboard(message)
    status_msg = await message.answer("Уточняю по вашей подсказке…")
    temp_path: Path | None = None
    try:
        temp_path = await download_photo_temp(bot, file_id)
        raw = await asyncio.to_thread(analyze_food_photo, str(temp_path), hint)
        result = parse_food_result(raw)
        if result is None:
            await end_with_status_text(
                message,
                "Не удалось уточнить результат. Попробуйте другое описание или новое фото",
                status_message=status_msg,
            )
            return
        await handle_ai_result(
            message,
            state,
            bot,
            result,
            file_id=file_id,
            status_message=status_msg,
        )
    except Exception as e:
        print(f"Ошибка при уточнении по подсказке: {e}")
        await end_with_status_text(
            message,
            "Ошибка при уточнении. Попробуйте ещё раз",
            status_message=status_msg,
        )
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink(missing_ok=True)
#endregion

#region Ввод веса
# Ввод точного веса (г) после ⚖️: пересчёт КБЖУ и «сохранение» в консоль.
# Используется в состоянии FoodFlow.waiting_weight.
@router.message(FoodFlow.waiting_weight, F.text)
async def on_weight_text(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip().replace(",", ".")
    try:
        weight = float(raw)
        if weight <= 0 or weight > 10_000:
            raise ValueError("out of range")
    except ValueError:
        await message.answer("Введите вес числом в граммах, например: 150")
        return

    data = await state.get_data()
    result_data = data.get("result")
    if not result_data:
        await message.answer("Данные для пересчёта потеряны. Пришлите фото заново")
        await state.clear()
        return

    base = FoodResult.model_validate(result_data)
    updated = recalc_by_weight(base, weight)
    user_id = message.from_user.id if message.from_user else 0
    await persist_confirmed_food(
        updated, user_id=user_id, bot=message.bot, chat_id=message.chat.id
    )
    await state.clear()
    await message.answer(
        f"{format_food_result(updated)}\n\nУчтено ✅ (с пересчётом на {weight:g} г)",
        parse_mode="HTML",
        reply_markup=build_main_menu_only_keyboard(),
    )
#endregion

#region Обработчик текста
# Текстовая ветка учёта: отправка текста в нейронку → confirm UI.
# Не срабатывает в waiting_hint / waiting_weight (отдельные хендлеры выше).
# Кнопки главного меню отфильтрованы NotMenuButtonFilter — их ловит menu_router.
@router.message(
    StateFilter(None, FoodFlow.confirming),
    F.text,
    NotMenuButtonFilter(),
)
async def on_text_food(message: Message, state: FSMContext, bot: Bot) -> None:
    # Если пользователь пишет во время confirming — считаем новым запросом.
    await state.clear()
    text = (message.text or "").strip()
    if not text:
        await message.answer("Напишите, что съели, или сколько ккал — либо пришлите фото")
        return

    # Скрываем «🏠» отдельно: статус должен оставаться editable для превью.
    await hide_reply_keyboard(message)
    status_msg = await message.answer("✨ Анализирую описание…")
    try:
        raw = await asyncio.to_thread(analyze_food_text, text)
        result = parse_food_result(raw)
        if result is None:
            await end_with_status_text(
                message,
                "Не удалось разобрать описание. Попробуйте сформулировать иначе "
                "или пришлите фото",
                status_message=status_msg,
            )
            return
        # Для чистого текста unclear/bad_desc обрабатываем мягче.
        if result.status == "unclear":
            await end_with_status_text(
                message,
                "Не хватило данных для оценки. Уточните блюдо и порцию "
                "или пришлите фото",
                status_message=status_msg,
            )
            return
        if result.status == "bad_desc":
            await end_with_status_text(
                message,
                "Не поняли описание. Напишите иначе или пришлите фото еды",
                status_message=status_msg,
            )
            return
        if result.status == "no_food":
            await end_with_status_text(
                message,
                "По этому тексту еду учесть не удалось",
                status_message=status_msg,
            )
            return
        if result.status == "label":
            result = result.model_copy(update={"status": "recognized", "is_label": False})
        await handle_ai_result(
            message,
            state,
            bot,
            result,
            file_id=None,
            status_message=status_msg,
        )
    except Exception as e:
        print(f"Ошибка при обработке текста: {e}")
        await end_with_status_text(
            message,
            "Произошла ошибка при обработке текста. Попробуй ещё раз",
            status_message=status_msg,
        )
        await state.clear()
#endregion

#region Callbacks
# Callback ✅: сохраняет JSON в консоль и завершает флоу.
# Регистрируется на router через декоратор.
@router.callback_query(F.data == CALLBACK_CONFIRM)
async def on_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    result_data = data.get("result")
    if not result_data:
        await callback.message.answer(
            "Нечего подтверждать. Пришлите фото или текст заново",
            reply_markup=build_main_menu_only_keyboard(),
        )
        await state.clear()
        return

    # Инвалидируем таймер.
    await state.update_data(confirm_token=None)
    result = FoodResult.model_validate(result_data)
    await persist_confirmed_food(
        result,
        user_id=callback.from_user.id,
        bot=callback.bot,
        chat_id=callback.message.chat.id if callback.message else callback.from_user.id,
    )
    await state.clear()
    await finalize_confirmed_preview(result, message=callback.message)
    await callback.message.answer(
        "Учтено ✅", reply_markup=build_main_menu_only_keyboard()
    )


# Callback ✏️: показывает меню правок с reply-клавиатурой (+ «🏠 Главное меню»).
# Регистрируется на router через декоратор.
@router.callback_query(F.data == CALLBACK_EDIT)
async def on_edit(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    if not data.get("result"):
        await callback.message.answer(
            "Нечего менять. Пришлите фото или текст заново",
            reply_markup=build_main_menu_only_keyboard(),
        )
        await state.clear()
        return

    await state.update_data(confirm_token=None)
    await state.set_state(FoodFlow.editing_choice)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(
        "Что поправить в результате?\n"
        "Выберите действие на клавиатуре ниже:",
        reply_markup=build_edit_menu_keyboard(),
    )


#region Меню правок (reply-кнопки)
# Пункт «✏️ Изменить вес порции»: переход к вводу граммов и пересчёту КБЖУ.
# Используется в состоянии FoodFlow.editing_choice.
@router.message(FoodFlow.editing_choice, F.text == BTN_EDIT_WEIGHT)
async def on_edit_weight(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("result"):
        await message.answer(
            "Данные потеряны. Пришлите фото или описание заново",
            reply_markup=build_main_menu_only_keyboard(),
        )
        await state.clear()
        return

    await state.set_state(FoodFlow.waiting_weight)
    await message.answer(
        "Введите вес порции в граммах (например: 150)",
        reply_markup=build_main_menu_only_keyboard(),
    )


# Пункт «➕ Дополнить…»: запрос уточнения (к фото или к текстовому результату).
# Используется в состоянии FoodFlow.editing_choice.
@router.message(FoodFlow.editing_choice, F.text == BTN_EDIT_HINT)
async def on_edit_hint(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("result") and not data.get("file_id"):
        await message.answer(
            "Данные потеряны. Пришлите фото или описание заново",
            reply_markup=build_main_menu_only_keyboard(),
        )
        await state.clear()
        return

    await state.set_state(FoodFlow.waiting_hint)
    await message.answer(
        "Дополните или уточните описание блюда текстом",
        reply_markup=build_main_menu_only_keyboard(),
    )
    await message.answer(
        "Если передумали — нажмите «Отмена»",
        reply_markup=build_cancel_keyboard(),
    )


# Пункт «🔄 Заменить…»: сброс флоу и ожидание нового фото/текста.
# Используется в состоянии FoodFlow.editing_choice.
@router.message(FoodFlow.editing_choice, F.text == BTN_EDIT_REPLACE)
async def on_edit_replace(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Хорошо. Пришлите новое фото или текстовое описание — "
        "распознаем заново",
        reply_markup=build_main_menu_only_keyboard(),
    )


# Пункт «❌ Не сохранять»: выход без записи результата в дневник.
# Используется в состоянии FoodFlow.editing_choice.
@router.message(FoodFlow.editing_choice, F.text == BTN_EDIT_DISCARD)
async def on_edit_discard(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Хорошо, результат не сохранён в дневник",
        reply_markup=build_main_menu_only_keyboard(),
    )


# Любой другой текст в меню правок — напоминание выбрать кнопку.
# Используется в состоянии FoodFlow.editing_choice.
@router.message(FoodFlow.editing_choice, F.text)
async def on_edit_choice_other(message: Message) -> None:
    await message.answer(
        "Выберите один из пунктов на клавиатуре ниже",
        reply_markup=build_edit_menu_keyboard(),
    )
#endregion


# Callback ⚖️: переход к вводу точного веса продукта.
# Регистрируется на router через декоратор.
@router.callback_query(F.data == CALLBACK_WEIGHT)
async def on_weight_button(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    if not data.get("result"):
        await callback.message.answer("Данные потеряны. Пришлите фото заново")
        await state.clear()
        return

    await state.update_data(confirm_token=None)
    await state.set_state(FoodFlow.waiting_weight)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer("Введите реальный вес продукта в граммах (например: 150)")


# Callback «Отмена»: выход из waiting_hint без повторного запроса.
# Регистрируется на router через декоратор.
@router.callback_query(F.data == CALLBACK_CANCEL)
async def on_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(
        "Отменено. Можете прислать новое фото или текстовое описание",
        reply_markup=build_main_menu_only_keyboard(),
    )
#endregion

#region Setup
# Подключает MemoryStorage, тексты меню / «🏠» и колбэк после сохранения еды; возвращает router.
# Используется в main.py: setup_food_recognition(storage, menu_button_texts=...,
#   main_menu_button_text=..., on_food_saved=...).
def setup_food_recognition(
    storage: MemoryStorage,
    menu_button_texts: frozenset[str] | None = None,
    main_menu_button_text: str | None = None,
    on_food_saved: OnFoodSavedCallback | None = None,
) -> Router:
    global _storage, _menu_button_texts, _main_menu_button_text, _on_food_saved
    _storage = storage
    _menu_button_texts = menu_button_texts or frozenset()
    _main_menu_button_text = main_menu_button_text
    _on_food_saved = on_food_saved
    return router
#endregion
