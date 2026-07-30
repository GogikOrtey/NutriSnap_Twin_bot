# NutriSnap Twin Bot — контекст проекта

Telegram-бот для учёта калорий по фото/тексту (NutriSnap). Актуальная точка входа: `main.py`. Распознавание еды — в `food_recognition.py`.

## Назначение

Пользователь присылает фото блюда (опционально с подписью) или текст → Gemini возвращает структурированный JSON со `status` → бот ведёт по веткам (unclear / no_food / label / recognized) → превью с кнопками и автоподтверждением → ✅ пишет в NocoDB `food_logs` (+ консоль). Главное меню / дневник / настройки / напоминания — данные из NocoDB через `db_nocodb.py`.

## Структура

```
NutriSnap_Twin_bot/
├── main.py                # Точка входа: /start, меню, обёртки БД, роутеры
├── app_logging.py         # Логи: консоль + logs/bot.log (ротация)
├── error_notify.py        # Ошибки → лог + SMTP-письмо 🟨⬛🍎 на FEEDBACK_TO_EMAIL
├── proxy_config.py        # Точечный HTTPS-прокси для Telegram + Gemini (VPS)
├── db_nocodb.py           # Клиент NocoDB Data API v3 (users / food_logs / reminders)
├── initial_survey.py      # Первичный опрос: профиль → цель → timezone → ккал
├── food_recognition.py    # FSM-флоу распознавания еды (Gemini + confirm UI)
├── requests_DB_test.py    # Песочница: test_users/food_logs/reminders_crud
├── requests_DB_latency_test.py  # Песочница: GET users + latency_ms
├── nutriclip_DB_swagger_documentation.json  # OpenAPI 3.1 NocoDB (источник правды)
├── test_3_local.py        # Локальный прототип анализа фото (без Telegram)
├── test_4_tg_bot.py       # Минимальный каркас бота (/start + кнопка)
├── img_1.jpg              # Тестовое фото для локального запуска
├── requirements.txt       # pip-зависимости (в т.ч. aiohttp-socks)
├── Dockerfile             # Образ Python 3.10-slim → python main.py
├── docker-compose.yml     # Сервис bot: env_file .env, volumes logs + day keys
├── docker-compose.vps.yml # VPS override: network_mode host (mihomo + /etc/hosts)
├── deploy.sh              # На VPS: git reset origin/build + compose up --build -d
├── deploy_to_build.cmd    # Локально: merge в build, push → триггер Actions
├── .github/workflows/deploy.yml  # push build → SSH → deploy.sh
├── .dockerignore          # .env, logs, Старое/, тесты, *.md — вне контекста сборки
├── .env                   # Секреты (не в git; в образ не копируется)
├── .env.example           # Шаблон переменных окружения (+ LOG_LEVEL)
├── .gitignore             # в т.ч. logs/, *.log
├── logs/                  # bot.log (+ ротация; не в git; volume в compose)
├── .reminder_day_keys.json  # Локальный ключ сброса is_triggered_today (не в git; volume)
├── .reminder_pending_meal.json  # Отложенные после пропуска «на ближайший приём» (volume)
├── cursorcontext_on_SkyNode.md  # SSH/VPS шпаргалка (SkyNode)
├── backlog.md             # Личный бэклог (не править агентом)
└── Старое/                # Устаревшие эксперименты (не использовать как основу)
```

## Связь модулей

```
main.py
  ├── Bot + AiohttpSession(proxy?) + Dispatcher + MemoryStorage
  ├── app_logging.setup_logging()           # консоль + logs/bot.log
  ├── proxy_config.py                   # TELEGRAM_/GEMINI_/OUTBOUND_HTTPS_PROXY
  ├── error_notify.py                   # report_console_error + хуки → лог + SMTP 🟨⬛🍎
  ├── /start → db.get_user? опрос : меню  (INITIAL_SURVEY_ENABLED=False)
  ├── db_nocodb.py                      # HTTP xc-token → NocoDB (без прокси)
  ├── include_router(menu_router)
  ├── include_router(setup_initial_survey(on_complete=_on_survey_complete))
  │     └── … → usage-reminder + «🟩 Хорошо» (+ параллельный set_profile)
  │           → закрепить бота (таймер 15с → «Готово») → show_recognize
  └── include_router(setup_food_recognition(..., on_food_saved=_on_food_saved,
        │                                    on_after_food_ack=_on_after_food_ack))
        └── persist → INSERT food_logs → «Учтено ✅» → notify reminders
```

