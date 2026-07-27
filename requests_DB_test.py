"""
Песочница запросов к NocoDB API (SkyNode).

Зачем: проверять правильность HTTP-запросов к БД до подключения их в бота.
Как пользоваться:
  1. Добавь/раскомментируй вызов в блоке `if __name__ == "__main__"`.
  2. Запусти: python requests_DB_test.py
  3. Смотри статус и JSON-ответ в консоли.

Swagger: https://skynode.nocodb.api.gogortey.ru/api/v3/meta/bases/p6iywpukq1yiryf/swagger
Ключ: NOCODB_SKYNODE_API_KEY в .env (заголовок xc-token).

Запуск: python requests_DB_test.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

from dotenv import load_dotenv

load_dotenv()

# Windows-консоль часто в cp1251 — иначе кириллица в JSON «ломается»
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# --- Константы NocoDB (SkyNode) ---
NOCODB_BASE_URL = "https://skynode.nocodb.api.gogortey.ru"
NOCODB_BASE_ID = "p6iywpukq1yiryf"
# ID таблиц (из Swagger / URL records)
TABLE_USERS = "meooj41uwpyrx9t"
# TABLE_FOOD_LOGS = "..."  # подставить, когда понадобится
# TABLE_REMINDERS = "..."  # подставить, когда понадобится

API_KEY = os.getenv("NOCODB_SKYNODE_API_KEY", "").strip()


def _records_url(table_id: str, record_id: str | int | None = None) -> str:
    """Собирает URL списка записей или одной записи таблицы NocoDB v3 data API."""
    base = f"{NOCODB_BASE_URL}/api/v3/data/{NOCODB_BASE_ID}/{table_id}/records"
    if record_id is None:
        return base
    return f"{base}/{record_id}"


def call_api(
    method: str,
    url: str,
    *,
    body: dict[str, Any] | list[Any] | None = None,
    label: str | None = None,
) -> dict[str, Any] | list[Any] | None:
    """
    Выполняет HTTP-запрос к NocoDB и печатает статус + pretty JSON.
    Используется в этом файле для ручной проверки запросов.
    Возвращает распарсенный JSON или None при ошибке/пустом теле.
    """
    if not API_KEY:
        print("ERROR: NOCODB_SKYNODE_API_KEY не задан в .env")
        sys.exit(1)

    title = label or f"{method.upper()} {url}"
    print("=" * 60)
    print(title)
    print("=" * 60)

    data: bytes | None = None
    headers = {
        "accept": "application/json",
        "xc-token": API_KEY,
    }
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        status = e.code
        raw = e.read().decode("utf-8", errors="replace")
        print(f"HTTP {status}")
        _print_body(raw)
        return None
    except urllib.error.URLError as e:
        print(f"URL error: {e.reason}")
        return None

    print(f"HTTP {status}")
    parsed = _print_body(raw)
    print()
    return parsed


def _print_body(raw: str) -> dict[str, Any] | list[Any] | None:
    """Печатает тело ответа: pretty JSON, если получилось распарсить."""
    if not raw.strip():
        print("(пустое тело)")
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        print(raw)
        return None
    print(json.dumps(parsed, ensure_ascii=False, indent=2))
    return parsed


# ---------------------------------------------------------------------------
# Готовые запросы — добавляй новые функции по образцу и вызывай их в main
# ---------------------------------------------------------------------------


def get_all_users() -> dict[str, Any] | list[Any] | None:
    """GET всех записей таблицы users. Пример ответа: { records: [...], nestedNext }."""
    return call_api(
        "GET",
        _records_url(TABLE_USERS),
        label="GET all users",
    )


def create_user(fields: dict[str, Any]) -> dict[str, Any] | list[Any] | None:
    """
    POST новой записи в users.
    Body по Swagger: {"fields": {...}}; обязателен id (Telegram ID, не autoincrement).
    Используется в песочнице для проверки создания профиля.
    """
    name = fields.get("first_name") or fields.get("id")
    return call_api(
        "POST",
        _records_url(TABLE_USERS),
        body={"fields": fields},
        label=f"POST create user ({name})",
    )


# Пример шаблона для следующих запросов (раскомментируй и допиши):
#
# def get_user_by_id(user_id: int):
#     """GET одной записи users по id (Telegram ID)."""
#     return call_api("GET", _records_url(TABLE_USERS, user_id), label=f"GET user {user_id}")


if __name__ == "__main__":
    # Какие запросы прогнать сейчас — правь этот список:
    # get_all_users()
    create_user(
        {
            "id": 123456790,
            "first_name": "Тестовый-2",
            "gender": "М",
            "age": 99,
            "height": 200,
            "weight": 100,
            "activity_level": 2,
            "goal": "Похудение",
            "daily_calories": 2000,
            "timezone": "UTC",
            "day_change_hour": 4,
            "created_at": 123456790,
        }
    )
