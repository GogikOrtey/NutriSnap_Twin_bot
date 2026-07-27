"""
proxy_config.py — точечный HTTPS-прокси для Telegram и Gemini.

Зачем нужен файл
----------------
На VPS (SkyNode) api.telegram.org и Gemini недоступны «напрямую»; локальный
mihomo слушает http://127.0.0.1:7890. NocoDB / SMTP / Nominatim должны ходить
без прокси — поэтому НЕ ставим HTTP_PROXY/HTTPS_PROXY/ALL_PROXY на весь процесс.

Как устроен файл
----------------
1. Чтение OUTBOUND_HTTPS_PROXY / TELEGRAM_PROXY / GEMINI_HTTPS_PROXY из .env.
2. get_telegram_proxy / get_gemini_proxy — URL или None (локальный запуск без прокси).
3. make_gemini_client — google-genai Client с httpx proxy только для этого клиента.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()


# Берёт непустую строку из env; пустая / пробелы → None.
# Используется resolve_*_proxy.
def _env_nonempty(*names: str) -> str | None:
    for name in names:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return None


# URL прокси для aiogram (Telegram Bot API) или None.
# Приоритет: TELEGRAM_PROXY → OUTBOUND_HTTPS_PROXY.
# Используется в main.py при создании AiohttpSession.
def get_telegram_proxy() -> str | None:
    return _env_nonempty("TELEGRAM_PROXY", "OUTBOUND_HTTPS_PROXY")


# URL прокси для google-genai (Gemini) или None.
# Приоритет: GEMINI_HTTPS_PROXY → OUTBOUND_HTTPS_PROXY.
# Используется в make_gemini_client.
def get_gemini_proxy() -> str | None:
    return _env_nonempty("GEMINI_HTTPS_PROXY", "OUTBOUND_HTTPS_PROXY")


# Создаёт genai.Client; при заданном прокси — только через него (trust_env=False).
# Без прокси — обычный клиент (локальная разработка). Используется в
# food_recognition.py и initial_survey.py.
def make_gemini_client(api_key: str | None) -> genai.Client | None:
    if not api_key:
        return None

    proxy = get_gemini_proxy()
    if not proxy:
        return genai.Client(api_key=api_key)

    # proxy в client_args → httpx.Client(proxy=...); trust_env=False, чтобы
    # случайный HTTP(S)_PROXY процесса не дублировал и не ломал маршрут.
    http_options = types.HttpOptions(
        client_args={"proxy": proxy, "trust_env": False},
        async_client_args={"proxy": proxy, "trust_env": False},
    )
    return genai.Client(api_key=api_key, http_options=http_options)
