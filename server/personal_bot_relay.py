"""Personal TG/MAX-боты юзеров (заменяет общий tg_management/max_management).

Архитектура: каждый юзер сам создаёт бот в @BotFather (TG) или у соответствующего
сервиса MAX → копирует токен в /agents-modular.html → платформа:
  1. Валидирует токен через getMe API
  2. Сохраняет в users.personal_{tg|max}_bot_token (EncryptedString)
  3. Считает sha256(JWT_SECRET + token)[:24] → personal_{tg|max}_bot_token_hash
  4. Устанавливает webhook на наш /webhook/personal-{tg|max}/<hash>
  5. При получении update'а — находит юзера по hash → process_message от его имени

Преимущества над общим ботом:
  - White-label: каждый юзер видит свой бренд бота
  - Нет SPoF / rate-limit на общий @aiche_bot
  - Админу платформы не нужно ничего создавать в BotFather

Token security:
  - Хранится в БД через EncryptedString (шифруется HKDF от JWT_SECRET)
  - В логах не светится (только token[-8:])
  - Hash для routing — derived, не сам токен, не обратимо к токену
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
from typing import Any

import httpx

log = logging.getLogger("personal-bot")


TG_API_BASE = "https://api.telegram.org/bot"
MAX_API_BASE = "https://botapi.max.ru"


def _tg_proxy() -> str | None:
    """Прокси для api.telegram.org если задан. На РФ-проде сеть к TG нестабильна,
    через Xray (AI_HTTPS_PROXY) идёт надёжнее. Если переменная не задана —
    идём напрямую."""
    return (os.getenv("TG_HTTPS_PROXY") or os.getenv("AI_HTTPS_PROXY") or "").strip() or None


async def _tg_request(method: str, url: str, *, data=None, json_body=None,
                      timeout: float = 30.0, retries: int = 3) -> tuple[int, dict | None, str | None]:
    """HTTP-запрос к TG API с retry + опциональным прокси.

    Returns: (status_code, json_or_none, error_or_none).
    Делает retries попытки с экспоненциальной паузой при ConnectError / Timeout.
    """
    import asyncio
    proxy = _tg_proxy()
    last_err: str | None = None
    for attempt in range(retries):
        try:
            kwargs: dict = {"timeout": timeout}
            if proxy:
                kwargs["proxy"] = proxy
            async with httpx.AsyncClient(**kwargs) as client:
                if method.upper() == "GET":
                    r = await client.get(url)
                else:
                    r = await client.post(url, data=data, json=json_body)
            try:
                body = r.json() if r.content else None
            except Exception:
                body = None
            return r.status_code, body, None
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
            last_err = f"{type(e).__name__}"
            log.warning(f"[tg] {method} {url[:60]}… attempt {attempt+1}/{retries}: {last_err}")
            if attempt < retries - 1:
                await asyncio.sleep(1.5 ** attempt)  # 1s, 1.5s, 2.25s
                continue
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:80]}"
            break
    return 0, None, last_err or "unknown"


# ── Token hashing for webhook routing ────────────────────────────────────────


def compute_token_hash(bot_token: str) -> str:
    """SHA-256 от JWT_SECRET + bot_token → первые 24 hex-символа.

    Используется как path-параметр в webhook URL. Атакер с этим hash НЕ может
    восстановить bot_token (нужен JWT_SECRET), но платформа может найти юзера
    в БД по hash без расшифровки EncryptedString.
    """
    if not bot_token:
        return ""
    secret = os.getenv("JWT_SECRET", "").encode("utf-8")
    digest = hmac.new(secret, bot_token.encode("utf-8"), hashlib.sha256).hexdigest()
    return digest[:24]


# ── Validation: проверить что токен живой через getMe ────────────────────────


async def tg_validate_token(token: str) -> dict:
    """Дёрнуть https://api.telegram.org/bot<token>/getMe.

    Returns:
        {"ok": True,  "bot_username": "your_bot", "bot_first_name": "..."}
        {"ok": False, "error": "human-readable"}
    """
    if not token or not re.match(r"^[0-9]+:[A-Za-z0-9_\-]+$", token.strip()):
        return {"ok": False, "error": "Невалидный формат токена. Должен быть вида '123456:ABC-DEF...'"}
    url = f"{TG_API_BASE}{token.strip()}/getMe"
    status, data, err = await _tg_request("GET", url, timeout=15, retries=3)
    if err:
        return {"ok": False, "error": f"Сеть: {err}"}
    if status == 401:
        return {"ok": False, "error": "Telegram отклонил токен (401). Проверь правильность копирования."}
    if status != 200 or not data:
        return {"ok": False, "error": f"Telegram вернул {status}"}
    if not data.get("ok"):
        return {"ok": False, "error": data.get("description", "unknown")}
    result = data.get("result") or {}
    return {
        "ok": True,
        "bot_username": (result.get("username") or "").strip(),
        "bot_first_name": (result.get("first_name") or "").strip(),
        "is_bot": bool(result.get("is_bot")),
    }


async def max_validate_token(token: str) -> dict:
    """Дёрнуть MAX getMe (или эквивалент). MAX API: GET /me с Authorization."""
    if not token or len(token.strip()) < 10:
        return {"ok": False, "error": "Невалидный формат токена"}
    headers = {"Authorization": token.strip()}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{MAX_API_BASE}/me", headers=headers)
    except Exception as e:
        return {"ok": False, "error": f"Сеть: {type(e).__name__}"}
    if r.status_code != 200:
        return {"ok": False, "error": f"MAX вернул {r.status_code}: {r.text[:100]}"}
    try:
        data = r.json() or {}
    except Exception:
        return {"ok": False, "error": "MAX вернул не-JSON"}
    return {
        "ok": True,
        "bot_username": data.get("username") or data.get("name") or "",
        "bot_first_name": data.get("name") or "",
    }


# ── Webhook setup ────────────────────────────────────────────────────────────


def _app_url() -> str:
    return os.getenv("APP_URL", "https://aiche.ru").rstrip("/")


async def tg_set_webhook(token: str, token_hash: str) -> dict:
    """Установить webhook https://aiche.ru/webhook/personal-tg/<token_hash>."""
    if not token or not token_hash:
        return {"ok": False, "error": "no token"}
    webhook_url = f"{_app_url()}/webhook/personal-tg/{token_hash}"
    url = f"{TG_API_BASE}{token.strip()}/setWebhook"
    status, data, err = await _tg_request("POST", url, data={
        "url": webhook_url,
        "allowed_updates": '["message","callback_query"]',
    }, timeout=30, retries=3)
    if err:
        return {"ok": False, "error": f"Сеть: {err}"}
    data = data or {}
    if status != 200 or not data.get("ok"):
        return {"ok": False, "error": data.get("description", f"HTTP {status}")}
    return {"ok": True, "webhook_url": webhook_url}


