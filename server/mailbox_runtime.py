"""Mailbox runtime для модуля 📧 Почта (Loom).

Назначение: позволить модулю «mail» при invoke прочитать последние письма
юзера и использовать их как контекст ответа («суммаризируй вчерашнюю
переписку», «составь ответ на письмо от X», «есть ли необработанные
письма от клиентов?»).

Отличия от server/email_imap.py:
  - Тот модуль создан для IMAP-trigger ботов (нужны новые письма после
    last_uid). Здесь — снэпшот последних N писем для LLM-контекста.
  - Не сохраняет last_uid, не запускает workflow.
  - Имеет verify_mailbox_connection для UI-валидации при подключении.
  - Маскирует PII (email-отправителя/получателя) ПОСЛЕ передачи в LLM
    через PrivacyGuard (это делает уже ai.py wrapper — мы просто
    передаём содержимое как есть, размаскировка тоже автоматическая).

Пресеты host/port для популярных провайдеров: см. PRESETS ниже.
"""
from __future__ import annotations

import asyncio
import imaplib
import logging
from datetime import datetime
from email import message_from_bytes
from email.header import decode_header
from typing import Any

log = logging.getLogger(__name__)


# Пресеты IMAP-серверов для популярных провайдеров.
# Юзер в UI выбирает провайдера → автозаполнение host/port.
PRESETS: dict[str, dict[str, Any]] = {
    "yandex": {
        "host": "imap.yandex.ru", "port": 993, "ssl": True,
        "help_url": "https://id.yandex.ru/security/app-passwords",
        "help_text": "Создай app-password в Яндекс ID → Безопасность → "
                     "Пароли приложений → Почта (IMAP). Обычный пароль "
                     "от аккаунта не подойдёт.",
    },
    "gmail": {
        "host": "imap.gmail.com", "port": 993, "ssl": True,
        "help_url": "https://myaccount.google.com/apppasswords",
        "help_text": "Включи 2FA в Google → myaccount.google.com/apppasswords → "
                     "Создать пароль для приложения «Mail» / «Другое». "
                     "Обычный пароль не подойдёт — Google блокирует.",
    },
    "mailru": {
        "host": "imap.mail.ru", "port": 993, "ssl": True,
        "help_url": "https://account.mail.ru/user/2-step-auth/passwords/",
        "help_text": "Mail.ru → Настройки → Безопасность → Пароли для "
                     "внешних приложений → Создать пароль.",
    },
    "other": {
        "host": "", "port": 993, "ssl": True,
        "help_text": "Укажи IMAP-сервер вручную. Обычно imap.example.com:993 SSL.",
    },
}


def detect_provider(email: str) -> str:
    """Эвристика по домену email."""
    e = (email or "").lower()
    if "@yandex." in e or "@ya.ru" in e:
        return "yandex"
    if "@gmail.com" in e or "@googlemail.com" in e:
        return "gmail"
    if "@mail.ru" in e or "@inbox.ru" in e or "@list.ru" in e or "@bk.ru" in e:
        return "mailru"
    return "other"


def _decode_mime(s: str) -> str:
    if not s:
        return ""
    try:
        parts = decode_header(s)
        result = []
        for text, charset in parts:
            if isinstance(text, bytes):
                result.append(text.decode(charset or "utf-8", errors="ignore"))
            else:
                result.append(text)
        return "".join(result)
    except Exception:
        return s


def _extract_body(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if ctype == "text/plain" and "attachment" not in disp:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="ignore")
        return ""
    payload = msg.get_payload(decode=True)
    if payload:
        charset = msg.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="ignore")
    return ""


def _verify_sync(host: str, port: int, username: str, password: str) -> dict:
    """Попытка login + select INBOX. Возвращает {"ok", "messages_total", "error"}."""
    try:
        from server.email_imap import _imap_ssl_context
        M = imaplib.IMAP4_SSL(host, port, ssl_context=_imap_ssl_context())
        M.login(username, password)
        typ, data = M.select("INBOX", readonly=True)
        if typ != "OK":
            M.logout()
            return {"ok": False, "error": f"SELECT INBOX failed: {data}"}
        # data — байты с количеством писем
        total = int((data[0] or b"0").decode())
        M.logout()
        return {"ok": True, "messages_total": total}
    except imaplib.IMAP4.error as e:
        # Login fail — обычно неверный app-password
        return {"ok": False, "error": f"Ошибка входа в IMAP: {e!s:.200}. "
                                       "Проверь email и app-password "
                                       "(обычный пароль провайдер не даст)."}
    except Exception as e:
        return {"ok": False, "error": f"Ошибка соединения: {e!s:.200}"}


