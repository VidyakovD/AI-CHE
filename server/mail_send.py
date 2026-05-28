"""Отправка писем через SMTP (модуль mail).

Без зависимостей aiosmtplib — используем std smtplib + EmailMessage + ssl.
Достаточно для редких отправок (юзер подтверждает каждое письмо вручную).

Автоматический подбор SMTP-сервера из IMAP-хоста: yandex/gmail/mail.ru
используют одинаковую тройку (user+app-password+ssl) для IMAP и SMTP.

Безопасность:
  - host берётся из UserMailbox.smtp_host или auto-derive (защита от
    подмены через прокинутые из LLM параметры).
  - to валидируется по простому email-regex.
  - PrivacyGuard — НЕ применяется тут (PII в самом письме это нормально,
    юзер видел preview и подтвердил).
"""
from __future__ import annotations

import logging
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr
from typing import Optional

log = logging.getLogger(__name__)


_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


# Карта IMAP host → SMTP host. Для частых провайдеров известно заранее.
# Порт 465 — implicit SSL (старый, рекомендуется); 587 — STARTTLS.
# Yandex/Gmail/Mail.ru поддерживают оба, мы используем 465 как простой SSL.
_PROVIDER_MAP = {
    "imap.yandex.ru":      ("smtp.yandex.ru", 465),
    "imap.yandex.com":     ("smtp.yandex.com", 465),
    "imap.gmail.com":      ("smtp.gmail.com", 465),
    "imap.mail.ru":        ("smtp.mail.ru", 465),
    "imap.mail.yahoo.com": ("smtp.mail.yahoo.com", 465),
}


def derive_smtp(imap_host: str, smtp_host: Optional[str] = None,
                smtp_port: Optional[int] = None) -> tuple[str, int]:
    """Если smtp_host задан в UserMailbox — берём его. Иначе пробуем
    auto-derive по IMAP-хосту. Если ничего не нашли — заменяем 'imap.' на
    'smtp.' и пробуем 465 (это работает для большинства провайдеров).
    """
    if smtp_host:
        return smtp_host, int(smtp_port or 465)
    h = (imap_host or "").lower().strip()
    if h in _PROVIDER_MAP:
        return _PROVIDER_MAP[h]
    if h.startswith("imap."):
        return ("smtp." + h[len("imap."):], 465)
    # Fallback — пусть caller вернёт ошибку «не могу определить SMTP» если хост странный
    return (h, 465)


def is_valid_email(s: str) -> bool:
    return bool(isinstance(s, str) and _EMAIL_RE.match(s.strip()))


def send_via_smtp(
    *, smtp_host: str, smtp_port: int,
    smtp_user: str, smtp_password: str,
    from_addr: str, from_name: Optional[str],
    to: str, subject: str, body: str,
    reply_to: Optional[str] = None,
    timeout: int = 20,
) -> dict:
    """Реально отправить письмо. Возвращает {ok, message_id|None, error|None}.

    Безопасные дефолты:
      - SSL подключение (порт 465) — для 587/STARTTLS вызывающий код должен
        переключиться сам (мы предполагаем 465 для всех).
      - Plain-text тело (без HTML) — достаточно для модуля mail,
        упрощает защиту от XSS-через-почтовый-клиент.
      - Кодировка UTF-8.
    """
    if not is_valid_email(to):
        return {"ok": False, "message_id": None,
                "error": f"Невалидный адрес: {to!r}"}
    if not (subject or "").strip():
        return {"ok": False, "message_id": None,
                "error": "Пустая тема письма"}
    if not (body or "").strip():
        return {"ok": False, "message_id": None,
                "error": "Пустое тело письма"}

    msg = EmailMessage()
    msg["Subject"] = subject[:998]   # RFC 2822 limit на заголовок
    msg["From"] = formataddr((from_name, from_addr)) if from_name else from_addr
    msg["To"] = to
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body, subtype="plain", charset="utf-8")

    try:
        context = ssl.create_default_context()
        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_host, smtp_port,
                                  context=context, timeout=timeout) as s:
                s.login(smtp_user, smtp_password)
                s.send_message(msg)
        else:
            # 587 — STARTTLS upgrade
            with smtplib.SMTP(smtp_host, smtp_port, timeout=timeout) as s:
                s.ehlo()
                s.starttls(context=context)
                s.ehlo()
                s.login(smtp_user, smtp_password)
                s.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        return {"ok": False, "message_id": None,
                "error": f"SMTP auth failed: {e.smtp_code} {e.smtp_error!s:.120}"}
    except smtplib.SMTPRecipientsRefused as e:
        return {"ok": False, "message_id": None,
                "error": f"Получатель отказан: {e.recipients}"}
    except smtplib.SMTPException as e:
        return {"ok": False, "message_id": None,
                "error": f"SMTP error: {type(e).__name__}: {e!s:.200}"}
    except (OSError, ssl.SSLError) as e:
        return {"ok": False, "message_id": None,
                "error": f"Network error: {type(e).__name__}: {e!s:.200}"}
    except Exception as e:
        log.exception("[mail_send] unexpected error")
        return {"ok": False, "message_id": None,
                "error": f"Unexpected: {type(e).__name__}: {e!s:.200}"}

    return {"ok": True, "message_id": msg.get("Message-Id"), "error": None}
