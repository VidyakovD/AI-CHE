"""
Senders: отправка сообщений в каналы (Telegram / MAX / VK / WhatsApp / Avito).
Вынесено из server/chatbot_engine.py.

Все функции работают через общий httpx.AsyncClient (HTTP). Backward-compat
re-export сделан в server/chatbot_engine.py.
"""
import logging
import os
import time

import httpx

log = logging.getLogger("chatbot")

HTTP = httpx.AsyncClient(timeout=30)


# ══════════════════════════════════════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════════════════════════════════════

async def setup_telegram_webhook(tg_token: str, webhook_url: str) -> dict:
    from server.security import tg_webhook_secret
    secret = tg_webhook_secret(tg_token)
    payload = {"url": webhook_url, "allowed_updates": ["message", "callback_query"]}
    if secret:
        payload["secret_token"] = secret
    try:
        r = await HTTP.post(
            f"https://api.telegram.org/bot{tg_token}/setWebhook",
            json=payload,
        )
        data = r.json()
        log.info(f"[TG] setWebhook → {data}")
        return data
    except Exception as e:
        log.error(f"[TG] setWebhook error: {e}")
        return {"ok": False, "description": str(e)}


async def delete_telegram_webhook(tg_token: str) -> dict:
    try:
        r = await HTTP.post(f"https://api.telegram.org/bot{tg_token}/deleteWebhook")
        return r.json()
    except Exception as e:
        return {"ok": False, "description": str(e)}


async def send_telegram(token: str, chat_id: str, text: str,
                        reply_to: int = None, parse_mode: str = "Markdown") -> dict:
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode: payload["parse_mode"] = parse_mode
    if reply_to: payload["reply_to_message_id"] = reply_to
    try:
        r = await HTTP.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)
        return r.json()
    except Exception as e:
        log.error(f"[TG] send error: {e}")
        try:
            payload.pop("parse_mode", None)
            r = await HTTP.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)
            return r.json()
        except Exception:
            return {"ok": False}


async def send_telegram_with_buttons(token: str, chat_id: str, text: str,
                                     buttons: list) -> dict:
    """Отправить сообщение с inline-кнопками. buttons = [{text, callback_data}, ...]"""
    keyboard = [[b] for b in buttons]
    payload = {
        "chat_id": chat_id, "text": text,
        "reply_markup": {"inline_keyboard": keyboard},
    }
    try:
        r = await HTTP.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)
        return r.json()
    except Exception as e:
        log.error(f"[TG buttons] {e}")
        return {"ok": False}


async def send_telegram_with_reply_keyboard(token: str, chat_id: str, text: str,
                                             buttons: list[dict],
                                             one_time: bool = True,
                                             resize: bool = True) -> dict:
    """Reply-keyboard в TG (постоянная клавиатура внизу).

    buttons: list[dict] — каждый элемент:
      {text: "...", request_contact: True} — попросить телефон
      {text: "...", request_location: True} — попросить геолокацию
      {text: "..."} — обычная кнопка-текст (бот получит как text)
    one_time=True — клавиатура исчезнет после первого нажатия.
    """
    try:
        keyboard = [[b] for b in buttons]
        payload = {
            "chat_id": str(chat_id),
            "text": text[:4096] or "Выберите вариант:",
            "reply_markup": {
                "keyboard": keyboard,
                "resize_keyboard": resize,
                "one_time_keyboard": one_time,
            },
        }
        r = await HTTP.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)
        return r.json()
    except Exception as e:
        log.error(f"[TG reply-kb] {e}")
        return {"ok": False}


