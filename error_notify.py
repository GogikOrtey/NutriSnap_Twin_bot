"""
error_notify.py — письма разработчику о консольных ошибках бота.

Зачем нужен файл
----------------
Дублирует сообщения об ошибках из консоли на FEEDBACK_TO_EMAIL (SMTP из .env)
с префиксом 🟨⬛🍎. Используется из main / food_recognition / initial_survey
и глобальных хуков необработанных исключений (install_error_email_hooks).
SMTP без прокси (как обратная связь). При сбое отправки письма — только
консоль, без рекурсии.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import smtplib
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from email.mime.text import MIMEText
from typing import Any

from dotenv import load_dotenv

load_dotenv()

ERROR_TO_EMAIL = os.getenv("FEEDBACK_TO_EMAIL", "gog.ortey@yandex.ru")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.yandex.ru")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "gog.ortey@yandex.ru")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")

# Одинаковый текст ошибки — не чаще раза за TTL (антиспам при ретраях Gemini и т.п.).
_DEDUP_TTL_SEC = 300
_dedup_lock = threading.Lock()
_dedup_sent: dict[str, float] = {}
_mail_lock = threading.Lock()


# Хэш текста для антиспама писем.
# Используется _should_email.
def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:32]


# True, если это сообщение ещё не слали за последние _DEDUP_TTL_SEC.
# Используется report_console_error.
def _should_email(text: str) -> bool:
    key = _fingerprint(text)
    now = time.monotonic()
    with _dedup_lock:
        expired = [k for k, ts in _dedup_sent.items() if now - ts > _DEDUP_TTL_SEC]
        for k in expired:
            del _dedup_sent[k]
        last = _dedup_sent.get(key)
        if last is not None and now - last < _DEDUP_TTL_SEC:
            return False
        _dedup_sent[key] = now
        return True


# Синхронная отправка письма об ошибке через SMTP_SSL.
# Используется фоновым потоком из _send_email_async.
def _smtp_send_error(subject: str, body: str) -> None:
    if not SMTP_USER or not SMTP_PASSWORD:
        print(
            "🟧 SMTP не настроен — письмо об ошибке не отправлено",
            flush=True,
        )
        return
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = SMTP_USER
    msg["To"] = ERROR_TO_EMAIL
    msg["Subject"] = subject
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, [ERROR_TO_EMAIL], msg.as_string())


# Ставит отправку письма в daemon-поток (не блокирует хендлеры бота).
# Используется report_console_error.
def _send_email_async(subject: str, body: str) -> None:
    def _worker() -> None:
        try:
            with _mail_lock:
                _smtp_send_error(subject, body)
        except Exception as e:
            # Без report_console_error — иначе цикл при мёртвом SMTP.
            print(f"🟧 error_notify SMTP failed: {e}", flush=True)

    threading.Thread(target=_worker, name="error-email", daemon=True).start()


# Печатает ошибку в консоль и (с антиспамом) шлёт копию на почту разработчика.
# Используется except-блоками бота и глобальными хуками исключений.
def report_console_error(
    message: str,
    *,
    exc: BaseException | None = None,
) -> None:
    parts: list[str] = []
    if message and message.strip():
        parts.append(message.strip())
    if exc is not None:
        parts.append(
            "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            ).rstrip()
        )
    full = "\n\n".join(parts) if parts else "(empty error)"
    print(full, flush=True)

    if not _should_email(full):
        return

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    subject = f"🟨⬛🍎 [NutriClick] ошибка в консоли"
    body = f"Время: {ts}\n\n{full}\n"
    _send_email_async(subject, body)


# sys.excepthook: необработанные исключения главного потока → почта.
# Используется install_error_email_hooks.
def _excepthook(
    exc_type: type[BaseException],
    exc: BaseException | None,
    tb: Any,
) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    text = "".join(traceback.format_exception(exc_type, exc, tb)).rstrip()
    report_console_error(f"Unhandled exception:\n{text}")


# threading.excepthook: необработанные исключения в потоках → почта.
# Используется install_error_email_hooks.
def _threading_excepthook(args: threading.ExceptHookArgs) -> None:
    if args.exc_type is not None and issubclass(args.exc_type, KeyboardInterrupt):
        return
    text = "".join(
        traceback.format_exception(
            args.exc_type, args.exc_value, args.exc_traceback
        )
    ).rstrip()
    thread_name = getattr(args.thread, "name", "?")
    report_console_error(f"Unhandled thread exception ({thread_name}):\n{text}")


# asyncio exception handler: «forgotten» task / callback errors → почта.
# Используется install_error_email_hooks / attach_asyncio_error_handler.
def _asyncio_exception_handler(
    loop: asyncio.AbstractEventLoop,
    context: dict[str, Any],
) -> None:
    message = str(context.get("message") or "Unhandled asyncio exception")
    exc = context.get("exception")
    if isinstance(exc, BaseException):
        report_console_error(f"asyncio: {message}", exc=exc)
    else:
        report_console_error(f"asyncio: {message}\n{context!r}")


# Вешает sys/threading хуки; asyncio — если loop уже running.
# Используется в main() при старте (дополняется attach_asyncio_error_handler).
def install_error_email_hooks() -> None:
    sys.excepthook = _excepthook
    threading.excepthook = _threading_excepthook
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.set_exception_handler(_asyncio_exception_handler)


# Ставит asyncio exception handler на конкретный event loop.
# Используется в async main() сразу после входа в корутину.
def attach_asyncio_error_handler(loop: asyncio.AbstractEventLoop) -> None:
    loop.set_exception_handler(_asyncio_exception_handler)