## Актуальная логика

### `main.py`

- Библиотека: `aiogram` 3.x + `MemoryStorage` (FSM), long polling.
- Проверка `TELEGRAM_BOT_API_KEY` / `GEMINI_API_KEY`, старт polling.
- VPS: `AiohttpSession(proxy=TELEGRAM_PROXY|OUTBOUND_HTTPS_PROXY)` — polling и скачивание фото через mihomo `127.0.0.1:7890`. Без переменных — напрямую (локалка). Не ставить глобальный `HTTP_PROXY` на процесс.
- `DropStaleMessagesMiddleware` (`dp.message.outer_middleware`): сообщения старше 10 мин (`STALE_MESSAGE_MAX_AGE_SEC`) не обрабатываются (очередь после даунтайма); чату один раз за 60 с пишется `STALE_RECOVERY_TEXT` («был офлайн — пришлите ещё раз»).
- `INITIAL_SURVEY_ENABLED = False`: `/start` смотрит NocoDB `users` — нет записи → опрос, есть → главное меню. Флаг `True` принудительно всегда открывает опрос (отладка UI).
- `/start` и `dispatch_start`: нет профиля → опрос, есть → меню. `get_user` без записи → `UserNotRegisteredError`; `@dp.error` ловит и зовёт `dispatch_start` (как /start).
- Прочие необработанные update-ошибки → `@dp.error`: внешние (NocoDB/Gemini/сеть) → `TECH_ISSUES_USER_TEXT` + письмо `🟧🍎`; иначе `report_console_error` (`🟨⬛🍎`).
- `/start` (есть профиль) → «🏠 Главный экран | Сегодня» из реальных `food_logs`.
- Главный экран idle: `show_main_menu` / `refresh_main_menu_card` ставят таймер `MAIN_IDLE_REFRESH_SEC` (15 мин); если всё ещё `menu_screen=main` и тот же `ui_message_id` — `edit_message_reply_markup` с inline «🔄 Обновить» (`CALLBACK_MAIN_REFRESH`). Клик → пересборка карточки «сегодня» на месте и новый таймер. Задачи в `_main_idle_refresh_tasks`.
- Дневник / выгрузка / настройки / reminders — через обёртки `get_user` (кэш), `get_food_logs_*`, `set_*` (+ invalidate), `add_reminder`, … → `db_nocodb`. Из async-хендлеров — только через `run_db(...)` (`asyncio.to_thread`), чтобы urllib NocoDB не блокировал event loop.
- Кэш `users` в `main.py`: `_user_cache` по telegram_id; `set_*` / `set_profile` — optimistic `_patch_user_cache` / `_put_user_cache` (без лишнего GET); `invalidate_user_cache` — fallback если кэша ещё нет. Singleflight + `_user_gen` для гонок.
- Меньше roundtrip: `trigger_reminders_for_food` — один list + параллельные PATCH; `show_settings` — user∥reminders; toggle/delete reminder и delete food_log из FSM без ownership-GET; списки reminders/food_logs в FSM на пагинации.
- После ✅ еды: `_on_food_saved` → `insert_food_log_from_result` (emoji в `details_json` + PATCH `users.last_food_logged_on`); затем «Учтено ✅»; затем `_on_after_food_ack` → `notify_reminders_after_food` / `trigger_reminders_for_food` (чтобы reminders шли после ack в чате).
- Дневник «✏️ Изменить блюдо»: выбор номера → свободный текст → Gemini (`analyze_food_log_edit`) → `update_food_log` + сообщение об успехе. Удаление — сразу DELETE + «✅ Блюдо удалено».
- `_on_survey_complete`: сразу текст про usage-reminder + inline «🟩 Хорошо»; `set_profile` стартует параллельно (`_survey_profile_saves`). По кнопке — await upsert → напоминание закрепить/положить в важную папку (`SURVEY_PIN_REMINDER_TEXT`) с inline «Жду выполнения (15)»…«(1)» → «Готово» (клик во время отсчёта игнорируется) → «всё настроено» + `show_recognize`.
- Usage-reminder: если `usage_reminder_enabled` и до 13:00 (TZ юзера) `last_food_logged_on` ≠ логический today — фоновый `usage_reminder_loop` (раз в час, старт sleep 40 с) шлёт сообщение и пишет `usage_reminder_sent_on`. Тик: только `list_users_with_usage_reminder` (без N+1 к food_logs). Вкл/выкл: Настройки → Напоминания → «📲 Напоминание использования бота».
- Reminders maintenance (`reminders_maintenance_loop`, тик в `:05` каждого часа серверного времени — `REMINDER_MISSED_CHECK_MINUTE=5`): `list_all_reminders` + `list_all_users` (без per-user GET) → сброс `is_triggered_today` при смене логических суток; ключ даты в `.reminder_day_keys.json`. Пропущенные окна: `now > time_end`, ещё не triggered, не frozen, не в pending → «⏰ Напоминание пропущено» + mark triggered; кнопки «➡️ Перенести на ближайший приём» (`rem:defer:`) и «✅ Понятно».
- Defer после пропуска: `defer_reminder_to_next_meal` пишет в `.reminder_pending_meal.json` (`reminder_id → user_id/time_start/min_calories/missed_on`), `is_triggered_today` не сбрасывает. `trigger_reminders_for_food` шлёт уведомление на ближайшую еду вне окна (по `min_calories`). Если до `time_start` следующего логического дня еды не было — silent expire (ничего в чат), окно снова активное.
- Food logs retention (`food_logs_cleanup_loop`, раз в сутки / 86400 с, старт sleep 55 с): `delete_food_logs_older_than(FOOD_LOG_RETENTION_DAYS=100)` — удаляет записи с `logged_date` &lt; today_UTC−100; в лог пишет число удалённых.
- Профиль: «🔄 Обновить данные пользователя» → `start_initial_survey`. Смена goal → пересчёт ккал через `resolve_recommended_calories` (Mifflin–St Jeor + Gemini fallback).
- Версия бота: константа `BOT_VERSION` (`v1.0.0`) в `main.py`; Настройки → «🔩 Для разработчика» → сообщение «Версия бота: …».
- ReplyKeyboard / Inline / логическая дата / FAQ / SMTP feedback — без изменений UX.
- `main()`: `setup_logging` + `install_error_email_hooks` + `attach_asyncio_error_handler` + `usage_reminder_loop` + `reminders_maintenance_loop` + `food_logs_cleanup_loop` перед polling.