async def send_telegram_photo(token: str, chat_id: str, photo: str,
                               caption: str = "", parse_mode: str = "Markdown") -> dict:
    """Отправить фото. photo — URL или относительный путь /uploads/...
    Если файл локальный — multipart upload, если URL — TG сам скачает."""
    try:
        if photo.startswith(("http://", "https://")):
            payload = {"chat_id": str(chat_id), "photo": photo}
            if caption:
                payload["caption"] = caption[:1024]
                payload["parse_mode"] = parse_mode
            r = await HTTP.post(f"https://api.telegram.org/bot{token}/sendPhoto", json=payload)
            return r.json()
        # Локальный путь — корень проекта
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        base = os.path.dirname(base)  # из server/messaging → server → корень
        abs_path = os.path.join(base, photo.lstrip("/"))
        if not os.path.exists(abs_path):
            log.error(f"[TG photo] file not found: {abs_path}")
            return {"ok": False, "description": "file not found"}
        with open(abs_path, "rb") as f:
            files = {"photo": (os.path.basename(abs_path), f)}
            data = {"chat_id": str(chat_id)}
            if caption:
                data["caption"] = caption[:1024]
                data["parse_mode"] = parse_mode
            r = await HTTP.post(f"https://api.telegram.org/bot{token}/sendPhoto",
                                files=files, data=data)
        return r.json()
    except Exception as e:
        log.error(f"[TG photo] {e}")
        return {"ok": False}


async def edit_telegram_message(token: str, chat_id: str, message_id: int,
                                 text: str, parse_mode: str = "Markdown",
                                 buttons: list[dict] | None = None) -> dict:
    """Заменить текст ранее отправленного сообщения. Когда юзер нажимает
    кнопку «Выбрать дату», заменяем «выбери услугу» на «✓ Услуга: Маникюр»
    вместо нового спама. UX становится приличным."""
    try:
        payload = {
            "chat_id": str(chat_id),
            "message_id": int(message_id),
            "text": text[:4096],
            "parse_mode": parse_mode,
        }
        if buttons:
            keyboard = [[{"text": b.get("text", "")[:64],
                          "callback_data": str(b.get("callback_data", ""))[:64]}]
                        for b in buttons[:10]]
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        r = await HTTP.post(f"https://api.telegram.org/bot{token}/editMessageText",
                            json=payload)
        return r.json()
    except Exception as e:
        log.error(f"[TG edit] {e}")
        return {"ok": False}


async def set_telegram_commands(token: str, commands: list[dict]) -> dict:
    """Установить меню команд бота — то что показывается в меню «/».
    commands: list of {"command": "start", "description": "Начать работу"}.
    Вызывается при деплое, не в каждом ответе."""
    try:
        payload = {"commands": [
            {"command": c.get("command", "").lstrip("/")[:32],
             "description": (c.get("description", "") or "")[:256]}
            for c in commands[:10]
        ]}
        r = await HTTP.post(f"https://api.telegram.org/bot{token}/setMyCommands",
                            json=payload)
        return r.json()
    except Exception as e:
        log.error(f"[TG commands] {e}")
        return {"ok": False}


async def send_telegram_chat_action(token: str, chat_id: str,
                                     action: str = "typing") -> dict:
    """«Бот печатает…» — показывается до 5 сек или до следующего сообщения.
    Вызываем перед длинным AI-вызовом, чтобы юзер не думал что бот завис."""
    try:
        r = await HTTP.post(f"https://api.telegram.org/bot{token}/sendChatAction",
                            json={"chat_id": str(chat_id), "action": action})
        return r.json()
    except Exception:
        return {"ok": False}


async def send_telegram_document(token: str, chat_id: str, file_path: str,
                                 caption: str = "") -> dict:
    """Отправить документ. file_path — относительно корня проекта."""
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    base = os.path.dirname(base)  # server/messaging → server → корень
    abs_path = os.path.join(base, file_path.lstrip("/"))
    if not os.path.exists(abs_path):
        log.error(f"[TG doc] file not found: {abs_path}")
        return {"ok": False}
    try:
        with open(abs_path, "rb") as f:
            files = {"document": (os.path.basename(abs_path), f)}
            data = {"chat_id": str(chat_id)}
            if caption: data["caption"] = caption
            r = await HTTP.post(f"https://api.telegram.org/bot{token}/sendDocument",
                                files=files, data=data)
        return r.json()
    except Exception as e:
        log.error(f"[TG doc] {e}")
        return {"ok": False}


