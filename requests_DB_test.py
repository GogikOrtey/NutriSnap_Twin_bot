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
import sys
import time

from dotenv import load_dotenv

load_dotenv()

# Windows-консоль часто в cp1251 — иначе кириллица в JSON «ломается»
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import db_nocodb as db

# Тестовый Telegram ID (не пересекается с реальными пользователями)
TEST_USER_ID = 900000001


def _ok(label: str, cond: bool, detail: str = "") -> None:
    status = "OK" if cond else "FAIL"
    extra = f" — {detail}" if detail else ""
    print(f"[{status}] {label}{extra}")
    if not cond:
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------


def test_users_crud() -> None:
    """GET / POST / PATCH / DELETE users с реалистичными полями опроса."""
    print("\n=== test_users_crud ===\n")

    try:
        db.delete_user(TEST_USER_ID)
        print("(cleanup: deleted leftover test user)")
    except db.NocoDBError as e:
        if e.status not in (404, 400):
            print(f"(cleanup skip: {e})")

    missing = db.get_user(TEST_USER_ID)
    _ok("GET missing user → None", missing is None)

    now = int(time.time())
    created = db.create_user(
        {
            "id": TEST_USER_ID,
            "first_name": "ТестОпрос",
            "gender": "male",
            "age": 28,
            "height": 178.0,
            "weight": 75.0,
            "activity_level": 1.375,
            "goal": "weight_loss",
            "daily_calories": 2200,
            "timezone": "Europe/Moscow",
            "day_change_hour": 4,
            "created_at": now,
            "last_active_at": now,
        }
    )
    _ok(
        "POST create user",
        created.get("id") == TEST_USER_ID and created.get("first_name") == "ТестОпрос",
        json.dumps(created, ensure_ascii=False),
    )

    fetched = db.get_user(TEST_USER_ID)
    _ok(
        "GET user by id",
        fetched is not None and fetched["first_name"] == "ТестОпрос",
        json.dumps(fetched, ensure_ascii=False) if fetched else "",
    )

    patched = db.update_user(
        TEST_USER_ID,
        {
            "first_name": "ТестПатч",
            "daily_calories": 2300,
            "day_change_hour": 5,
            "goal": "maintain",
            "last_active_at": now + 10,
        },
    )
    _ok(
        "PATCH user fields",
        patched.get("first_name") == "ТестПатч"
        and patched.get("daily_calories") == 2300
        and patched.get("day_change_hour") == 5
        and patched.get("goal") == "maintain",
        json.dumps(patched, ensure_ascii=False),
    )

    upserted = db.upsert_profile(
        TEST_USER_ID,
        first_name="ТестUpsert",
        gender="female",
        age=30,
        height=165.0,
        weight=60.0,
        activity_level=1.55,
        goal="muscle_gain",
        timezone="Asia/Yekaterinburg",
        daily_calories=2500,
    )
    _ok(
        "upsert_profile (existing → PATCH)",
        upserted.get("first_name") == "ТестUpsert"
        and upserted.get("goal") == "muscle_gain"
        and upserted.get("timezone") == "Asia/Yekaterinburg",
        json.dumps(upserted, ensure_ascii=False),
    )

    db.delete_user(TEST_USER_ID)
    after = db.get_user(TEST_USER_ID)
    _ok("DELETE user → gone", after is None)
    print("\n=== users CRUD: all passed ===\n")


# ---------------------------------------------------------------------------
# food_logs
# ---------------------------------------------------------------------------