async def tg_delete_webhook(token: str) -> dict:
    """Снять webhook у TG-бота (вызывается при disconnect юзером)."""
    if not token:
        return {"ok": False, "error": "no token"}
    url = f"{TG_API_BASE}{token.strip()}/deleteWebhook"
    status, _data, err = await _tg_request("POST", url, timeout=15, retries=2)
    if err:
        return {"ok": False, "error": err}
    return {"ok": status == 200}


async def max_set_webhook(token: str, token_hash: str) -> dict:
    """Установить webhook на /webhook/personal-max/<token_hash>."""
    if not token or not token_hash:
        return {"ok": False, "error": "no token"}
    webhook_url = f"{_app_url()}/webhook/personal-max/{token_hash}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{MAX_API_BASE}/subscriptions",
                headers={"Authorization": token.strip(), "Content-Type": "application/json"},
                json={"url": webhook_url, "update_types": ["message_created"]},
            )
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": r.status_code == 200, "webhook_url": webhook_url}


async def max_delete_webhook(token: str) -> dict:
    if not token:
        return {"ok": False, "error": "no token"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.delete(
                f"{MAX_API_BASE}/subscriptions",
                headers={"Authorization": token.strip()},
            )
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": r.status_code == 200}


# ── Send message via user's token ────────────────────────────────────────────


async def tg_send_message(token: str, chat_id: str, text: str,
                          parse_mode: str = "HTML") -> bool:
    """Отправить сообщение через бот юзера. Возвращает True если успех."""
    if not token or not chat_id:
        return False
    url = f"{TG_API_BASE}{token.strip()}/sendMessage"
    payload = {
        "chat_id": str(chat_id),
        "text": text[:4000],
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    status, _data, err = await _tg_request("POST", url, json_body=payload,
                                            timeout=20, retries=2)
    if err:
        log.warning(f"[personal-tg] send exception: {err}")
        return False
    if status != 200:
        log.warning(f"[personal-tg] send failed: {status}")
        return False
    return True


async def max_send_message(token: str, user_id: str, text: str) -> bool:
    """Отправить через MAX бот юзера. format=markdown (после mgmt-relay-fix)."""
    if not token or not user_id:
        return False
    url = f"{MAX_API_BASE}/messages"
    headers = {"Authorization": token.strip(), "Content-Type": "application/json"}
    params = {"user_id": str(user_id)}
    body = {"text": text[:4000], "format": "markdown"}
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.post(url, headers=headers, params=params, json=body)
            if r.status_code != 200:
                log.warning(f"[personal-max] send failed: {r.status_code}")
                return False
        return True
    except Exception as e:
        log.warning(f"[personal-max] send exception: {type(e).__name__}")
        return False


# ── Find user by webhook token hash ──────────────────────────────────────────


def find_user_by_tg_token_hash(db, token_hash: str):
    """O(1) поиск юзера по hash. Hash хранится индексом в personal_tg_bot_token_hash."""
    from server.models import User
    if not token_hash:
        return None
    return db.query(User).filter_by(personal_tg_bot_token_hash=token_hash).first()


def find_user_by_max_token_hash(db, token_hash: str):
    from server.models import User
    if not token_hash:
        return None
    return db.query(User).filter_by(personal_max_bot_token_hash=token_hash).first()


# ── handle TG update from personal bot ───────────────────────────────────────


async def handle_personal_tg_update(update: dict, user_id: int) -> None:
    """Обработать update от personal TG-бота юзера.

    Логика проще чем у management-бота: нет /link (юзер уже привязан через
    свой токен), нет команд управления. Любое сообщение → relay к Че.
    Команды:
      /start — установить personal_tg_chat_id (для будущих ответов) + приветствие
      /help  — справка
    Остальное (text) — process_message → tg_send_message обратно.
    """
    if not isinstance(update, dict):
        return
    msg = update.get("message")
    if not msg:
        return
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    text = (msg.get("text") or "").strip()
    if not chat_id:
        return

    from server.db import db_session
    from server.models import User
    from server.tg_che_relay import process_message, format_for_tg

    # Загрузим юзера + его токен один раз
    with db_session() as db:
        u = db.query(User).filter_by(id=user_id).first()
        if not u or not u.personal_tg_bot_token:
            return
        token = u.personal_tg_bot_token  # дешифруется EncryptedString при чтении
        # Сохраняем chat_id при первом /start или первом любом сообщении
        if not u.personal_tg_chat_id:
            u.personal_tg_chat_id = chat_id
            db.commit()

    if text.startswith("/start"):
        await tg_send_message(token, chat_id,
            f"👋 Привет! Я твой Че через личный бот @{u.personal_tg_bot_username or '?'}\n\n"
            "Пиши обычным текстом — отвечу как на сайте aiche.ru.\n"
            "Команды: /help · /me · /unlink (отвязать бота)")
        return
    if text.startswith("/help"):
        await tg_send_message(token, chat_id,
            "Команды: /me /unlink /help\nИли пиши обычным текстом — Че ответит.")
        return
    if text.startswith("/me"):
        balance_rub = (u.tokens_balance or 0) / 100
        await tg_send_message(token, chat_id,
            f"👤 <b>{u.email}</b>\n💰 Баланс: <b>{balance_rub:.0f} ₽</b>")
        return
    if text.startswith("/unlink"):
        await tg_send_message(token, chat_id,
            "Отвязать бота можно на aiche.ru → 📲 «Где использовать Че» → Telegram.")
        return
    if text.startswith("/"):
        await tg_send_message(token, chat_id,
            "Не понял команду. Доступные: /me /help /unlink. Или пиши текстом.")
        return

    # Relay к Че — синхронная логика, гоним через executor
    import asyncio
    loop = asyncio.get_event_loop()

    def _do_in_thread():
        with db_session() as db:
            u2 = db.query(User).filter_by(id=user_id).first()
            if not u2:
                return {"reply": "Аккаунт не найден.", "error": "no_user"}
            return process_message(db, u2, text)

    try:
        result = await loop.run_in_executor(None, _do_in_thread)
    except Exception as e:
        log.exception(f"[personal-tg] relay failed user={user_id}: {e}")
        await tg_send_message(token, chat_id,
            "😔 Что-то пошло не так. Попробуй ещё раз через минуту.")
        return

    if result.get("error") == "insufficient_funds":
        await tg_send_message(token, chat_id, result.get("reply", "Недостаточно средств."))
        return

    parts = format_for_tg(result)
    for part in parts:
        await tg_send_message(token, chat_id, part)


async def handle_personal_max_update(update: dict, user_id: int) -> None:
    """То же что handle_personal_tg_update, но для MAX. format=markdown."""
    if not isinstance(update, dict):
        return
    msg = update.get("message")
    if not msg:
        return
    sender = msg.get("sender") or {}
    body = msg.get("body") or {}
    sender_uid = str(sender.get("user_id", ""))
    text = (body.get("text") or "").strip()
    if not sender_uid:
        return

    from server.db import db_session
    from server.models import User
    from server.tg_che_relay import process_message
    from server.max_management import _format_for_max  # markdown-форматтер

    with db_session() as db:
        u = db.query(User).filter_by(id=user_id).first()
        if not u or not u.personal_max_bot_token:
            return
        token = u.personal_max_bot_token
        if not u.personal_max_user_id:
            u.personal_max_user_id = sender_uid
            db.commit()

    if text.startswith("/start"):
        await max_send_message(token, sender_uid,
            f"👋 Привет\\! Я твой Че через личный MAX\\-бот\\.\n\n"
            "Пиши обычным текстом — отвечу как на сайте\\.\n"
            "Команды: /help /me /unlink")
        return
    if text.startswith("/help"):
        await max_send_message(token, sender_uid,
            "Команды: /me /unlink /help\\.\nИли пиши обычным текстом — Че ответит\\.")
        return
    if text.startswith("/me"):
        balance_rub = (u.tokens_balance or 0) / 100
        await max_send_message(token, sender_uid,
            f"👤 **{u.email}**\n💰 Баланс: **{balance_rub:.0f} ₽**")
        return
    if text.startswith("/unlink"):
        await max_send_message(token, sender_uid,
            "Отвязать бота можно на aiche\\.ru → 📲 «Где использовать Че» → MAX\\.")
        return
    if text.startswith("/"):
        await max_send_message(token, sender_uid,
            "Не понял команду\\. Доступные: /me /help /unlink\\.")
        return

    import asyncio
    loop = asyncio.get_event_loop()

    def _do_in_thread():
        with db_session() as db:
            u2 = db.query(User).filter_by(id=user_id).first()
            if not u2:
                return {"reply": "Аккаунт не найден.", "error": "no_user"}
            return process_message(db, u2, text)

    try:
        result = await loop.run_in_executor(None, _do_in_thread)
    except Exception as e:
        log.exception(f"[personal-max] relay failed user={user_id}: {e}")
        await max_send_message(token, sender_uid,
            "😔 Что-то пошло не так\\. Попробуй ещё раз\\.")
        return

    if result.get("error") == "insufficient_funds":
        await max_send_message(token, sender_uid, result.get("reply", "Недостаточно средств."))
        return

    parts = _format_for_max(result)
    for part in parts:
        await max_send_message(token, sender_uid, part)