### `app_logging.py`

- `setup_logging()` — root logger: StreamHandler + RotatingFileHandler (`logs/bot.log`, 5 МБ × 5 файлов).
- Формат: `YYYY-MM-DD HH:MM:SS | LEVEL | module | message`.
- Уровень из `LOG_LEVEL` (по умолчанию INFO); aiogram/aiohttp/httpx/google_genai → WARNING.
- Модули: `logger = logging.getLogger(__name__)`.
- При тестировании на windows (cp1251): символ `→` в лог-сообщениях ломает вывод консоли (`UnicodeEncodeError`); в текстах логов использовать ASCII `->`.

### `error_notify.py`

- `report_console_error(msg, exc=?)` — logger.error + SMTP, тема `🟨⬛🍎 [NutriClick] ошибка в консоли`.
- `report_service_problem(msg, exc=?)` — logger.error + SMTP, тема `🟧🍎 [NutriClick] проблема с внешним сервисом` (БД / Gemini / сеть).
- `is_external_service_error` / `report_error_auto` — классификация; при внешнем сбое UX = `TECH_ISSUES_USER_TEXT` в чате.
- `notify_user_tech_issues` / `notify_user_tech_issues_from_event` — текст пользователю.
- Антиспам: одинаковый текст не чаще раза в 5 мин; сбой SMTP → только лог (без рекурсии).
- Хуки: `sys.excepthook`, `threading.excepthook`, asyncio exception handler.
- Используется из main / food_recognition / initial_survey.

### `db_nocodb.py`

- Транспорт: `urllib` + `xc-token` + UTF-8 JSON; `NocoDBError` при HTTP ≠ 2xx.
- Table IDs: users=`meooj41uwpyrx9t`, food_logs=`mqhuz4edun8xpdc`, reminders=`m04n35tamrsu1wn`.
- CRUD: `get_user` / `create_user` / `update_user` / `delete_user` / `upsert_profile`; food_logs list/insert/`update_food_log`/delete + `delete_food_logs_older_than` (retention по `logged_date`); reminders CRUD + toggle/snooze/mark_triggered + `list_all_reminders`; usage-reminder: `set_usage_reminder_enabled` / `mark_usage_reminder_sent` / `mark_last_food_logged_on` / `list_users_with_usage_reminder` / `list_all_users`.
- Связь владельца: поле Link `users: {id: telegram_id}` (не колонка `user_id`).
- `sort` v3: JSON `[{"field":"…","direction":"asc"}]`.
- `emoji` только внутри `details_json`; при чтении поднимается в плоское поле для UI.
- Enum как в боте: `gender` male/female, `goal` weight_loss/muscle_gain/maintain.
- Retention: `FOOD_LOG_RETENTION_DAYS=100`; cleanup list `where (logged_date,lt,cutoff)` + bulk DELETE батчами по 100 id.