def test_food_logs_crud() -> None:
    """Создать user → INSERT food_log → list → PATCH → DELETE → list."""
    print("\n=== test_food_logs_crud ===\n")

    try:
        db.delete_user(TEST_USER_ID)
    except db.NocoDBError:
        pass

    now = int(time.time())
    db.create_user(
        {
            "id": TEST_USER_ID,
            "first_name": "FoodTest",
            "gender": "male",
            "age": 25,
            "height": 180.0,
            "weight": 80.0,
            "activity_level": 1.2,
            "goal": "maintain",
            "daily_calories": 2000,
            "timezone": "Europe/Moscow",
            "day_change_hour": 4,
            "created_at": now,
            "last_active_at": now,
        }
    )

    logged_date = "2026-07-27"
    inserted = db.insert_food_log(
        TEST_USER_ID,
        title="Овсянка тест",
        calories=420,
        proteins=14.0,
        fats=9.0,
        carbs=68.0,
        portion_g=300.0,
        logged_date=logged_date,
        details_json={"emoji": "🍳", "dish": "Овсянка тест", "status": "recognized"},
    )
    _ok(
        "POST food_log",
        inserted.get("title") == "Овсянка тест"
        and inserted.get("emoji") == "🍳"
        and inserted.get("logged_date") == logged_date,
        json.dumps(inserted, ensure_ascii=False),
    )
    log_id = int(inserted["id"])

    day_rows = db.get_food_logs_for_date(TEST_USER_ID, logged_date)
    _ok(
        "GET food_logs for date",
        any(r["id"] == log_id for r in day_rows),
        f"count={len(day_rows)}",
    )

    range_rows = db.get_food_logs_range(TEST_USER_ID, "2026-07-01", "2026-07-31")
    _ok(
        "GET food_logs range",
        any(r["id"] == log_id for r in range_rows),
        f"count={len(range_rows)}",
    )

    patched = db.update_food_log(
        TEST_USER_ID,
        log_id,
        title="Овсянка с бананом",
        calories=480,
        proteins=15.0,
        fats=10.0,
        carbs=78.0,
        portion_g=350.0,
        details_json={
            "emoji": "🍌",
            "dish": "Овсянка с бананом",
            "status": "recognized",
        },
        check_owner=False,
    )
    _ok(
        "PATCH food_log",
        patched is not None
        and patched.get("title") == "Овсянка с бананом"
        and int(patched.get("calories") or 0) == 480
        and patched.get("emoji") == "🍌",
        json.dumps(patched, ensure_ascii=False) if patched else "None",
    )

    deleted = db.delete_food_log(TEST_USER_ID, log_id)
    _ok("DELETE food_log", deleted is True)

    after = db.get_food_logs_for_date(TEST_USER_ID, logged_date)
    _ok("food_log gone after delete", all(r["id"] != log_id for r in after))

    db.delete_user(TEST_USER_ID)
    print("\n=== food_logs CRUD: all passed ===\n")


def test_food_logs_retention() -> None:
    """Старая запись (110 дней) удаляется cleanup'ом; свежая остаётся."""
    print("\n=== test_food_logs_retention ===\n")

    from datetime import datetime, timedelta, timezone

    try:
        db.delete_user(TEST_USER_ID)
    except db.NocoDBError:
        pass

    now = int(time.time())
    db.create_user(
        {
            "id": TEST_USER_ID,
            "first_name": "RetentionTest",
            "gender": "male",
            "age": 25,
            "height": 180.0,
            "weight": 80.0,
            "activity_level": 1.2,
            "goal": "maintain",
            "daily_calories": 2000,
            "timezone": "Europe/Moscow",
            "day_change_hour": 4,
            "created_at": now,
            "last_active_at": now,
        }
    )

    today = datetime.now(timezone.utc).date()
    old_date = (today - timedelta(days=110)).isoformat()
    fresh_date = today.isoformat()

    old = db.insert_food_log(
        TEST_USER_ID,
        title="Старое блюдо retention",
        calories=100,
        proteins=1.0,
        fats=1.0,
        carbs=1.0,
        portion_g=50.0,
        logged_date=old_date,
        details_json={"emoji": "🦴", "dish": "Старое блюдо retention"},
    )
    fresh = db.insert_food_log(
        TEST_USER_ID,
        title="Свежее блюдо retention",
        calories=200,
        proteins=2.0,
        fats=2.0,
        carbs=2.0,
        portion_g=100.0,
        logged_date=fresh_date,
        details_json={"emoji": "🥗", "dish": "Свежее блюдо retention"},
    )
    old_id = int(old["id"])
    fresh_id = int(fresh["id"])
    _ok("POST old + fresh food_logs", old_id > 0 and fresh_id > 0)

    deleted_n = db.delete_food_logs_older_than(100)
    _ok("cleanup deleted >= 1", deleted_n >= 1, f"deleted={deleted_n}")

    old_rows = db.get_food_logs_for_date(TEST_USER_ID, old_date)
    fresh_rows = db.get_food_logs_for_date(TEST_USER_ID, fresh_date)
    _ok(
        "old food_log gone",
        all(r["id"] != old_id for r in old_rows),
        f"count={len(old_rows)}",
    )
    _ok(
        "fresh food_log kept",
        any(r["id"] == fresh_id for r in fresh_rows),
        f"count={len(fresh_rows)}",
    )

    db.delete_food_log(TEST_USER_ID, fresh_id, check_owner=False)
    db.delete_user(TEST_USER_ID)
    print("\n=== food_logs retention: all passed ===\n")