async def send_telegram_audio(token: str, chat_id: str, file_path: str) -> dict:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    base = os.path.dirname(base)  # server/messaging → server → корень
    abs_path = os.path.join(base, file_path.lstrip("/"))
    if not os.path.exists(abs_path):
        log.error(f"[TG audio] file not found: {abs_path}")
        return {"ok": False}
    try:
        with open(abs_path, "rb") as f:
            files = {"voice": (os.path.basename(abs_path), f)}
            r = await HTTP.post(f"https://api.telegram.org/bot{token}/sendVoice",
                                files=files, data={"chat_id": str(chat_id)})
        return r.json()
    except Exception as e:
        log.error(f"[TG audio] {e}")
        return {"ok": False}


# ══════════════════════════════════════════════════════════════════════════════
#  MAX (https://max.ru) — российский мессенджер
# ══════════════════════════════════════════════════════════════════════════════
# API: https://botapi.max.ru. Auth: Authorization: Bearer <token> (header).
# Раньше было ?access_token=<token> но MAX deprecated этот способ —
# возвращает 401 с code='verify.token'. Docs: https://dev.max.ru/docs-api
# Webhook: POST /subscriptions с {url}. Send: POST /messages?user_id=<>&text=...

MAX_API = "https://botapi.max.ru"


def _max_headers(max_token: str) -> dict:
    """Auth-header для MAX API. Заменил query ?access_token=... после
    deprecation в апреле 2026.

    ВАЖНО: MAX ожидает голый токен в Authorization БЕЗ префикса 'Bearer '
    (несмотря на формулировку их error 'use Authorization header').
    Проверено живьём: с 'Bearer ' → 401, без префикса → 200 OK.
    """
    return {"Authorization": max_token}


async def setup_max_webhook(max_token: str, webhook_url: str) -> dict:
    """Подписать MAX-бота на webhook. Возвращает {ok, description}.
    Требует HTTPS — иначе MAX откажет."""
    if not webhook_url.startswith("https://"):
        log.error(f"[MAX] webhook URL must be HTTPS: {webhook_url[:60]}")
        return {"ok": False, "description": "Webhook URL должен быть HTTPS"}
    try:
        r = await HTTP.post(
            f"{MAX_API}/subscriptions",
            headers=_max_headers(max_token),
            json={"url": webhook_url, "update_types": ["message_created", "message_callback"]},
        )
        try:
            data = r.json() if r.content else {}
        except Exception:
            data = {"raw": r.text[:200]}
        ok = r.status_code == 200
        log.info(f"[MAX] subscribe → {r.status_code} {data}")
        return {"ok": ok, "description": data.get("message", "") if isinstance(data, dict) else "",
                "status_code": r.status_code}
    except Exception as e:
        log.error(f"[MAX] subscribe error: {type(e).__name__}")
        return {"ok": False, "description": type(e).__name__}


async def delete_max_webhook(max_token: str, webhook_url: str | None = None) -> dict:
    """Отписать webhook. Если webhook_url не задан — снимает все подписки бота."""
    try:
        params = {}
        if webhook_url:
            params["url"] = webhook_url
        r = await HTTP.delete(f"{MAX_API}/subscriptions",
                               headers=_max_headers(max_token), params=params)
        return {"ok": r.status_code == 200, "status_code": r.status_code}
    except Exception as e:
        return {"ok": False, "description": type(e).__name__}