### `requests_DB_test.py`

- Песочница поверх `db_nocodb`: `test_users_crud`, `test_food_logs_crud`, `test_food_logs_retention`, `test_reminders_crud`.
- Правило: новый запрос сначала прогонять здесь (2xx + до/после для DELETE), потом встраивать в бота. После 5 неудач подряд при верной доке — стоп, проверить NocoDB IDE.

### `requests_DB_latency_test.py`

- Урезанная песочница: один `GET users` через `call_api` + замер `perf_counter` → `latency_ms`.
- Запуск: `python requests_DB_latency_test.py`.

### `initial_survey.py`

- Router `initial_survey` + FSM `SurveyFlow`: `welcome` → … → `timezone` / `timezone_location_wait` / `timezone_city` → `calories_confirm` / `calories_edit`.
- Локация двухшагово: «Поделиться локацией» (текст) → `request_location` + таймер 7 с; при тишине (GPS выкл., Telegram не шлёт update) — подсказка включить GPS.
- `on_complete` в main: текст usage-reminder + параллельный `set_profile` → «🟩 Хорошо» → закрепить бота (таймер кнопки 15с) → «всё настроено» + `show_recognize`.

### `proxy_config.py`

- Точечный прокси только для Telegram и Gemini (VPS mihomo без TUN).
- Env: `OUTBOUND_HTTPS_PROXY`, алиасы `TELEGRAM_PROXY` / `GEMINI_HTTPS_PROXY`.
- `make_gemini_client`: `HttpOptions(client_args={proxy, trust_env=False})` — без глобального proxy env.
- NocoDB (`urllib`), SMTP, Nominatim (`geopy`) — **без** этого прокси.

### `food_recognition.py`

- Клиент Gemini через `make_gemini_client` (прокси на VPS).
- `_generate_with_fallback`: промежуточные сбои моделей (503/504 и т.п.) — только `logger.warning`, без письма; на почту уходит, когда все попытки исчерпаны и хендлер зовёт `report_service_problem` (пустой/неразобранный ответ). Принимает `response_schema` (FoodResult / FoodLogEditResult).
- `analyze_food_log_edit` + `parse_food_log_edit`: свободная текстовая правка записи дневника (status applied/unclear/irrelevant) → UPDATE в main.
- Фото перед Files API: `prepare_image_for_gemini` (Pillow) — EXIF-ориентация, длинная сторона ≤1024px, JPEG q=80; вызывается из `download_photo_temp`.
- Без изменений флоу Gemini/FSM распознавания; `persist_confirmed_food` → консоль + `on_food_saved` (INSERT в main); после «Учтено ✅» → `notify_after_food_ack` / `on_after_food_ack` (reminders).
- Статус анализа (`send_analysis_status`): «✨ Анализирую…» сразу с `ReplyKeyboardRemove` (без пустого stub+delete). Edit в превью обычно ок; fallback — новое сообщение.
- Меню «✏️ Изменить» (`editing_choice`): сверху reply «✅ Всё верно, добавить так» (`BTN_EDIT_CONFIRM` / `on_edit_confirm`) — сохранить текущий `result` без правок (если случайно зашли в edit после превью; inline ✅ уже недоступен).

## Переменные окружения

| Переменная | Назначение |
|---|---|
| `GEMINI_API_KEY` | Ключ Google AI Studio / Gemini |
| `TELEGRAM_BOT_API_KEY` | Токен бота @nutrisnap_ultra_bot (BotFather) |
| `OUTBOUND_HTTPS_PROXY` | Общий локальный прокси для TG+Gemini (VPS: `http://127.0.0.1:7890`) |
| `TELEGRAM_PROXY` | Опц. алиас прокси только для aiogram |
| `GEMINI_HTTPS_PROXY` | Опц. алиас прокси только для google-genai |
| `FEEDBACK_TO_EMAIL` | Куда слать отзывы и письма об ошибках (по умолчанию gog.ortey@yandex.ru) |
| `SMTP_HOST` / `SMTP_PORT` | SMTP-сервер (по умолчанию smtp.yandex.ru:465) |
| `SMTP_USER` / `SMTP_PASSWORD` | Логин и пароль приложения Yandex (отзывы; ошибки `🟨⬛🍎` / сервисы `🟧🍎`) |
| `NOCODB_SKYNODE_API_KEY` | API-токен NocoDB (SkyNode VPS); токены: `https://skynode.nocodb.api.gogortey.ru/account/tokens` |