# ---------------------------------------------------------------------------
# reminders
# ---------------------------------------------------------------------------


def test_reminders_crud() -> None:
    """Создать user → INSERT reminder → toggle → snooze → DELETE."""
    print("\n=== test_reminders_crud ===\n")

    try:
        db.delete_user(TEST_USER_ID)
    except db.NocoDBError:
        pass

    now = int(time.time())
    db.create_user(
        {
            "id": TEST_USER_ID,
            "first_name": "RemTest",
            "gender": "female",
            "age": 30,
            "height": 165.0,
            "weight": 60.0,
            "activity_level": 1.375,
            "goal": "weight_loss",
            "daily_calories": 1800,
            "timezone": "Europe/Moscow",
            "day_change_hour": 4,
            "created_at": now,
            "last_active_at": now,
        }
    )

    rem = db.add_reminder(
        TEST_USER_ID,
        title="Выпить Омега-3",
        time_start="07:00",
        time_end="11:00",
        min_calories=250,
    )
    _ok(
        "POST reminder",
        rem.get("title") == "Выпить Омега-3" and rem.get("is_active") is True,
        json.dumps(rem, ensure_ascii=False),
    )
    rem_id = int(rem["id"])

    listed = db.get_reminders(TEST_USER_ID)
    _ok("GET reminders list", any(r["id"] == rem_id for r in listed), f"count={len(listed)}")

    one = db.get_reminder(TEST_USER_ID, rem_id)
    _ok("GET reminder by id", one is not None and one["id"] == rem_id)

    toggled = db.set_reminder_active(TEST_USER_ID, rem_id, False)
    one2 = db.get_reminder(TEST_USER_ID, rem_id)
    _ok(
        "PATCH is_active=False",
        toggled is not None and one2 is not None and one2["is_active"] is False,
    )

    db.mark_reminder_triggered(rem_id)
    one3 = db.get_reminder(TEST_USER_ID, rem_id)
    _ok(
        "PATCH is_triggered_today=True",
        one3 is not None and one3["is_triggered_today"] is True,
    )

    snoozed = db.snooze_reminder(TEST_USER_ID, rem_id)
    one4 = db.get_reminder(TEST_USER_ID, rem_id)
    _ok(
        "snooze → is_triggered_today=False",
        snoozed and one4 is not None and one4["is_triggered_today"] is False,
    )

    deleted = db.delete_reminder(TEST_USER_ID, rem_id)
    after = db.get_reminders(TEST_USER_ID)
    _ok("DELETE reminder", deleted and all(r["id"] != rem_id for r in after))

    db.delete_user(TEST_USER_ID)
    print("\n=== reminders CRUD: all passed ===\n")


if __name__ == "__main__":
    # Полный прогон всех таблиц:
    test_users_crud()
    test_food_logs_crud()
    test_food_logs_retention()
    test_reminders_crud()