async def send_max(max_token: str, user_id: str | int, text: str,
                   format_: str = "markdown",
                   buttons: list[dict] | None = None) -> dict:
    """Отправить сообщение в MAX. user_id — int из update.message.sender.user_id.

    buttons (опц): список dict {text, callback_data} — отправляются как
    inline keyboard (attachment type=inline_keyboard, по докам MAX).
    """
    try:
        params = {"user_id": str(user_id)}
        body = {"text": text[:4000]}
        if format_:
            body["format"] = format_
        if buttons:
            body["attachments"] = [{
                "type": "inline_keyboard",
                "payload": {
                    "buttons": [[{
                        "type": "callback",
                        "text": b.get("text", "")[:64],
                        "payload": str(b.get("callback_data", ""))[:64],
                    }] for b in buttons[:10]]
                }
            }]
        r = await HTTP.post(f"{MAX_API}/messages",
                            headers=_max_headers(max_token),
                            params=params, json=body)
        try:
            data = r.json() if r.content else {}
        except Exception:
            data = {"raw": r.text[:200]}
        if r.status_code != 200:
            log.warning(f"[MAX] send failed {r.status_code}: {data}")
            # 401/403 = токен мёртв (отозван в MAX или удалён бот). Помечаем
            # max_webhook_set=False чтобы UI показал «требует переподключения»
            # и фоновые tick'и не молотили API в холостую.
            if r.status_code in (401, 403):
                _disable_max_bot_for_token(max_token,
                                            f"max_send {r.status_code}")
        return {"ok": r.status_code == 200, "data": data, "status_code": r.status_code}
    except Exception as e:
        log.error(f"[MAX] send error: {type(e).__name__}")
        return {"ok": False, "description": type(e).__name__}


def _disable_max_bot_for_token(max_token: str, reason: str) -> None:
    """Помечает все боты с этим max_token как отвалившиеся."""
    from server.db import db_session
    from server.models import ChatBot
    try:
        with db_session() as db:
            bots = db.query(ChatBot).all()
            updated = 0
            for b in bots:
                if b.max_token == max_token and b.max_webhook_set:
                    b.max_webhook_set = False
                    updated += 1
            if updated:
                db.commit()
                log.warning(f"[MAX] disabled {updated} bot(s) by token: {reason}")
                from server.audit_log import log_action
                log_action("bot.max_disconnected", target_type="bot",
                           level="warn", success=False,
                           details={"reason": reason, "bots_affected": updated})
    except Exception as e:
        log.error(f"[MAX] disable_max_bot error: {type(e).__name__}")


async def send_max_with_reply_keyboard(max_token: str, user_id: str | int, text: str,
                                         buttons: list[dict]) -> dict:
    """Reply-keyboard в MAX (постоянная клавиатура).

    buttons элементы:
      {text: "...", request_contact: True} — попросить телефон
      {text: "...", request_geolocation: True} — попросить локацию
      {text: "..."} — обычная кнопка-текст
    MAX-API использует attachments: type=request_keyboard.
    """
    try:
        params = {"user_id": str(user_id)}
        mx_buttons = []
        for b in buttons[:10]:
            row = {"text": b.get("text", "")[:64], "type": "text"}
            if b.get("request_contact"):
                row["type"] = "request_contact"
            elif b.get("request_geolocation") or b.get("request_location"):
                row["type"] = "request_geolocation"
            mx_buttons.append([row])
        body = {
            "text": (text or "Выберите вариант:")[:4000],
            "attachments": [{
                "type": "request_keyboard",
                "payload": {"buttons": mx_buttons},
            }],
        }
        r = await HTTP.post(f"{MAX_API}/messages",
                            headers=_max_headers(max_token),
                            params=params, json=body)
        return {"ok": r.status_code == 200, "status_code": r.status_code}
    except Exception as e:
        log.error(f"[MAX reply-kb] {type(e).__name__}")
        return {"ok": False}


async def send_max_photo(max_token: str, user_id: str | int, photo: str,
                          caption: str = "") -> dict:
    """Отправить фото в MAX. photo — URL (в идеале) или путь /uploads/...
    Для локальных файлов сначала загружаем через POST /uploads → получаем url."""
    try:
        photo_url = photo
        if not photo.startswith(("http://", "https://")):
            app_url = os.getenv("APP_URL", "https://aiche.ru").rstrip("/")
            photo_url = f"{app_url}{photo if photo.startswith('/') else '/' + photo}"
        params = {"user_id": str(user_id)}
        body = {
            "text": (caption or "")[:1000],
            "attachments": [{"type": "image", "payload": {"url": photo_url}}],
        }
        r = await HTTP.post(f"{MAX_API}/messages",
                            headers=_max_headers(max_token),
                            params=params, json=body)
        return {"ok": r.status_code == 200, "status_code": r.status_code}
    except Exception as e:
        log.error(f"[MAX photo] {type(e).__name__}")
        return {"ok": False}