## База данных (NocoDB / SkyNode)

- БД развёрнута на VPS **SkyNode**; доступ из бота — **только через API NocoDB** (не прямой SQL).
- База API: `https://skynode.nocodb.api.gogortey.ru`
- Base ID: `p6iywpukq1yiryf`
- Swagger: `https://skynode.nocodb.api.gogortey.ru/api/v3/meta/bases/p6iywpukq1yiryf/swagger`
- Ключ: `NOCODB_SKYNODE_API_KEY` в `.env` → заголовок `xc-token`.
- Data API v3: `/api/v3/data/{baseId}/{tableId}/records` (+ `/{recordId}` для одной записи).
- Table IDs: `users`=`meooj41uwpyrx9t`, `food_logs`=`mqhuz4edun8xpdc`, `reminders`=`m04n35tamrsu1wn`.
- Имена таблиц в NocoDB: `NutriClip_tg_bot_users` / `_food_logs` / `_reminders`.
- Связи (LinkToAnotherRecord), не SQL `user_id`: у users → `food_logs`=`cpzklvizorll7e2`, `reminders`=`cghqt6ph326wsmn`; у food_logs → `users`=`ciru4l8rjn8ak0p`; у reminders → `users`=`cii2y6a4f443637`. В ответе связь — поле `users: {id}` / массивы `food_logs`/`reminders`.
- Полный OpenAPI 3.1: `nutriclip_DB_swagger_documentation.json`.
- Формат ответа list: `{ "records": [ { "id", "id_fields", "fields": {...} } ], "nestedNext" }`.
- Песочница: `python requests_DB_test.py`.
- UTF-8: `json.dumps(..., ensure_ascii=False).encode("utf-8")`; на Windows — `sys.stdout.reconfigure(encoding="utf-8")`.
- Клиент бота: `db_nocodb.py` (подключён). Маркер `🔰` снят у рабочих мест; незавершённая логика — `🎈`.

### Таблица `users` (профиль)

| Поле | Тип | Назначение |
|---|---|---|
| `id` | BIGINT PK | Telegram ID (`from_user.id`) |
| `first_name` | VARCHAR(100) | Имя |
| `gender` | VARCHAR(10) | `'male'` / `'female'` |
| `age` | INT | Возраст (для нормы ккал) |
| `height` | FLOAT | Рост, см |
| `weight` | FLOAT | Вес, кг |
| `activity_level` | FLOAT | Коэфф. активности (1.2, 1.375, 1.55…) |
| `goal` | VARCHAR(20) | `'weight_loss'` / `'muscle_gain'` / `'maintain'` |
| `daily_calories` | INT | Целевая суточная норма ккал |
| `timezone` | VARCHAR(50) | IANA, напр. `'Europe/Moscow'` |
| `day_change_hour` | INT (default 4) | Час смены суток (04:00) |
| `last_active_at` | BIGINT | Unix time последней активности (заморозка >3 дней) |
| `created_at` | BIGINT | Unix time регистрации |
| `usage_reminder_enabled` | BOOLEAN (default True) | Напоминание «нет еды до 13:00» |
| `usage_reminder_sent_on` | DATE / VARCHAR | Дата YYYY-MM-DD последней отправки usage-reminder |
| `last_food_logged_on` | DATE | Логическая дата YYYY-MM-DD последней зафиксированной еды (usage-reminder без N+1) |

### Таблица `food_logs` (подтверждённые приёмы пищи)

Поля `title`/`calories`/`proteins`/`fats`/`carbs`/`portion_g` ↔ `FoodResult` (`dish` → `title`). Связь владельца — Link `users`. `emoji` — в `details_json`.

| Поле | Тип | Назначение |
|---|---|---|
| `id` | BIGINT PK | Автоинкремент |
| `title` | VARCHAR(255) | Название блюда (`FoodResult.dish`) |
| `calories` | INT | ккал |
| `proteins` / `fats` / `carbs` | FLOAT | БЖУ, г |
| `portion_g` | FLOAT | Вес порции, г |
| `logged_date` | VARCHAR(10) | Логическая дата `YYYY-MM-DD` |
| `created_at` | BIGINT | Unix time добавления |
| `details_json` | JSON | Сырой JSON Gemini (в т.ч. `emoji`) |
| `users` | Link | `{id: telegram_id}` |

