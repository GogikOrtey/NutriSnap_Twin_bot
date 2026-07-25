import asyncio
import html
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any, Literal

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv
from google import genai
from pydantic import BaseModel, Field

#region Конфиг
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Таймаут одного запроса к модели (мс). При истечении — переход к следующей модели в очереди.
REQUEST_TIMEOUT_MS = 10_000

# Таймаут автоподтверждения результата пользователем (сек).
CONFIRM_TIMEOUT_SEC = 10

# Стандартная порция (г), если на этикетке не указан объём.
DEFAULT_PORTION_G = 100.0

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
#endregion

#region Промпты
PHOTO_PROMPT = """Ты анализируешь фото для учёта калорий.

Верни JSON строго по схеме. Выбери ровно один status:
- recognized — на фото обычная еда, уверенно оцени dish + calories + БЖУ (proteins/fats/carbs).
  portion_g — ВСЕГДА оцени примерный вес порции на фото в граммах (визуально).
  portion_known=false, is_label=false.
- unclear — на фото похоже на еду, но уверенности нет: dish/calories/БЖУ можно оставить null.
- no_food — еды нет, или этикетка без читаемых данных о ккал.
- label — на фото этикетка/состав с читаемыми ккал (и опционально КБЖУ).
  is_label=true. Если указано «на 100 г» / «на порцию X г» — заполни portion_g и portion_known=true.
  Если объём не указан — portion_known=false, portion_g можно не заполнять.
- bad_desc — используй ТОЛЬКО если пользователь дал текстовую подсказку, и она явно
  противоречит фото / мешает классификации.

Числа: calories — целое ккал; proteins/fats/carbs — граммы (float); portion_g — граммы (float)."""

TEXT_PROMPT = """Пользователь описывает еду или калории текстом (без фото).

Верни JSON строго по схеме:
- status=recognized, если удалось извлечь/оценить калории (и по возможности dish + БЖУ).
- status=unclear, если текста недостаточно для оценки.
- status=bad_desc, если текст явно бессмысленный для учёта еды.
- status=no_food / label не используй для чистого текста.

Заполни calories (целое). dish и БЖУ — если возможно.
portion_g — ВСЕГДА: если пользователь указал вес — используй его; иначе оцени
типичную/среднюю порцию такого блюда в граммах.
is_label=false, portion_known=false."""
#endregion

#region Схема ответа
client = genai.Client(api_key=GEMINI_API_KEY)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)


# FSM-состояния учёта еды: подтверждение, подсказка к фото, ввод точного веса.
# Используется в хендлерах фото/текста/callback и таймере автоподтверждения.
class FoodFlow(StatesGroup):
    confirming = State()
    waiting_hint = State()
    waiting_weight = State()


# Схема structured output Gemini: статус ветки + КБЖУ/порция.
# Используется в analyze_food_photo / analyze_food_text как response_schema.
class FoodResult(BaseModel):
    status: Literal["recognized", "unclear", "no_food", "label", "bad_desc"] = Field(
        description="Ветка результата распознавания"
    )
    dish: str | None = Field(default=None, description="Название блюда/продукта")
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


# Форматирует FoodResult в читаемый блок для пользователя (блюдо + порция + КБЖУ).
# HTML-разметка (<b>); вызывающий код должен слать с parse_mode="HTML".
# Используется при показе превью перед подтверждением.
def format_food_result(result: FoodResult) -> str:
    dish = html.escape(result.dish or "Блюдо")
    lines = [f"🍽  {dish}"]

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
            lines.append(f"Значения рассчитаны на {portion:g} г (как на этикетке).")
        else:
            lines.append("")
            lines.append(
                f"Объём на этикетке не указан — значения для стандартной порции "
                f"{portion:g} г."
            )
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
#endregion