async def verify_mailbox_connection(host: str, port: int,
                                    username: str, password: str) -> dict:
    """Async wrapper для проверки IMAP-credentials.

    Возвращает {"ok": bool, "messages_total": int, "error": str?}.
    Юзер видит детальную ошибку при connect — без неё непонятно,
    что app-password не подошёл / 2FA не включён / hostname опечатан.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _verify_sync, host, port, username, password
    )


def _fetch_recent_sync(host: str, port: int, username: str,
                       password: str, limit: int) -> list[dict]:
    """Синхронно забрать последние N писем (без фильтра по UID).

    Возвращает [{"uid", "from", "to", "subject", "date", "body_preview"}, ...]
    от свежих к старым.
    """
    out: list[dict] = []
    try:
        from server.email_imap import _imap_ssl_context
        M = imaplib.IMAP4_SSL(host, port, ssl_context=_imap_ssl_context())
        M.login(username, password)
        M.select("INBOX", readonly=True)
        typ, data = M.uid("search", None, "ALL")
        if typ != "OK":
            M.logout()
            return out
        uids = data[0].split()
        # Берём последние `limit` (UID растёт)
        uids = uids[-limit:] if len(uids) > limit else uids
        # Идём от свежих к старым → реверс
        for uid in reversed(uids):
            typ, msg_data = M.uid("fetch", uid, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            msg = message_from_bytes(raw)
            body = _extract_body(msg) or ""
            out.append({
                "uid": int(uid),
                "from": _decode_mime(msg.get("From", "")),
                "to": _decode_mime(msg.get("To", "")),
                "subject": _decode_mime(msg.get("Subject", "")),
                "date": msg.get("Date", ""),
                # Обрезка для LLM-контекста: 1000 chars обычно достаточно
                # понять о чём письмо. Полное тело дёрнем отдельным
                # инструментом если модуль запросит.
                "body_preview": body[:1000],
            })
        M.logout()
    except Exception as e:
        log.warning("[mailbox] fetch failed for %s@%s: %s",
                    username.split("@")[0] if "@" in username else "?",
                    host, e)
    return out


async def fetch_mailbox_recent(mailbox, limit: int = 10) -> list[dict]:
    """Async-обёртка: вернуть последние N писем для модуля.

    mailbox — instance UserMailbox (имеет .host, .port, .email, .password
    после расшифровки EncryptedString'ом). Password уже распакован к моменту
    обращения (SQLAlchemy при чтении расшифровывает).
    """
    if not mailbox or not mailbox.is_active:
        return []
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _fetch_recent_sync,
        mailbox.host, mailbox.port, mailbox.email, mailbox.password, limit,
    )


def build_mail_context(emails: list[dict]) -> str:
    """Сформировать текстовый контекст для system-prompt модуля mail.

    Каждое письмо — заголовок + preview. LLM получает компактную сводку
    последних писем, может ответить «есть письма от X с темой Y» / составить
    ответ на конкретное / суммаризировать.
    """
    if not emails:
        return "📭 Inbox пуст (или нет подключённых ящиков)."
    parts = [f"═══ ПОСЛЕДНИЕ {len(emails)} ПИСЕМ ИЗ INBOX ═══"]
    for i, e in enumerate(emails, 1):
        parts.append(
            f"\n— Письмо {i} —\n"
            f"От:    {e.get('from', '?')}\n"
            f"Тема:  {e.get('subject', '(без темы)')}\n"
            f"Дата:  {e.get('date', '?')}\n"
            f"UID:   {e.get('uid', '?')}\n"
            f"Превью:\n{(e.get('body_preview') or '').strip()[:500]}"
        )
    parts.append("\n\nИспользуй эти письма как контекст. Если юзер просит "
                 "ответить на конкретное — упомяни UID в ответе.")
    return "\n".join(parts)
