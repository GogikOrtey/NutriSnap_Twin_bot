"""
Клиент NocoDB Data API v3 (SkyNode) для NutriSnap / NutriClick.

Зачем: единый HTTP-транспорт и хелперы таблиц users / food_logs / reminders
вместо in-memory stub в main.py.
Используется: main.py (профиль, дневник, напоминания, usage-reminder),
проверка запросов — через requests_DB_test.py до встраивания.
Опционально check_owner=False у update_food_log / delete_food_log /
set_reminder_active / delete_reminder / snooze_reminder — без ownership-GET,
когда id уже из FSM.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from dotenv import load_dotenv

load_dotenv()

NOCODB_BASE_URL = "https://skynode.nocodb.api.gogortey.ru"
NOCODB_BASE_ID = "p6iywpukq1yiryf"
TABLE_USERS = "meooj41uwpyrx9t"
TABLE_FOOD_LOGS = "mqhuz4edun8xpdc"
TABLE_REMINDERS = "m04n35tamrsu1wn"

API_KEY = os.getenv("NOCODB_SKYNODE_API_KEY", "").strip()


class NocoDBError(RuntimeError):
    """Ошибка HTTP/API NocoDB. Несёт status и тело ответа."""

    def __init__(self, message: str, *, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


# Собирает URL списка записей или одной записи таблицы NocoDB v3.
# Используется всеми хелперами CRUD в этом модуле.
def records_url(table_id: str, record_id: str | int | None = None) -> str:
    base = f"{NOCODB_BASE_URL}/api/v3/data/{NOCODB_BASE_ID}/{table_id}/records"
    if record_id is None:
        return base
    return f"{base}/{record_id}"


# Выполняет HTTP-запрос к NocoDB (xc-token, UTF-8 JSON).
# Используется хелперами таблиц; при ошибке HTTP поднимает NocoDBError.
def call_api(
    method: str,
    url: str,
    *,
    body: dict[str, Any] | list[Any] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any] | list[Any] | None:
    if not API_KEY:
        raise NocoDBError("NOCODB_SKYNODE_API_KEY не задан в .env")

    full_url = url
    if query:
        full_url = f"{url}?{urllib.parse.urlencode(query)}"

    data: bytes | None = None
    headers = {
        "accept": "application/json",
        "xc-token": API_KEY,
    }
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(
        full_url, data=data, headers=headers, method=method.upper()
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        status = e.code
        raw = e.read().decode("utf-8", errors="replace")
        parsed: Any = None
        try:
            parsed = json.loads(raw) if raw.strip() else None
        except json.JSONDecodeError:
            parsed = raw
        raise NocoDBError(
            f"NocoDB HTTP {status}: {raw[:500]}",
            status=status,
            body=parsed,
        ) from e
    except urllib.error.URLError as e:
        raise NocoDBError(f"NocoDB URL error: {e.reason}") from e

    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise NocoDBError(
            f"NocoDB: невалидный JSON (HTTP {status})",
            status=status,
            body=raw,
        ) from e


# Превращает запись NocoDB {id, fields} в плоский dict с ключом id.
# Используется мапперами users / food_logs / reminders.
def _flatten_record(rec: dict[str, Any]) -> dict[str, Any]:
    fields = dict(rec.get("fields") or {})
    rid = rec.get("id")
    if rid is not None:
        fields["id"] = rid
    return fields


# Достаёт первую запись из ответа list/create/update или None.
# Используется после POST/PATCH/GET list.
def _first_record(payload: dict[str, Any] | list[Any] | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    if isinstance(payload, list):
        if not payload:
            return None
        item = payload[0]
        return _flatten_record(item) if isinstance(item, dict) else None
    if isinstance(payload, dict):
        if "fields" in payload and "id" in payload:
            return _flatten_record(payload)
        records = payload.get("records")
        if isinstance(records, list) and records:
            return _flatten_record(records[0])
    return None


# Достаёт список плоских записей из ответа list.
# Используется get_food_logs_* / get_reminders.
def _list_records(payload: dict[str, Any] | list[Any] | None) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [_flatten_record(r) for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        records = payload.get("records")
        if isinstance(records, list):
            return [_flatten_record(r) for r in records if isinstance(r, dict)]
        if "fields" in payload:
            return [_flatten_record(payload)]
    return []


# Извлекает telegram user_id из поля связи users (object/list/scalar).
# Используется маппингом food_logs / reminders.
def _link_user_id(fields: dict[str, Any]) -> int | None:
    link = fields.get("users")
    if link is None:
        return None
    if isinstance(link, dict) and "id" in link:
        return int(link["id"])
    if isinstance(link, list) and link:
        first = link[0]
        if isinstance(first, dict) and "id" in first:
            return int(first["id"])
        return int(first)
    if isinstance(link, (int, float, str)):
        return int(link)
    return None


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------


# Нормализует профиль users к формату stub (плоский dict).
# Используется get_user / upsert_profile / PATCH-хелперами.
def _normalize_user(row: dict[str, Any]) -> dict[str, Any]:
    # Нет поля / null → включено (дефолт онбординга); явное false — выкл.
    ure_raw = row.get("usage_reminder_enabled")
    usage_reminder_enabled = True if ure_raw is None else bool(ure_raw)
    return {
        "id": int(row["id"]),
        "first_name": row.get("first_name") or "",
        "gender": row.get("gender") or "male",
        "age": int(row.get("age") or 0),
        "height": float(row.get("height") or 0),
        "weight": float(row.get("weight") or 0),
        "activity_level": float(row.get("activity_level") or 1.2),
        "goal": row.get("goal") or "maintain",
        "daily_calories": int(row.get("daily_calories") or 2000),
        "timezone": row.get("timezone") or "Europe/Moscow",
        "day_change_hour": int(row.get("day_change_hour") if row.get("day_change_hour") is not None else 4),
        "last_active_at": int(row.get("last_active_at") or 0),
        "created_at": int(row.get("created_at") or 0),
        "usage_reminder_enabled": usage_reminder_enabled,
        "usage_reminder_sent_on": str(row.get("usage_reminder_sent_on") or "").strip(),
    }


# GET users по Telegram id. Возвращает None, если записи нет (404).
# Используется /start, меню, опросом (upsert).
def get_user(user_id: int) -> dict[str, Any] | None:
    try:
        payload = call_api("GET", records_url(TABLE_USERS, user_id))
    except NocoDBError as e:
        if e.status == 404:
            return None
        raise
    row = _first_record(payload)
    if row is None:
        return None
    if "id" not in row:
        row["id"] = user_id
    return _normalize_user(row)


# POST новой записи users (id = Telegram ID, не autoincrement).
# Используется upsert_profile при первом прохождении опроса.
def create_user(fields: dict[str, Any]) -> dict[str, Any]:
    payload = call_api(
        "POST",
        records_url(TABLE_USERS),
        body={"fields": fields},
    )
    row = _first_record(payload)
    if row is None:
        raise NocoDBError("create_user: пустой ответ")
    return _normalize_user(row)


# PATCH полей users по id. Body: {id, fields}.
# Используется upsert_profile и точечными set_*.
def update_user(user_id: int, fields: dict[str, Any]) -> dict[str, Any]:
    payload = call_api(
        "PATCH",
        records_url(TABLE_USERS),
        body={"id": user_id, "fields": fields},
    )
    row = _first_record(payload)
    if row is None:
        # Некоторые ответы PATCH могут быть без records — перечитаем
        existing = get_user(user_id)
        if existing is None:
            raise NocoDBError(f"update_user: нет записи {user_id}")
        return existing
    if "id" not in row:
        row["id"] = user_id
    return _normalize_user(row)


# DELETE users по id (песочница / тесты). Body: {id}.
# Используется requests_DB_test.py для очистки тестовых записей.
def delete_user(user_id: int) -> None:
    call_api(
        "DELETE",
        records_url(TABLE_USERS),
        body={"id": user_id},
    )


# Создаёт или обновляет профиль из первичного опроса.
# Используется _on_survey_complete в main.py.
def upsert_profile(
    user_id: int,
    *,
    first_name: str,
    gender: str,
    age: int,
    height: float,
    weight: float,
    activity_level: float,
    goal: str,
    timezone: str,
    daily_calories: int,
) -> dict[str, Any]:
    now = int(time.time())
    fields = {
        "first_name": first_name,
        "gender": gender,
        "age": int(age),
        "height": float(height),
        "weight": float(weight),
        "activity_level": float(activity_level),
        "goal": goal,
        "timezone": timezone,
        "daily_calories": int(daily_calories),
        "last_active_at": now,
    }
    existing = get_user(user_id)
    if existing is None:
        return create_user(
            {
                "id": user_id,
                **fields,
                "day_change_hour": 4,
                "created_at": now,
                # Напоминание «не забыл ли бот» — вкл. с онбординга.
                "usage_reminder_enabled": True,
            }
        )
    return update_user(user_id, fields)


# PATCH users.last_active_at = now.
# Используется перед триггерами reminders и активностью в боте.
def touch_user_activity(user_id: int) -> None:
    update_user(user_id, {"last_active_at": int(time.time())})


# PATCH users.day_change_hour.
# Используется настройкой «Время смены суток».
def set_day_change_hour(user_id: int, hour: int) -> None:
    update_user(user_id, {"day_change_hour": int(hour)})


# PATCH users.goal.
# Используется настройкой «Тип отслеживания».
def set_goal(user_id: int, goal: str) -> None:
    update_user(user_id, {"goal": goal})


# PATCH users.daily_calories.
# Используется настройкой «Целевые ккал».
def set_daily_calories(user_id: int, calories: int) -> None:
    update_user(user_id, {"daily_calories": int(calories)})


# PATCH users.usage_reminder_enabled (напоминание «зафиксируй еду до 13:00»).
# Используется экраном настроек «Напоминание использования бота».
def set_usage_reminder_enabled(user_id: int, enabled: bool) -> dict[str, Any]:
    return update_user(user_id, {"usage_reminder_enabled": bool(enabled)})


# PATCH users.usage_reminder_sent_on = YYYY-MM-DD (антидубль за сутки).
# Используется фоновым чекером usage-reminder после успешной отправки.
def mark_usage_reminder_sent(user_id: int, sent_on: str) -> dict[str, Any]:
    return update_user(user_id, {"usage_reminder_sent_on": str(sent_on)})


# Список пользователей с включённым usage-reminder (пагинация page).
# Используется фоновым чекером «нет еды до 13:00» в main.py.
def list_users_with_usage_reminder(*, page_size: int = 200) -> list[dict[str, Any]]:
    # Checkbox: true/1. Старые записи без поля — не попадут, пока не выставят
    # дефолт в NocoDB или не пройдут upsert (create ставит True).
    where = "(usage_reminder_enabled,eq,true)"
    out: list[dict[str, Any]] = []
    page = 1
    while True:
        payload = call_api(
            "GET",
            records_url(TABLE_USERS),
            query={
                "where": where,
                "pageSize": str(page_size),
                "page": str(page),
            },
        )
        rows = _list_records(payload)
        for row in rows:
            if "id" not in row:
                continue
            user = _normalize_user(row)
            if user.get("usage_reminder_enabled"):
                out.append(user)
        if len(rows) < page_size:
            break
        page += 1
        if page > 500:
            break
    return out


# ---------------------------------------------------------------------------
# food_logs
# ---------------------------------------------------------------------------


# Нормализует food_logs: emoji из details_json, user_id из связи users.
# Используется get_food_logs_* / insert_food_log.
def _normalize_food_log(row: dict[str, Any]) -> dict[str, Any]:
    details = row.get("details_json")
    emoji = ""
    if isinstance(details, dict):
        emoji = str(details.get("emoji") or "").strip()
    elif isinstance(details, str) and details.strip():
        try:
            parsed = json.loads(details)
            if isinstance(parsed, dict):
                emoji = str(parsed.get("emoji") or "").strip()
                details = parsed
        except json.JSONDecodeError:
            pass

    uid = _link_user_id(row)
    return {
        "id": int(row["id"]),
        "user_id": int(uid) if uid is not None else 0,
        "emoji": emoji,
        "title": row.get("title") or "",
        "calories": int(row.get("calories") or 0),
        "proteins": float(row.get("proteins") or 0),
        "fats": float(row.get("fats") or 0),
        "carbs": float(row.get("carbs") or 0),
        "portion_g": float(row.get("portion_g") or 0),
        "logged_date": row.get("logged_date") or "",
        "created_at": int(row.get("created_at") or 0),
        "details_json": details,
    }


# Собирает query sort для NocoDB v3: JSON [{"field","direction"}].
# Используется list food_logs / reminders.
def _sort_param(*fields: str) -> str:
    return json.dumps(
        [{"field": f, "direction": "asc"} for f in fields],
        ensure_ascii=False,
    )


# GET food_logs с where/sort. Используется фильтрами по дате и user.
def _list_food_logs(*, where: str, sort_fields: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    query: dict[str, str] = {"where": where, "pageSize": "200"}
    if sort_fields:
        query["sort"] = _sort_param(*sort_fields)
    payload = call_api("GET", records_url(TABLE_FOOD_LOGS), query=query)
    rows = _list_records(payload)
    return [_normalize_food_log(r) for r in rows]


# Записи дневника за логическую дату YYYY-MM-DD.
# Используется главным меню, дневником, удалением и выгрузкой.
def get_food_logs_for_date(user_id: int, logged_date: str) -> list[dict[str, Any]]:
    where = (
        f"(users,eq,{user_id})~and(logged_date,eq,{logged_date})"
    )
    rows = _list_food_logs(where=where, sort_fields=("created_at",))
    return sorted(rows, key=lambda r: r["created_at"])


# Записи за диапазон дат [date_from, date_to] включительно.
# Используется выгрузкой журнала за неделю/месяц.
def get_food_logs_range(
    user_id: int, date_from: str, date_to: str
) -> list[dict[str, Any]]:
    where = (
        f"(users,eq,{user_id})"
        f"~and(logged_date,ge,{date_from})"
        f"~and(logged_date,le,{date_to})"
    )
    rows = _list_food_logs(where=where, sort_fields=("logged_date", "created_at"))
    return sorted(rows, key=lambda r: (r["logged_date"], r["created_at"]))


# INSERT в food_logs после ✅ распознавания. Связь: users: {id}.
# Используется _on_food_saved / persist в main.
def insert_food_log(
    user_id: int,
    *,
    title: str,
    calories: int,
    proteins: float,
    fats: float,
    carbs: float,
    portion_g: float,
    logged_date: str,
    details_json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "title": title,
        "calories": int(calories),
        "proteins": float(proteins),
        "fats": float(fats),
        "carbs": float(carbs),
        "portion_g": float(portion_g),
        "logged_date": logged_date,
        "created_at": int(time.time()),
        "users": {"id": user_id},
    }
    if details_json is not None:
        fields["details_json"] = details_json
    payload = call_api(
        "POST",
        records_url(TABLE_FOOD_LOGS),
        body={"fields": fields},
    )
    row = _first_record(payload)
    if row is None:
        raise NocoDBError("insert_food_log: пустой ответ")
    return _normalize_food_log(row)


# PATCH полей food_logs по id. check_owner=True → GET и проверка связи users.
# Используется флоу «Изменить блюдо» после правок через Gemini (из FSM — check_owner=False).
def update_food_log(
    user_id: int,
    log_id: int,
    *,
    title: str,
    calories: int,
    proteins: float,
    fats: float,
    carbs: float,
    portion_g: float,
    details_json: dict[str, Any] | None = None,
    check_owner: bool = True,
) -> dict[str, Any] | None:
    if check_owner:
        try:
            payload = call_api("GET", records_url(TABLE_FOOD_LOGS, log_id))
        except NocoDBError as e:
            if e.status == 404:
                return None
            raise
        row = _first_record(payload)
        if row is None:
            return None
        owner = _link_user_id(row)
        if owner is not None and int(owner) != int(user_id):
            return None

    fields: dict[str, Any] = {
        "title": title,
        "calories": int(calories),
        "proteins": float(proteins),
        "fats": float(fats),
        "carbs": float(carbs),
        "portion_g": float(portion_g),
    }
    if details_json is not None:
        fields["details_json"] = details_json

    payload = call_api(
        "PATCH",
        records_url(TABLE_FOOD_LOGS),
        body={"id": log_id, "fields": fields},
    )
    row = _first_record(payload)
    if row is None:
        # Некоторые ответы PATCH без records — перечитаем
        try:
            payload = call_api("GET", records_url(TABLE_FOOD_LOGS, log_id))
        except NocoDBError as e:
            if e.status == 404:
                return None
            raise
        row = _first_record(payload)
        if row is None:
            return None
    if "id" not in row:
        row["id"] = log_id
    return _normalize_food_log(row)


# DELETE food_logs по id. check_owner=True → GET и проверка связи users.
# Используется флоу «Удалить блюдо» (из FSM можно check_owner=False).
def delete_food_log(
    user_id: int, log_id: int, *, check_owner: bool = True
) -> bool:
    if check_owner:
        try:
            payload = call_api("GET", records_url(TABLE_FOOD_LOGS, log_id))
        except NocoDBError as e:
            if e.status == 404:
                return False
            raise
        row = _first_record(payload)
        if row is None:
            return False
        owner = _link_user_id(row)
        if owner is not None and int(owner) != int(user_id):
            return False
    call_api(
        "DELETE",
        records_url(TABLE_FOOD_LOGS),
        body={"id": log_id},
    )
    return True


# ---------------------------------------------------------------------------
# reminders
# ---------------------------------------------------------------------------


# Нормализует reminder к формату stub.
# Используется get_reminders / add_reminder / trigger.
def _normalize_reminder(row: dict[str, Any]) -> dict[str, Any]:
    uid = _link_user_id(row)
    return {
        "id": int(row["id"]),
        "user_id": int(uid) if uid is not None else 0,
        "title": row.get("title") or "",
        "time_start": row.get("time_start") or "",
        "time_end": row.get("time_end") or "",
        "min_calories": int(row.get("min_calories") or 0),
        "is_triggered_today": bool(row.get("is_triggered_today")),
        "is_active": bool(row.get("is_active") if row.get("is_active") is not None else True),
    }


# Список напоминаний пользователя (ORDER BY id).
# Используется экранами «Напоминания».
def get_reminders(user_id: int) -> list[dict[str, Any]]:
    where = f"(users,eq,{user_id})"
    payload = call_api(
        "GET",
        records_url(TABLE_REMINDERS),
        query={
            "where": where,
            "sort": _sort_param("id"),
            "pageSize": "200",
        },
    )
    rows = [_normalize_reminder(r) for r in _list_records(payload)]
    return sorted(rows, key=lambda r: r["id"])


# Все reminders (пагинация page) — для фонового сброса суток и missed-check.
# Используется reminders_maintenance_loop в main.py.
def list_all_reminders(*, page_size: int = 200) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    page = 1
    while True:
        payload = call_api(
            "GET",
            records_url(TABLE_REMINDERS),
            query={
                "pageSize": str(page_size),
                "page": str(page),
                "sort": _sort_param("id"),
            },
        )
        rows = _list_records(payload)
        for row in rows:
            rem = _normalize_reminder(row)
            if rem.get("user_id"):
                out.append(rem)
        if len(rows) < page_size:
            break
        page += 1
        if page > 500:
            break
    return out


# Одно напоминание по id (только своё).
# Используется карточкой reminder / toggle / delete / snooze.
def get_reminder(user_id: int, reminder_id: int) -> dict[str, Any] | None:
    try:
        payload = call_api("GET", records_url(TABLE_REMINDERS, reminder_id))
    except NocoDBError as e:
        if e.status == 404:
            return None
        raise
    row = _first_record(payload)
    if row is None:
        return None
    rem = _normalize_reminder(row)
    if rem["user_id"] and rem["user_id"] != int(user_id):
        return None
    return rem


# INSERT reminders + link users.
# Используется флоу «➕ Добавить напоминание».
def add_reminder(
    user_id: int,
    title: str,
    time_start: str,
    time_end: str,
    min_calories: int,
) -> dict[str, Any]:
    fields = {
        "title": title,
        "time_start": time_start,
        "time_end": time_end,
        "min_calories": int(min_calories),
        "is_triggered_today": False,
        "is_active": True,
        "users": {"id": user_id},
    }
    payload = call_api(
        "POST",
        records_url(TABLE_REMINDERS),
        body={"fields": fields},
    )
    row = _first_record(payload)
    if row is None:
        raise NocoDBError("add_reminder: пустой ответ")
    return _normalize_reminder(row)


# PATCH произвольных полей reminder.
# Используется set_active / snooze / trigger.
def update_reminder(reminder_id: int, fields: dict[str, Any]) -> dict[str, Any] | None:
    payload = call_api(
        "PATCH",
        records_url(TABLE_REMINDERS),
        body={"id": reminder_id, "fields": fields},
    )
    row = _first_record(payload)
    if row is None:
        return None
    return _normalize_reminder(row)


# Вкл/выкл reminders.is_active. check_owner=False — без GET (id из FSM).
# Возвращает обновлённую строку или None. Используется карточкой напоминания.
def set_reminder_active(
    user_id: int,
    reminder_id: int,
    is_active: bool,
    *,
    check_owner: bool = True,
) -> dict[str, Any] | None:
    base: dict[str, Any] | None = None
    if check_owner:
        base = get_reminder(user_id, reminder_id)
        if base is None:
            return None
    updated = update_reminder(reminder_id, {"is_active": bool(is_active)})
    if updated is not None:
        if base is not None:
            merged = dict(base)
            merged["is_active"] = bool(is_active)
            return merged
        return updated
    if base is not None:
        merged = dict(base)
        merged["is_active"] = bool(is_active)
        return merged
    return {
        "id": int(reminder_id),
        "user_id": int(user_id),
        "is_active": bool(is_active),
    }


# DELETE reminder. check_owner=False — без GET (id из FSM).
# Используется карточкой «Мои напоминания».
def delete_reminder(
    user_id: int, reminder_id: int, *, check_owner: bool = True
) -> bool:
    if check_owner:
        row = get_reminder(user_id, reminder_id)
        if row is None:
            return False
    call_api(
        "DELETE",
        records_url(TABLE_REMINDERS),
        body={"id": reminder_id},
    )
    return True


# Сброс is_triggered_today (snooze «на следующую еду»).
# Используется inline под уведомлением.
def snooze_reminder(
    user_id: int, reminder_id: int, *, check_owner: bool = True
) -> bool:
    if check_owner:
        row = get_reminder(user_id, reminder_id)
        if row is None:
            return False
    update_reminder(reminder_id, {"is_triggered_today": False})
    return True


# Пометить reminder как сработавший сегодня.
# Используется trigger_reminders_for_food.
def mark_reminder_triggered(reminder_id: int) -> None:
    update_reminder(reminder_id, {"is_triggered_today": True})
