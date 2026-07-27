"""
Песочница: один запрос к NocoDB + замер задержки до ответа.

Зачем: быстро проверить RTT до SkyNode (без CRUD-прогона).
Как пользоваться:
  1. Запусти: python requests_DB_latency_test.py
  2. Смотри статус, число записей и latency_ms в консоли.

Swagger: https://skynode.nocodb.api.gogortey.ru/api/v3/meta/bases/p6iywpukq1yiryf/swagger
Ключ: NOCODB_SKYNODE_API_KEY в .env (заголовок xc-token).

Запуск: python requests_DB_latency_test.py
"""

from __future__ import annotations

import json
import sys
import time

from dotenv import load_dotenv

load_dotenv()

# Windows-консоль часто в cp1251 — иначе кириллица в JSON «ломается»
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import db_nocodb as db


# GET список users и печатает latency (мс) до ответа NocoDB.
# Используется только в этой песочнице (запуск __main__).
def test_users_list_latency() -> None:
    """Один запрос: список пользователей + замер задержки."""
    print("\n=== test_users_list_latency ===\n")
    print(f"URL: {db.records_url(db.TABLE_USERS)}")

    t0 = time.perf_counter()
    try:
        payload = db.call_api("GET", db.records_url(db.TABLE_USERS))
    except db.NocoDBError as e:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        print(f"[FAIL] GET users — HTTP {e.status}, latency={elapsed_ms:.1f} ms")
        print(e)
        raise SystemExit(1) from e
    elapsed_ms = (time.perf_counter() - t0) * 1000

    rows = db._list_records(payload)
    print(f"[OK] GET users — count={len(rows)}, latency={elapsed_ms:.1f} ms")
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    print(f"\n=== latency: {elapsed_ms:.1f} ms ===\n")


if __name__ == "__main__":
    test_users_list_latency()
