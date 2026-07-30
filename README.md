# NutriClick Twin Bot

Telegram-бот для учёта калорий по фото и тексту

Пользователь присылает фото блюда (опционально с подписью) или описание текстом → Gemini возвращает структурированный разбор → бот показывает превью с подтверждением → запись попадает в дневник (NocoDB). Есть первичная анкета, дневник, настройки, напоминания и выгрузка.

Бот в Telegram: [@nutrisnap_ultra_bot](https://t.me/nutrisnap_ultra_bot)

## Возможности

- Распознавание еды по **фото** и **тексту** (Google Gemini)
- Превью КБЖУ с кнопками и автоподтверждением
- **Дневник**: просмотр, правка через Gemini, удаление
- **Первичный опрос**: пол, возраст, рост/вес, цель, часовой пояс, норма ккал (Mifflin–St Jeor)
- **Напоминания** (витамины / приём пищи) с окном времени
- Напоминание «не забыл поесть» до 13:00 (по TZ пользователя)
- Выгрузка дневника, FAQ, обратная связь по email
- Фоновая очистка старых записей дневника (retention 100 дней)

## Стек

| Слой | Технология |
|------|------------|
| Бот | Python 3.10+, [aiogram](https://docs.aiogram.dev/) 3.x, long polling |
| ИИ | Google Gemini (`google-genai`) |
| БД | [NocoDB](https://nocodb.com/) Data API v3 (SkyNode VPS) |
| Прочее | Pydantic, Pillow, timezonefinder, geopy, SMTP (отзывы и алерты) |

## Структура проекта

```
NutriSnap_Twin_bot/
├── main.py                 # Точка входа: /start, меню, обёртки БД, фоновые циклы
├── food_recognition.py     # FSM: распознавание еды (Gemini + confirm UI)
├── initial_survey.py       # Первичный опрос профиля
├── db_nocodb.py            # Клиент NocoDB (users / food_logs / reminders)
├── proxy_config.py         # HTTPS-прокси только для Telegram + Gemini
├── error_notify.py         # Ошибки консоли / внешних сервисов → SMTP
├── requests_DB_test.py     # Песочница CRUD NocoDB
├── requirements.txt
├── .env.example            # Шаблон переменных окружения
└── nutriclip_DB_swagger_documentation.json  # OpenAPI NocoDB
```

Папка `Старое/` — устаревшие эксперименты, не использовать как основу.

## Быстрый старт

### 1. Клонировать и зависимости

```bash
git clone <repo-url>
cd NutriSnap_Twin_bot
python -m venv .venv

# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Переменные окружения

Скопируйте `.env.example` → `.env` и заполните:

| Переменная | Назначение |
|---|---|
| `GEMINI_API_KEY` | Ключ [Google AI Studio](https://aistudio.google.com/api-keys) |
| `TELEGRAM_BOT_API_KEY` | Токен бота от [@BotFather](https://t.me/BotFather) |
| `NOCODB_SKYNODE_API_KEY` | API-токен NocoDB (`xc-token`) |
| `OUTBOUND_HTTPS_PROXY` | Прокси для TG + Gemini на VPS (локально можно пустым) |
| `FEEDBACK_TO_EMAIL` | Куда слать отзывы и письма об ошибках |
| `SMTP_*` | SMTP для отзывов и алертов (по умолчанию Yandex) |

Опционально: `TELEGRAM_PROXY`, `GEMINI_HTTPS_PROXY` — отдельные алиасы прокси.

**Важно:** не задавайте глобальные `HTTP_PROXY` / `HTTPS_PROXY` на процесс бота — иначе NocoDB, SMTP и Nominatim уйдут в VPN. Прокси настраивается точечно через `OUTBOUND_HTTPS_PROXY`.

### 3. Запуск

```bash
python main.py
```

## Как устроен поток пользователя

```
/start
  → нет профиля в NocoDB? → первичный опрос → меню распознавания
  → есть профиль? → главный экран «Сегодня»

Фото / текст
  → Gemini (JSON со status)
  → ветки: unclear / no_food / label / recognized
  → превью + кнопки / автоподтверждение
  → ✅ → INSERT food_logs (+ триггер напоминаний)
```

## База данных (NocoDB)

Доступ **только через API** (не прямой SQL).

- Base: `https://skynode.nocodb.api.gogortey.ru`
- Таблицы: `users`, `food_logs`, `reminders`
- Связь владельца — Link `users: {id: telegram_id}`
- Полная схема: `nutriclip_DB_swagger_documentation.json`

Проверка API вручную:

```bash
python requests_DB_test.py
```

Новый запрос к NocoDB сначала гоняют в песочнице (2xx), потом встраивают в бота.

## Деплой на VPS

1. Поднять mihomo на `127.0.0.1:7890` (без TUN).
2. В `.env`: `OUTBOUND_HTTPS_PROXY=http://127.0.0.1:7890`
3. Telegram и Gemini идут через прокси; NocoDB / SMTP / Nominatim — напрямую.
4. `python main.py` (или systemd / screen / pm2 по вашему процессу)

## Фоновые задачи

При старте `main()` поднимаются циклы:

| Цикл | Интервал | Что делает |
|------|----------|------------|
| Usage reminder | ~1 ч | Напоминание, если до 13:00 нет еды за день |
| Reminders maintenance | каждый час в :05 | Сброс `is_triggered_today`, пропущенные окна |
| Food logs cleanup | ~1 сут | Удаление записей старше 100 дней |

## Разработка

- Для функций — краткий комментарий: зачем, что делает, где используется.
- В `main.py`, `food_recognition.py`, `initial_survey.py`, `db_nocodb.py` — модульный docstring сверху.
- Незавершённую логику помечать `🎈`.
- Секреты только в `.env` (не коммитить).
- Подробный внутренний контекст для агентов: [`cursorcontext.md`](cursorcontext.md)

## Лицензия

Проект частный / внутренний. Уточняйте условия использования у автора.