#region Утилиты
# Печатает итоговый JSON результата в консоль (заглушка вместо дневника/БД).
# Используется при ✅ / автотаймауте / после пересчёта веса.
def save_to_console(result: FoodResult) -> None:
    payload = result.model_dump()
    print(
        "\n===== SAVE (console) =====\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        "==========================\n",
        flush=True,
    )


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
    state = FSMContext(
        storage=storage,
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
    save_to_console(result)
    await state.clear()
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=preview_message_id, reply_markup=None
        )
    except Exception:
        pass
    await bot.send_message(chat_id, "Учтено ✅ (автоподтверждение по таймауту)")


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
    text = format_food_result(result)
    if edit_message is not None:
        preview = await edit_message.edit_text(
            text, reply_markup=keyboard, parse_mode="HTML"
        )
    else:
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
        text = "Еда на фото не обнаружена."
        if status_message is not None:
            await status_message.edit_text(text)
        else:
            await message.answer(text)
        await state.clear()
        return

    if result.status == "bad_desc":
        text = "Не поняли описание. Сфотографируйте еду заново."
        if status_message is not None:
            await status_message.edit_text(text)
        else:
            await message.answer(text)
        await state.clear()
        return

    if result.status == "unclear":
        ask = (
            "На фото похоже на еду, но не удалось точно распознать.\n"
            "Опишите текстом, что за еда на фото — или нажмите «Отмена»."
        )
        if status_message is not None:
            await status_message.edit_text(ask, reply_markup=build_cancel_keyboard())
        else:
            await message.answer(ask, reply_markup=build_cancel_keyboard())
        await state.set_state(FoodFlow.waiting_hint)
        await state.update_data(file_id=file_id, result=None, confirm_token=None)
        return

    if result.status in ("recognized", "label"):
        if result.calories is None:
            text = "Не удалось оценить калорийность. Попробуйте другое фото или уточните текстом."
            if status_message is not None:
                await status_message.edit_text(text)
            else:
                await message.answer(text)
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

    text = "Не удалось обработать результат. Попробуйте ещё раз."
    if status_message is not None:
        await status_message.edit_text(text)
    else:
        await message.answer(text)
    await state.clear()
#endregion

#region Стартовое сообщение
# Обработчик /start: кратко объясняет, как учитывать еду (фото или текст).
# Регистрируется через декоратор dp.message(CommandStart()).
@dp.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "v0.2\n"
        "\n"
        "Привет! Я @nutrisnap_ultra_bot.\n"
        "Пришли фото блюда (можно с подписью) или напиши текстом, "
        "что съел / сколько ккал — оценю калорийность и БЖУ."
    )
#endregion