### Таблица `reminders` (напоминания / витамины)

| Поле | Тип | Назначение |
|---|---|---|
| `id` | BIGINT PK | Автоинкремент |
| `title` | VARCHAR(255) | Текст («Выпить Омега-3») |
| `time_start` / `time_end` | VARCHAR(5) | Окно `'HH:MM'` |
| `min_calories` | INT (default 0) | `0` — любая еда; `>=250` — крупный приём |
| `is_triggered_today` | BOOLEAN (default False) | Уже сработало за текущие сутки |
| `is_active` | BOOLEAN (default True) | Вкл/выкл пользователем |
| `users` | Link | `{id: telegram_id}` |

## Зависимости (по коду)

- `python-dotenv`, `google-genai`, `httpx`, `pydantic`, `aiogram`, `aiohttp-socks`, `Pillow`, `timezonefinder`, `geopy`
- Список в `requirements.txt`.

## Docker (локально Windows / VPS)

- `Dockerfile`: `python:3.10-slim` (паритет с локальной 3.10.10), `CMD ["python", "main.py"]`, `PYTHONUNBUFFERED=1`.
- `docker-compose.yml`: сервис `bot`, `env_file: .env`, volumes `./logs`, `./.reminder_day_keys.json`, `./.reminder_pending_meal.json` (файлы на хосте должны существовать до первого `up`; `deploy.sh` создаёт пустые `{}`).
- `docker-compose.vps.yml`: `network_mode: host` — контейнер видит mihomo `127.0.0.1:7890` и `/etc/hosts` хоста.
- Локальный тест: `docker compose up --build` (без vps-файла). Прокси в `.env` пустой.
- VPS: `docker compose -f docker-compose.yml -f docker-compose.vps.yml up --build -d`.
- Не гонять параллельно локальный контейнер/процесс и VPS с тем же `TELEGRAM_BOT_API_KEY` (иначе TelegramConflictError).

## Деплой SkyNode (ветка `build` + GitHub Actions)

- Каталог на VPS: `~/NutriSnap_Twin_bot` (ветка `build`).
- Локально: `deploy_to_build.cmd` → commit (если нужно) → merge в `build` → `git push origin build` → обратно на рабочую ветку.
- Actions (`.github/workflows/deploy.yml`): `push` на `build` → SSH (`appleboy/ssh-action`) → `bash deploy.sh`.
- Secrets: `VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY`, `VPS_DEPLOY_PATH` (`/home/gogortey/NutriSnap_Twin_bot`).
- Ключи на VPS: `~/.ssh/nutrisnap_github` (deploy key RO → GitHub), `~/.ssh/nutrisnap_gha` (Actions → VM, в `authorized_keys`).
- Compose plugin без sudo: `~/.docker/cli-plugins/docker-compose`.
- mihomo: `127.0.0.1:7890`; в `.env` на VPS: `OUTBOUND_HTTPS_PROXY=http://127.0.0.1:7890`.
- Telegram + Gemini → прокси; NocoDB / SMTP / Nominatim → напрямую. Не ставить unit-wide `HTTP_PROXY` на процесс бота.

## Планируемое развитие

- Дневной лимит распознаваний.

## Важные договорённости

- Для функций — краткий комментарий-аннотация: зачем, что делает и где используется.
- В `main.py`, `food_recognition.py`, `initial_survey.py`, `db_nocodb.py` сверху — модульный docstring; при изменениях обновлять.
- Новые запросы к NocoDB: сначала `requests_DB_test.py` (2xx), потом код бота. 5 фейлов подряд → стоп, проверить IDE NocoDB.
- Незавершённую логику помечать `🎈`.
- Не опираться на код в `Старое/` при новых изменениях.
- Секреты только в `.env`.
- Тексты пользователю в Telegram: дружелюбный тон без точки в конце сообщения (последнее предложение / весь пузырь не завершать `.`). Точки внутри текста (между предложениями), сокращения (`гр.`, `стр.`), `!`/`?`, а также содержимое выгрузки `.txt` и сообщения консоли/SystemExit — оставлять как есть. Ориентир — стиль `food_recognition.py`.
