import os

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_API_KEY")

# Callback-данные кнопки «Написать 1»
CALLBACK_WRITE_ONE = "write_one"


# Собирает клавиатуру с одной кнопкой «Написать 1».
# Используется в /start при отправке приветствия.
def build_start_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("Написать 1", callback_data=CALLBACK_WRITE_ONE)],
    ]
    return InlineKeyboardMarkup(keyboard)


# Обработчик /start: шлёт приветствие и кнопку «Написать 1».
# Регистрируется в Application как CommandHandler("start", ...).
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Я @nutrisnap_ultra_bot.\n"
        "Нажми кнопку ниже — я отвечу в чат.",
        reply_markup=build_start_keyboard(),
    )


# Обработчик нажатия кнопки «Написать 1»: отвечает текстом «1».
# Регистрируется в Application как CallbackQueryHandler.
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.data == CALLBACK_WRITE_ONE:
        await query.message.reply_text("1")


# Точка входа: поднимает long-polling бота с обработчиками /start и кнопки.
def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "TELEGRAM_BOT_API_KEY не найден. Добавь его в .env и перезапусти скрипт."
        )

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_button))

    print("Бот @nutrisnap_ultra_bot запущен. Нажми Ctrl+C для остановки.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
