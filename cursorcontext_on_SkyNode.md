# SkyNode_Agent_worker

Инструкция для настройки и сопровождения VPS **SkyNode** (виртуалка + БД) через Cursor-агента: SSH-поручения, конфиги, сервисы.

## VPS: SkyNode

| Параметр | Значение |
|----------|----------|
| Имя | SkyNode |
| Хост | `139.100.204.113` |
| Пользователь | `gogortey` |
| Подключение | `ssh gogortey@139.100.204.113` |
| Auth | SSH-ключ в стандартном месте (`~/.ssh`) |
| Часовой пояс (проверено) | `+05` |

## Работа агента с сервером

- Команды на VPS — через `ssh gogortey@139.100.204.113 '…'`.
- В одном чате shell-сессии агента сохраняют `cwd`/env между запросами; это не замена persistent remote-shell.
- Для устойчивого состояния на сервере предпочитать `tmux`/`screen` или SSH `ControlPersist`.
- Интерактивный терминал пользователя в IDE агент видит (вывод), но не управляет им напрямую.

## Заметки

- Репозиторий пока минимальный: фокус на удалённой настройке SkyNode, не на локальном приложении.
- Сеть: Google / Gemini / OpenAI — HTTPS ок. DNS `api.telegram.org` на «родные» IP (`149.154.166.110`, `149.154.167.99`) — timeout; рабочий DC IP `149.154.167.220:443`.
- Обход Telegram (сделано на VPS): в `/etc/hosts` — `149.154.167.220 api.telegram.org`. После этого `getMe` и aiogram работают (проверено 2026-07-28, бот NutriClick / `@nutrisnap_ultra_bot`). IPv6 до Telegram с VPS не ходит — клиенты должны брать IPv4 из hosts.
- Исходящие сервисы бота (проверено 2026-07-28 с VPS): Telegram 443 ✅ (через hosts); Gemini HTTPS до gateway ✅, но **напрямую с ключом → 400 geo**; NocoDB ✅; Yandex SMTP 465 ✅; Nominatim ✅.
- **Outbound proxy (вариант A, 2026-07-28):** на VPS `~/proxy` — mihomo **без TUN**, только `127.0.0.1:7890` + API `127.0.0.1:9090`. Unit: `systemd --user` `mihomo.service` (`Restart=always`, enabled). **Linger=yes** — поднимается после ребута без SSH. Старт: `systemctl --user restart mihomo`. Узел Latvia → `LV`. Прокси для Telegram+Gemini; NocoDB/SMTP/Nominatim — напрямую. Бриф: `HANDOFF_BOT_VPS.md`.
- На VPS: `python3.12-venv` / pip. Тестовый стенд: `~/tg-aiogram-test`. У `gogortey` sudo без пароля недоступен.
- **NutriSnap бот (прод, 2026-07-29):** `~/NutriSnap_Twin_bot`, ветка `build`, контейнер `nutrisnap-bot` (`network_mode: host`). Деплой: push в `build` → GitHub Actions → `deploy.sh`. Compose plugin: `~/.docker/cli-plugins/docker-compose`. Deploy key: `~/.ssh/nutrisnap_github`; Actions SSH: `~/.ssh/nutrisnap_gha`. `.env` на хосте (не в git), `OUTBOUND_HTTPS_PROXY=http://127.0.0.1:7890`. Не запускать второй экземпляр с тем же токеном локально.

## Правила

- Тут я не даю тебе полный sudo доступ, но если тебе понадобиться выполнить команду от sudo - например для установки библиотеки, или для чего-то ещё - то можешь смело останаливаться, и просить меня выполнить эту команду, написав что она делает и для чего нужна. Я её выполню, и ты продолжишь выполнение задачи