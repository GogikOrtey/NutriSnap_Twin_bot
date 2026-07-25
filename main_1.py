import asyncio
import json
import os
import tempfile
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message
from dotenv import load_dotenv
from google import genai
from pydantic import BaseModel, Field

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Таймаут одного запроса к модели (мс). При истечении — переход к следующей модели в очереди.
REQUEST_TIMEOUT_MS = 10_000

# Очередь моделей для попыток (fallback при ошибке/таймауте).
MODELS_QUEUE = [
    "gemini-3.5-flash",
    "gemini-flash-latest",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash",
]

client = genai.Client(api_key=GEMINI_API_KEY)
dp = Dispatcher()


# Схема structured output Gemini: блюдо + КБЖУ.
# Используется в analyze_food_photo как response_schema.
class FoodAnalysis(BaseModel):
    dish: str = Field(description="Название распознанного блюда")
    calories: int = Field(description="Калорийность в ккал")
    proteins: float = Field(description="Белки в граммах")
    fats: float = Field(description="Жиры в граммах")
    carbs: float = Field(description="Углеводы в граммах")


# Анализ фото блюда через Gemini Files API с fallback по очереди моделей.
# Используется в обработчике фото бота (через asyncio.to_thread).
def analyze_food_photo(image_path: str) -> str | None:
    print("Загрузка файла в Google Files API...")
    uploaded_file = client.files.upload(file=image_path)
    print(f"Файл успешно загружен. URI: {uploaded_file.uri}")

    prompt = "Распознай еду на фото и детально оцени её калорийность и БЖУ."
    response_text = None
    skipped_models: set[str] = set()
    total_attempts = len(MODELS_QUEUE)

    try:
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
                    contents=[uploaded_file, prompt],
                    config={
                        "response_mime_type": "application/json",
                        "response_schema": FoodAnalysis,
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
    finally:
        print("\nУдаление временного файла из Google Cloud...")
        try:
            client.files.delete(name=uploaded_file.name)
        except Exception as e:
            print(f"Не удалось удалить файл из Google Cloud: {e}")

    return response_text


# Форматирует FoodAnalysis в читаемый блок для пользователя (блюдо + КБЖУ).
# Используется при ответе бота после успешного анализа фото.
def format_food_analysis(analysis: FoodAnalysis) -> str:
    return (
        f"🍽  {analysis.dish}\n"
        f"\n"
        f"📋 Пищевая ценность:\n"
        f"• 🔥 Калорийность: {analysis.calories} ккал\n"
        f"• 🥩 Белки: {analysis.proteins} г\n"
        f"• 🥑 Жиры: {analysis.fats} г\n"
        f"• 🍞 Углеводы: {analysis.carbs} г"
    )


# Обработчик /start: кратко объясняет, что нужно прислать фото блюда.
# Регистрируется через декоратор dp.message(CommandStart()).
@dp.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(
        "v0.1\n"
        "\n"
        "Привет! Я @nutrisnap_ultra_bot.\n"
        "Пришли фото блюда — оценю калорийность и БЖУ."
    )


# Обработчик фото: скачивает изображение, анализирует через Gemini, отвечает сводкой КБЖУ.
# Регистрируется через декоратор dp.message(F.photo).
@dp.message(F.photo)
async def on_photo(message: Message, bot: Bot) -> None:
    status = await message.answer("Анализирую фото…")
    photo = message.photo[-1]
    temp_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            temp_path = Path(tmp.name)

        await bot.download(photo, destination=temp_path)

        result = await asyncio.to_thread(analyze_food_photo, str(temp_path))
        if not result:
            await status.edit_text(
                "Не удалось проанализировать фото: модели сейчас недоступны. "
                "Попробуй ещё раз чуть позже."
            )
            return

        try:
            analysis = FoodAnalysis.model_validate(json.loads(result))
            await status.edit_text(format_food_analysis(analysis))
        except Exception as e:
            print(f"Не удалось разобрать ответ модели: {e}")
            await status.edit_text(
                "Модель ответила, но не удалось разобрать результат. Попробуй другое фото."
            )
    except Exception as e:
        print(f"Ошибка при обработке фото: {e}")
        await status.edit_text("Произошла ошибка при обработке фото. Попробуй ещё раз.")
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink(missing_ok=True)


# Подсказка, если прислали не фото (и не /start).
# Регистрируется как общий обработчик текстовых сообщений.
@dp.message(F.text)
async def on_text(message: Message) -> None:
    await message.answer("Пришли фото блюда — оценю калорийность и БЖУ.")


# Точка входа: проверяет ключи и поднимает long-polling бота.
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


# Запуск: python main_1.py

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🟧 Бот остановлен", flush=True)
