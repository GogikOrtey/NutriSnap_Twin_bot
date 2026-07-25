"""
Здесь можно протестировать бота в Тг
"""


import asyncio
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_API_KEY")

# Callback-данные кнопки «Написать 1»
CALLBACK_WRITE_ONE = "write_one"

dp = Dispatcher()


# Собирает клавиатуру с одной кнопкой «Написать 1».
# Используется в /start при отправке приветствия.
def build_start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            # [
            #     InlineKeyboardButton(text="Написать 1", callback_data=CALLBACK_WRITE_ONE),
            #     InlineKeyboardButton(text="Написать 2", callback_data=CALLBACK_WRITE_ONE)
            # ],
            [
                InlineKeyboardButton(text="✏️ Изменить", callback_data=CALLBACK_WRITE_ONE),
                InlineKeyboardButton(text="✅ Подтвердить", callback_data=CALLBACK_WRITE_ONE)
            ],
        ]
    )


# Обработчик /start: шлёт приветствие и кнопку «Написать 1».
# Регистрируется через декоратор dp.message(CommandStart()).
@dp.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(
        # "Привет! Я @nutrisnap_ultra_bot.\n"
        # "Нажми кнопку ниже — я отвечу в чат.",
        "123\n"
        "012345678901234567890123456789\n" # 3 набора от 0 до 9
        "123\n"
        "123\n",
        reply_markup=build_start_keyboard(),
    )


# Обработчик нажатия кнопки «Написать 1»: отвечает текстом «1».
# Регистрируется через декоратор dp.callback_query(...).
@dp.callback_query(F.data == CALLBACK_WRITE_ONE)
async def on_write_one(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.answer("1")


# Точка входа: поднимает long-polling бота с обработчиками /start и кнопки.
async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "TELEGRAM_BOT_API_KEY не найден. Добавь его в .env и перезапусти скрипт."
        )

    bot = Bot(token=BOT_TOKEN)
    print("Бот @nutrisnap_ultra_bot запущен. Нажми Ctrl+C для остановки.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())