async def get_max_me(max_token: str) -> dict:
    """Возвращает {user_id, name, username, ...} бота. Используем для валидации токена."""
    try:
        r = await HTTP.get(f"{MAX_API}/me", headers=_max_headers(max_token))
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log.error(f"[MAX] me error: {type(e).__name__}")
    return {}


# ══════════════════════════════════════════════════════════════════════════════
#  VK
# ══════════════════════════════════════════════════════════════════════════════

async def send_vk(token: str, user_id: str, text: str) -> dict:
    import random
    try:
        r = await HTTP.post("https://api.vk.com/method/messages.send", data={
            "user_id": user_id, "message": text,
            "random_id": random.randint(1, 2**31),
            "access_token": token, "v": "5.131",
        })
        return r.json()
    except Exception as e:
        log.error(f"[VK] send error: {e}")
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
#  WhatsApp via Wazzup24 (российский официальный посредник)
# ══════════════════════════════════════════════════════════════════════════════
# https://wazzup24.com/help/api/v3/
# Схема: бот общается с Wazzup24 REST API; Wazzup24 общается с WhatsApp Cloud
# (официально, без серых обходов). Преимущества: российская инфра без VPN,
# договор оферта с РФ-юрлицом, ПДн обрабатываются в РФ.

_WAZZUP_API = "https://api.wazzup24.com/v3"


async def send_whatsapp(api_key: str, channel_id: str, chat_id: str,
                          text: str) -> dict:
    """Отправить текстовое сообщение в WhatsApp через Wazzup24.

    chat_id — это телефонный номер собеседника (E.164, например 79991234567).
    Wazzup24 принимает в поле chatId. Текст через text. content_type='text'.
    """
    if not api_key or not channel_id:
        return {"error": "wazzup_api_key/channel_id not set"}
    try:
        r = await HTTP.post(
            f"{_WAZZUP_API}/message",
            json={
                "channelId": channel_id,
                "chatId": str(chat_id),
                "chatType": "whatsapp",
                "text": text[:4096],
            },
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=20.0,
        )
        if r.status_code >= 400:
            log.warning(f"[Wazzup] {r.status_code}: {r.text[:200]}")
            return {"error": f"HTTP {r.status_code}"}
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else {"ok": True}
    except Exception as e:
        log.error(f"[Wazzup] send error: {e}")
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
#  Avito
# ══════════════════════════════════════════════════════════════════════════════

_avito_tokens: dict[int, tuple[str, float]] = {}


async def _get_avito_token(bot) -> str | None:
    if not bot.avito_client_id or not bot.avito_client_secret:
        return None
    cached = _avito_tokens.get(bot.id)
    if cached and cached[1] > time.time():
        return cached[0]
    try:
        r = await HTTP.post("https://api.avito.ru/token/", data={
            "grant_type": "client_credentials",
            "client_id": bot.avito_client_id,
            "client_secret": bot.avito_client_secret,
        })
        data = r.json()
        token = data.get("access_token")
        expires_in = data.get("expires_in", 3600)
        if token:
            _avito_tokens[bot.id] = (token, time.time() + expires_in - 60)
        return token
    except Exception as e:
        log.error(f"[Avito] token error: {e}")
        return None


async def send_avito(bot, chat_id: str, text: str) -> dict:
    token = await _get_avito_token(bot)
    if not token:
        return {"error": "No Avito token"}
    try:
        r = await HTTP.post(
            f"https://api.avito.ru/messenger/v1/accounts/{bot.avito_user_id}/chats/{chat_id}/messages",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"message": {"text": text}, "type": "text"},
        )
        return r.json()
    except Exception as e:
        log.error(f"[Avito] send error: {e}")
        return {"error": str(e)}
