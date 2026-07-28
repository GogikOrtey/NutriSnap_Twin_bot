"""
error_notify.py — письма разработчику и UX при сбоях внешних сервисов.

Зачем нужен файл
----------------
1) Ошибки → logging + SMTP на FEEDBACK_TO_EMAIL с префиксом 🟨⬛🍎
   (report_console_error + глобальные хуки).
2) Сбои БД / Gemini / сети → то же + префикс 🟧🍎 и текст пользователю
   в чате (report_service_problem / TECH_ISSUES_USER_TEXT).
SMTP без прокси. При сбое отправки письма — только лог, без рекурсии.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import smtplib
import sys
import threading
import time
import traceback
import urllib.error
from datetime import datetime, timezone
from email.mime.text import MIMEText
from typing import Any

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Префиксы темы писем (по ТЗ).
EMAIL_PREFIX_CONSOLE = "🟨⬛🍎"
EMAIL_PREFIX_SERVICE = "🟧🍎"

# Текст в чат при сбое NocoDB / Gemini / сети (без точки в конце — стиль бота).
TECH_ISSUES_USER_TEXT = (
    "У нас небольшие технические неполадки. "
    "Попробуйте повторить действие немного позже"
)

ERROR_TO_EMAIL = os.getenv("FEEDBACK_TO_EMAIL", "gog.ortey@yandex.ru")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.yandex.ru")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "gog.ortey@yandex.ru")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")

# Одинаковый текст ошибки — не чаще раза за TTL (антиспам при ретраях).
_DEDUP_TTL_SEC = 300
_dedup_lock = threading.Lock()
_dedup_sent: dict[str, float] = {}
_mail_lock = threading.Lock()


# Хэш текста для антиспама писем.
# Используется _should_email.
def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:32]


# True, если это сообщение ещё не слали за последние _DEDUP_TTL_SEC.
# Используется _emit_error_email.
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
        logger.warning("SMTP не настроен — письмо об ошибке не отправлено")
        return
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = SMTP_USER
    msg["To"] = ERROR_TO_EMAIL
    msg["Subject"] = subject
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, [ERROR_TO_EMAIL], msg.as_string())


# Ставит отправку письма в daemon-поток (не блокирует хендлеры бота).
# Используется _emit_error_email.
def _send_email_async(subject: str, body: str) -> None:
    def _worker() -> None:
        try:
            with _mail_lock:
                _smtp_send_error(subject, body)
        except Exception as e:
            # Без report_* — иначе цикл при мёртвом SMTP.
            logger.error("error_notify SMTP failed: %s", e)

    threading.Thread(target=_worker, name="error-email", daemon=True).start()


# Собирает текст ошибки, пишет в лог и шлёт письмо с заданным префиксом.
# Используется report_console_error / report_service_problem.
def _emit_error_email(
    message: str,
    *,
    exc: BaseException | None,
    subject_prefix: str,
    subject_label: str,
) -> str:
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
    logger.error("%s %s", subject_prefix, full)

    if not _should_email(f"{subject_prefix}\n{full}"):
        return full

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    subject = f"{subject_prefix} [NutriClick] {subject_label}"
    body = f"Время: {ts}\n\n{full}\n"
    _send_email_async(subject, body)
    return full


# True, если исключение — сбой внешнего сервиса/сети (не логика бота).
# Используется хендлерами и @dp.error для выбора UX 🟧🍎 vs 🟨⬛🍎.
def is_external_service_error(exc: BaseException | None) -> bool:
    if exc is None:
        return False

    if isinstance(
        exc,
        (
            TimeoutError,
            ConnectionError,
            BrokenPipeError,
            ConnectionResetError,
            urllib.error.URLError,
            urllib.error.HTTPError,
        ),
    ):
        return True

    # Сетевые OSError (не FileNotFoundError / PermissionError и т.п.).
    if isinstance(exc, OSError) and not isinstance(
        exc,
        (
            FileNotFoundError,
            NotADirectoryError,
            IsADirectoryError,
            PermissionError,
            FileExistsError,
        ),
    ):
        errno = getattr(exc, "errno", None)
        if errno in {110, 111, 113, 101, 104, 10054, 10060, 10061}:
            return True

    try:
        from db_nocodb import NocoDBError

        if isinstance(exc, NocoDBError):
            return True
    except ImportError:
        pass

    try:
        from google.genai.errors import APIError

        if isinstance(exc, APIError):
            return True
    except ImportError:
        pass

    try:
        import httpx

        if isinstance(exc, httpx.HTTPError):
            return True
    except ImportError:
        pass

    try:
        from aiohttp import ClientError

        if isinstance(exc, ClientError):
            return True
    except ImportError:
        pass

    try:
        from aiogram.exceptions import TelegramNetworkError

        if isinstance(exc, TelegramNetworkError):
            return True
    except ImportError:
        pass

    cause = exc.__cause__ or exc.__context__
    if isinstance(cause, BaseException) and cause is not exc:
        return is_external_service_error(cause)
    return False


# Пишет ошибку в лог и шлёт письмо 🟨⬛🍎 (любая консольная ошибка).
# Используется except-блоками и глобальными хуками исключений.
def report_console_error(
    message: str,
    *,
    exc: BaseException | None = None,
) -> None:
    _emit_error_email(
        message,
        exc=exc,
        subject_prefix=EMAIL_PREFIX_CONSOLE,
        subject_label="ошибка в консоли",
    )


# Пишет сбой внешнего сервиса в лог и шлёт письмо 🟧🍎 (БД / Gemini / сеть).
# Используется вместе с TECH_ISSUES_USER_TEXT в чате пользователя.
def report_service_problem(
    message: str,
    *,
    exc: BaseException | None = None,
) -> None:
    _emit_error_email(
        message,
        exc=exc,
        subject_prefix=EMAIL_PREFIX_SERVICE,
        subject_label="проблема с внешним сервисом",
    )


# Выбирает 🟧🍎 или 🟨⬛🍎 по типу исключения и шлёт письмо (+ лог).
# Используется единым except в хендлерах, когда нужна авто-классификация.
def report_error_auto(
    message: str,
    *,
    exc: BaseException | None = None,
) -> bool:
    """Возвращает True, если это сбой внешнего сервиса (нужен TECH_ISSUES текст)."""
    if is_external_service_error(exc):
        report_service_problem(message, exc=exc)
        return True
    report_console_error(message, exc=exc)
    return False


# Пишет TECH_ISSUES_USER_TEXT в чат (message / callback / bot+chat_id).
# Используется @dp.error и колбэками сохранения профиля/еды.
async def notify_user_tech_issues(
    *,
    message: Any | None = None,
    bot: Any | None = None,
    chat_id: int | None = None,
) -> None:
    text = TECH_ISSUES_USER_TEXT
    try:
        if message is not None and hasattr(message, "answer"):
            await message.answer(text)
            return
        if bot is not None and chat_id is not None:
            await bot.send_message(chat_id, text)
    except Exception as e:
        logger.error("notify_user_tech_issues failed: %s", e)


# Из ErrorEvent aiogram достаёт chat и шлёт TECH_ISSUES_USER_TEXT.
# Используется on_unhandled_update_error при внешнем сбое.
async def notify_user_tech_issues_from_event(event: Any) -> None:
    update = getattr(event, "update", None)
    if update is None:
        return
    try:
        if getattr(update, "message", None) is not None:
            await update.message.answer(TECH_ISSUES_USER_TEXT)
            return
        cq = getattr(update, "callback_query", None)
        if cq is not None and cq.message is not None:
            try:
                await cq.answer()
            except Exception:
                pass
            await cq.message.answer(TECH_ISSUES_USER_TEXT)
    except Exception as e:
        logger.error("notify_user_tech_issues_from_event failed: %s", e)


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
    if exc is not None and is_external_service_error(exc):
        report_service_problem(f"Unhandled exception:\n{text}", exc=exc)
    else:
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
    exc = args.exc_value
    msg = f"Unhandled thread exception ({thread_name}):\n{text}"
    if isinstance(exc, BaseException) and is_external_service_error(exc):
        report_service_problem(msg, exc=exc)
    else:
        report_console_error(msg, exc=exc if isinstance(exc, BaseException) else None)


# asyncio exception handler: «forgotten» task / callback errors → почта.
# Используется install_error_email_hooks / attach_asyncio_error_handler.
def _asyncio_exception_handler(
    loop: asyncio.AbstractEventLoop,
    context: dict[str, Any],
) -> None:
    message = str(context.get("message") or "Unhandled asyncio exception")
    exc = context.get("exception")
    if isinstance(exc, BaseException):
        if is_external_service_error(exc):
            report_service_problem(f"asyncio: {message}", exc=exc)
        else:
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
