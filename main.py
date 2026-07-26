"""
main.py — точка входа Telegram-бота NutriSnap (@nutrisnap_ultra_bot).

Зачем нужен файл
----------------
Запуск бота (long polling), общая инфраструктура (Bot, Dispatcher, MemoryStorage)
и базовые хендлеры вроде /start. Сюда же позже добавится главное меню.
Распознавание еды вынесено в food_recognition.py и подключается как Router.

Как устроен файл
----------------
1. Импорты, загрузка .env, проверка ключей.
2. Создание Bot / Dispatcher / MemoryStorage.
3. Подключение router распознавания еды.
4. Хендлер /start.
5. main() — старт polling.
"""

import asyncio
import os

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message
from dotenv import load_dotenv

from food_recognition import setup_food_recognition

#region Конфиг и инфраструктура
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

storage = MemoryStorage()
dp = Dispatcher(storage=storage)
dp.include_router(setup_food_recognition(storage))
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
        "что съел / сколько ккал — оценю калорийность и БЖУ"
    )
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
    print("🟩 Бот @nutrisnap_ultra_bot запущен. Нажми Ctrl+C для остановки", flush=True)
    await dp.start_polling(bot)


# Запуск: python main.py
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🟧 Бот остановлен", flush=True)
#endregion