#region Обработчик фото
# Обработчик фото: анализ через Gemini, ветки status, confirm UI.
# Регистрируется через декоратор dp.message(F.photo).
@dp.message(F.photo)
async def on_photo(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    status_msg = await message.answer("Анализирую фото…")
    photo = message.photo[-1]
    file_id = photo.file_id
    hint = (message.caption or "").strip() or None
    temp_path: Path | None = None

    try:
        temp_path = await download_photo_temp(bot, file_id)
        raw = await asyncio.to_thread(analyze_food_photo, str(temp_path), hint)
        result = parse_food_result(raw)
        if result is None:
            await status_msg.edit_text(
                "Не удалось проанализировать фото: модели сейчас недоступны "
                "или ответ не разобран. Попробуй ещё раз чуть позже."
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
        await status_msg.edit_text("Произошла ошибка при обработке фото. Попробуй ещё раз.")
        await state.clear()
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink(missing_ok=True)
#endregion

#region Подсказка к фото
# Текстовая подсказка к фото: повторный AI-запрос с тем же file_id.
# Используется в состоянии FoodFlow.waiting_hint.
@dp.message(FoodFlow.waiting_hint, F.text)
async def on_hint_text(message: Message, state: FSMContext, bot: Bot) -> None:
    hint = (message.text or "").strip()
    if not hint:
        await message.answer("Напишите текстовую подсказку или нажмите «Отмена».")
        return

    data = await state.get_data()
    file_id = data.get("file_id")
    if not file_id:
        await message.answer("Фото для уточнения не найдено. Пришлите новое фото.")
        await state.clear()
        return

    status_msg = await message.answer("Уточняю по вашей подсказке…")
    temp_path: Path | None = None
    try:
        temp_path = await download_photo_temp(bot, file_id)
        raw = await asyncio.to_thread(analyze_food_photo, str(temp_path), hint)
        result = parse_food_result(raw)
        if result is None:
            await status_msg.edit_text(
                "Не удалось уточнить результат. Попробуйте другое описание или новое фото."
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
        await status_msg.edit_text("Ошибка при уточнении. Попробуйте ещё раз.")
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink(missing_ok=True)
#endregion

#region Ввод веса
# Ввод точного веса (г) после ⚖️: пересчёт КБЖУ и «сохранение» в консоль.
# Используется в состоянии FoodFlow.waiting_weight.
@dp.message(FoodFlow.waiting_weight, F.text)
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
        await message.answer("Данные для пересчёта потеряны. Пришлите фото заново.")
        await state.clear()
        return

    base = FoodResult.model_validate(result_data)
    updated = recalc_by_weight(base, weight)
    save_to_console(updated)
    await state.clear()
    await message.answer(
        f"{format_food_result(updated)}\n\nУчтено ✅ (с пересчётом на {weight:g} г).",
        parse_mode="HTML",
    )
#endregion

#region Обработчик текста
# Текстовая ветка учёта: отправка текста в нейронку → confirm UI.
# Не срабатывает в waiting_hint / waiting_weight (отдельные хендлеры выше).
@dp.message(StateFilter(None, FoodFlow.confirming), F.text)
async def on_text_food(message: Message, state: FSMContext, bot: Bot) -> None:
    # Если пользователь пишет во время confirming — считаем новым запросом.
    await state.clear()
    text = (message.text or "").strip()
    if not text:
        await message.answer("Напишите, что съели, или сколько ккал — либо пришлите фото.")
        return

    status_msg = await message.answer("Анализирую описание…")
    try:
        raw = await asyncio.to_thread(analyze_food_text, text)
        result = parse_food_result(raw)
        if result is None:
            await status_msg.edit_text(
                "Не удалось разобрать описание. Попробуйте сформулировать иначе "
                "или пришлите фото."
            )
            return
        # Для чистого текста unclear/bad_desc обрабатываем мягче.
        if result.status == "unclear":
            await status_msg.edit_text(
                "Не хватило данных для оценки. Уточните блюдо и порцию "
                "или пришлите фото."
            )
            return
        if result.status == "bad_desc":
            await status_msg.edit_text(
                "Не поняли описание. Напишите иначе или пришлите фото еды."
            )
            return
        if result.status == "no_food":
            await status_msg.edit_text("По этому тексту еду учесть не удалось.")
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
        await status_msg.edit_text("Произошла ошибка при обработке текста. Попробуй ещё раз.")
        await state.clear()
#endregion

#region Callbacks
# Callback ✅: сохраняет JSON в консоль и завершает флоу.
# Регистрируется через декоратор dp.callback_query.
@dp.callback_query(F.data == CALLBACK_CONFIRM)
async def on_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    result_data = data.get("result")
    if not result_data:
        await callback.message.answer("Нечего подтверждать. Пришлите фото или текст заново.")
        await state.clear()
        return

    # Инвалидируем таймер.
    await state.update_data(confirm_token=None)
    result = FoodResult.model_validate(result_data)
    save_to_console(result)
    await state.clear()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer("Учтено ✅")


# Callback ✏️: запрос текстовой подсказки (повтор с тем же фото, если есть).
# Регистрируется через декоратор dp.callback_query.
@dp.callback_query(F.data == CALLBACK_EDIT)
async def on_edit(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    file_id = data.get("file_id")
    await state.update_data(confirm_token=None)

    if not file_id:
        await state.clear()
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.answer(
            "Опишите текстом блюдо и порцию заново — или пришлите новое фото."
        )
        return

    await state.set_state(FoodFlow.waiting_hint)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(
        "Неверно распознано. Опишите текстом, что за еда на фото — "
        "или нажмите «Отмена».",
        reply_markup=build_cancel_keyboard(),
    )


# Callback ⚖️: переход к вводу точного веса продукта.
# Регистрируется через декоратор dp.callback_query.
@dp.callback_query(F.data == CALLBACK_WEIGHT)
async def on_weight_button(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    if not data.get("result"):
        await callback.message.answer("Данные потеряны. Пришлите фото заново.")
        await state.clear()
        return

    await state.update_data(confirm_token=None)
    await state.set_state(FoodFlow.waiting_weight)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer("Введите реальный вес продукта в граммах (например: 150).")


# Callback «Отмена»: выход из waiting_hint без повторного запроса.
# Регистрируется через декоратор dp.callback_query.
@dp.callback_query(F.data == CALLBACK_CANCEL)
async def on_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer("Отменено. Можете прислать новое фото или текстовое описание.")
#endregion

#region Запуск
# Точка входа: проверяет ключи и поднимает long-polling бота с MemoryStorage.
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
    print("🟩 Бот @nutrisnap_ultra_bot (main_2) запущен. Нажми Ctrl+C для остановки", flush=True)
    await dp.start_polling(bot)


# Запуск: python main_2.py
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🟧 Бот остановлен", flush=True)
#endregion
