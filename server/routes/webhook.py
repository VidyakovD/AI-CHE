"""Webhook обработчики для входящих сообщений мессенджеров."""
import logging
import time
import threading
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from server.routes.deps import get_db
from server.models import ChatBot
from server.chatbot_engine import handle_message, send_telegram, send_vk, send_avito, send_max, send_whatsapp
from server.security import tg_webhook_secret

log = logging.getLogger("webhook")
router = APIRouter(prefix="/webhook", tags=["webhook"])

# Cap размера webhook-payload. TG/VK/MAX/Avito реальные обновления — единицы КБ.
# Без cap'а атакующий может прислать 100MB JSON → OOM воркера. На уровне
# nginx стоит client_max_body_size, но defence-in-depth.
_WEBHOOK_MAX_BYTES = 1_000_000  # 1 MB


async def _safe_json(request: Request, max_bytes: int = _WEBHOOK_MAX_BYTES):
    """Прочитать JSON из request с cap'ом на размер. Бросает HTTPException
    413 если превышен. Возвращает dict или {} при ошибке парсинга."""
    # Content-Length проверка — fast-path, если клиент честный.
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > max_bytes:
        raise HTTPException(413, "Payload too large")
    # Реальное чтение с cap'ом на случай chunked-transfer без CL
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > max_bytes:
            raise HTTPException(413, "Payload too large")
    try:
        import json as _json
        return _json.loads(body) if body else {}
    except Exception:
        return {}


def _get_active_bot(bot_id: int, db: Session) -> ChatBot | None:
    bot = db.query(ChatBot).filter_by(id=bot_id).first()
    if not bot or bot.status != "active":
        return None
    return bot


# ── Idempotency: дедупликация webhook-апдейтов ──────────────────────────────
# Мессенджеры (особенно MAX и VK) могут переотправить тот же update_id
# при сетевом сбое или рестарте. Без дедупа — двойная обработка =
# двойное списание токенов и дубль ответ юзеру.
# In-memory кэш (платформа, bot_id, update_id) → timestamp. TTL 1 час.
_DEDUP_TTL = 3600
_dedup_cache: dict[tuple, float] = {}
_dedup_lock = threading.Lock()


def _is_duplicate_update(platform: str, bot_id: int, update_id) -> bool:
    """True если этот update_id уже обрабатывался в последний час."""
    if update_id is None or update_id == "":
        return False  # без id дедуп невозможен — пропускаем
    key = (platform, int(bot_id), str(update_id))
    now = time.monotonic()
    with _dedup_lock:
        # Чистим expired по дороге
        if len(_dedup_cache) > 5000:
            cutoff = now - _DEDUP_TTL
            for k, ts in list(_dedup_cache.items()):
                if ts < cutoff:
                    _dedup_cache.pop(k, None)
        if key in _dedup_cache:
            return True
        _dedup_cache[key] = now
        return False


# ── Telegram ─────────────────────────────────────────────────────────────────

@router.post("/tg/{bot_id}")
async def telegram_webhook(bot_id: int, request: Request,
                           db: Session = Depends(get_db)):
    """Обработка входящих от Telegram Bot API."""
    bot = _get_active_bot(bot_id, db)
    if not bot or not bot.tg_token:
        return {"ok": True}  # Telegram ожидает 200

    # Проверка X-Telegram-Bot-Api-Secret-Token (защита от подделки webhook'а).
    # Без secret-заголовка — отклоняем, т.к. иначе любой может POST-ить на
    # /webhook/tg/{bot_id} от чьего угодно имени и тратить наш AI-баланс.
    expected_secret = tg_webhook_secret(bot.tg_token)
    if expected_secret:
        import hmac as _hmac
        got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not got:
            log.warning(f"[TG Bot {bot_id}] webhook без secret — отклоняем")
            raise HTTPException(401, "Secret token required (re-register webhook)")
        # Сравнение через compare_digest — защита от timing-атаки на побайтовый
        # подбор secret по network-латентности.
        if not _hmac.compare_digest(got, expected_secret):
            log.warning(f"[TG Bot {bot_id}] Invalid secret token")
            raise HTTPException(401, "Invalid secret")

    try:
        body = await _safe_json(request)
    except Exception:
        return {"ok": True}

    # Обработка callback_query (нажатие inline-кнопки)
    cb = body.get("callback_query")
    if cb:
        data = cb.get("data", "")
        chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))
        user_name = cb.get("from", {}).get("first_name", "")
        cb_id = cb.get("id")
        # Idempotency: TG может ретрайнуть тот же callback при slow ACK —
        # без дедупа второй worker пройдёт обработку и спишет деньги ещё раз.
        if cb_id and _is_duplicate_update("tg-cb", bot.id, cb_id):
            return {"ok": True}
        # ACK callback чтобы кнопка перестала крутиться
        try:
            import httpx as _hx
            async with _hx.AsyncClient(timeout=10) as c:
                await c.post(f"https://api.telegram.org/bot{bot.tg_token}/answerCallbackQuery",
                             json={"callback_query_id": cb_id})
        except Exception:
            pass
        # Обрабатываем как обычное сообщение с флагом is_callback
        answer = await handle_message(bot, chat_id, data, "tg", user_name,
                                      extra_ctx={"is_callback": True})
        if answer:
            await send_telegram(bot.tg_token, chat_id, answer)
        return {"ok": True}

    # Обработка message
    msg = body.get("message") or body.get("edited_message")
    if not msg:
        return {"ok": True}

    chat_id = str(msg["chat"]["id"])
    user_name = msg.get("from", {}).get("first_name", "")
    msg_id = msg.get("message_id")
    extra_ctx = {}

    # Idempotency: TG ретрайнет одно и то же message_id при slow-ACK или таймауте.
    # На 4 worker'ах in-memory дедуп per-process, поэтому ретрай на другой worker
    # пропустит — приходится мириться, но в пределах одного worker'а спам блокируется.
    if msg_id is not None and _is_duplicate_update("tg", bot.id, msg_id):
        return {"ok": True}

    # Voice / audio сообщение
    text = msg.get("text", "")
    if msg.get("voice") or msg.get("audio"):
        extra_ctx["is_voice"] = True
        extra_ctx["file_id"] = (msg.get("voice") or msg.get("audio")).get("file_id")
        text = text or "[voice message]"
    elif msg.get("document"):
        extra_ctx["is_file"] = True
        extra_ctx["file_id"] = msg["document"].get("file_id")
        extra_ctx["file_name"] = msg["document"].get("file_name", "file")
        text = text or msg.get("caption", "[document]")
    elif msg.get("photo"):
        extra_ctx["is_file"] = True
        photos = msg["photo"]
        if photos:
            extra_ctx["file_id"] = photos[-1].get("file_id")
        text = text or msg.get("caption", "[photo]")
    elif msg.get("contact"):
        # Юзер нажал кнопку «Поделиться номером» из reply-keyboard.
        # Сохраняем телефон в ctx — следующая нода сможет взять из ctx["customer_phone"].
        ct = msg["contact"]
        phone = ct.get("phone_number", "")
        first = ct.get("first_name", "") or user_name
        last = ct.get("last_name", "") or ""
        extra_ctx["customer_phone"] = phone
        extra_ctx["customer_name"] = (first + " " + last).strip() or user_name
        extra_ctx["is_contact"] = True
        text = text or f"📞 {phone}"
    elif msg.get("location"):
        loc = msg["location"]
        extra_ctx["customer_lat"] = loc.get("latitude")
        extra_ctx["customer_lng"] = loc.get("longitude")
        extra_ctx["is_location"] = True
        text = text or f"📍 {loc.get('latitude')},{loc.get('longitude')}"

    if not text:
        return {"ok": True}

    # Команда /start
    if text.strip() == "/start":
        welcome = f"Здравствуйте! Я бот «{bot.name}». Напишите ваш вопрос."
        await send_telegram(bot.tg_token, chat_id, welcome)
        return {"ok": True}

    # Генерация ответа через AI
    answer = await handle_message(bot, chat_id, text, "tg", user_name, extra_ctx=extra_ctx)
    if answer:
        await send_telegram(bot.tg_token, chat_id, answer, reply_to=msg_id)
    else:
        await send_telegram(bot.tg_token, chat_id,
                            "Извините, бот временно недоступен. Попробуйте позже.")

    return {"ok": True}


# ── VK Callback API ──────────────────────────────────────────────────────────

@router.post("/vk/{bot_id}")
async def vk_webhook(bot_id: int, request: Request,
                     db: Session = Depends(get_db)):
    """Обработка VK Callback API."""
    bot = db.query(ChatBot).filter_by(id=bot_id).first()
    if not bot:
        raise HTTPException(404)

    try:
        body = await _safe_json(request)
    except Exception:
        raise HTTPException(400)

    event_type = body.get("type")

    # Подтверждение сервера
    if event_type == "confirmation":
        if not bot.vk_confirmation:
            log.warning(f"[VK Bot {bot_id}] confirmation requested but vk_confirmation is empty")
            raise HTTPException(503, "vk_confirmation not set — re-save bot to fetch it")
        return bot.vk_confirmation

    # Проверка secret. ВАЖНО: secret обязателен — иначе любой может постить
    # message_new и сжигать AI-баланс владельца. Если в БД пусто — отклоняем
    # (юзеру нужно задать `vk_secret` при настройке Callback API).
    # Сравнение через compare_digest — защита от timing attack на «ловлю» secret.
    if not bot.vk_secret:
        log.warning(f"[VK Bot {bot_id}] vk_secret не настроен — отклоняем webhook")
        raise HTTPException(401, "vk_secret not configured")
    import hmac as _hmac
    body_secret = body.get("secret") or ""
    if not _hmac.compare_digest(str(body_secret), str(bot.vk_secret)):
        log.warning(f"[VK Bot {bot_id}] Invalid secret")
        raise HTTPException(401, "Invalid secret")

    if bot.status != "active":
        return "ok"

    # Новое сообщение
    if event_type == "message_new":
        obj = body.get("object", {})
        msg = obj.get("message", obj)  # VK API v5.103+ / legacy
        text = msg.get("text", "")
        user_id = str(msg.get("from_id") or msg.get("user_id", ""))

        if not text or not user_id:
            return "ok"

        # Idempotency: VK ретрайнет тот же event_id (или conversation_message_id)
        # при не-200 ответе или таймауте. Без дедупа — двойное списание баланса.
        update_id = body.get("event_id") or msg.get("conversation_message_id") or msg.get("id")
        if update_id and _is_duplicate_update("vk", bot.id, update_id):
            return "ok"

        answer = await handle_message(bot, user_id, text, "vk", user_id)
        if answer and bot.vk_token:
            await send_vk(bot.vk_token, user_id, answer)

    return "ok"


# ── Авито Messenger ──────────────────────────────────────────────────────────

@router.post("/avito/{bot_id}")
async def avito_webhook(bot_id: int, request: Request,
                        db: Session = Depends(get_db)):
    """Обработка Avito Messenger webhook.

    Авторизация: Avito Messenger сам не передаёт signature header. Защищаем
    через secret в URL `?secret=<computed>`. Без него любой мог бы POST'ить
    фейк-сообщения и сжигать AI-баланс владельца бота.
    """
    bot = _get_active_bot(bot_id, db)
    if not bot or not bot.avito_client_id:
        return {"ok": True}

    # SECURITY: проверка secret (computed = HMAC от avito_client_id + JWT_SECRET)
    expected_secret = tg_webhook_secret(bot.avito_client_id or "")
    if expected_secret:
        got_secret = request.query_params.get("secret", "")
        import hmac as _hmac
        if not got_secret or not _hmac.compare_digest(got_secret, expected_secret):
            log.warning(f"[Avito Bot {bot_id}] webhook без/с неверным secret — отклонено")
            raise HTTPException(401, "Invalid or missing secret")

    try:
        body = await _safe_json(request)
    except Exception:
        return {"ok": True}

    # Avito Messenger webhook payload
    payload = body.get("payload", body)
    value = payload.get("value", payload)

    chat_id = str(value.get("chat_id", ""))
    user_id = str(value.get("user_id", ""))
    text = ""

    # Извлекаем текст сообщения
    content = value.get("content", {})
    if isinstance(content, dict):
        text = content.get("text", "")
    elif isinstance(value, dict):
        text = value.get("text", "")
        if not text:
            last_msg = value.get("last_message", {})
            if isinstance(last_msg, dict):
                text = last_msg.get("text", "")

    # Пропускаем свои сообщения
    author_id = str(value.get("author_id", ""))
    if author_id == str(bot.avito_user_id):
        return {"ok": True}

    if not text or not chat_id:
        return {"ok": True}

    # Idempotency — Avito может ретрайнуть тот же webhook при таймауте.
    msg_id = value.get("id") or (content.get("id") if isinstance(content, dict) else None)
    if msg_id and _is_duplicate_update("avito", bot.id, msg_id):
        return {"ok": True}

    answer = await handle_message(bot, chat_id, text, "avito", user_id,
                                  extra_ctx={"avito_user_id": user_id,
                                             "avito_chat_id": chat_id,
                                             "is_avito": True})
    if answer:
        await send_avito(bot, chat_id, answer)

    return {"ok": True}


# ── MAX (https://max.ru) ─────────────────────────────────────────────────────

@router.post("/max/{bot_id}")
async def max_webhook(bot_id: int, request: Request,
                      db: Session = Depends(get_db)):
    """Обработка входящих от MAX Bot API.
    Update types: message_created, message_callback (по подписке).
    Доки: https://dev.max.ru/docs-api

    Авторизация: secret в URL-параметре `?secret=...`. MAX не передаёт
    собственный signature header, поэтому защищаем URL через secret который
    знаем только мы и MAX (зашиваем при subscribe). Computed по bot.max_token
    + JWT_SECRET, так что подделать без знания этих двух значений невозможно.
    """
    bot = _get_active_bot(bot_id, db)
    if not bot or not bot.max_token:
        return {"ok": True}

    # SECURITY: проверка secret — без неё любой может POST'ить и сжигать баланс.
    expected_secret = tg_webhook_secret(bot.max_token)
    if expected_secret:
        got_secret = request.query_params.get("secret", "")
        import hmac as _hmac
        if not got_secret or not _hmac.compare_digest(got_secret, expected_secret):
            log.warning(f"[MAX Bot {bot_id}] webhook без/с неверным secret — отклонено")
            raise HTTPException(401, "Invalid or missing secret")

    try:
        body = await _safe_json(request)
    except Exception:
        return {"ok": True}

    update_type = body.get("update_type") or body.get("type")

    # IDEMPOTENCY: дедуп по timestamp+sender+message_id чтобы переотправленный
    # webhook не вызвал двойное AI-списание.
    msg_outer = body.get("message", body.get("callback", {})) or {}
    update_id = (
        msg_outer.get("body", {}).get("mid")  # message_id в MAX
        or msg_outer.get("timestamp")
        or body.get("timestamp")
        or body.get("update_id")
    )
    if _is_duplicate_update("max", bot_id, update_id):
        log.info(f"[MAX Bot {bot_id}] duplicate update {update_id} — пропускаем")
        return {"ok": True}

    # message_created — новое сообщение от юзера
    if update_type == "message_created":
        msg = body.get("message", {}) or {}
        sender = msg.get("sender", {}) or {}
        body_obj = msg.get("body", {}) or {}
        text = body_obj.get("text", "") or ""
        user_id = str(sender.get("user_id", ""))
        user_name = sender.get("name", "") or sender.get("first_name", "") or ""
        # Пропускаем сообщения от самого бота (на всякий)
        if sender.get("is_bot"):
            return {"ok": True}

        extra_ctx = {"max_token": bot.max_token, "max_user_id": user_id, "is_max": True}
        # MAX контакт/локация приходят в attachments массиве
        attachments = body_obj.get("attachments") or msg.get("attachments") or []
        for att in attachments:
            atype = att.get("type", "")
            payload = att.get("payload") or {}
            if atype == "contact":
                extra_ctx["customer_phone"] = payload.get("phone", "") or payload.get("phone_number", "")
                extra_ctx["customer_name"] = payload.get("name", "") or user_name
                extra_ctx["is_contact"] = True
                text = text or f"📞 {extra_ctx['customer_phone']}"
            elif atype in ("location", "geolocation"):
                extra_ctx["customer_lat"] = payload.get("latitude")
                extra_ctx["customer_lng"] = payload.get("longitude")
                extra_ctx["is_location"] = True
                text = text or f"📍 {payload.get('latitude')},{payload.get('longitude')}"

        if not text or not user_id:
            return {"ok": True}
        answer = await handle_message(bot, user_id, text, "max", user_name,
                                      extra_ctx=extra_ctx)
        # ВАЖНО: проверка `if answer and answer.strip()` — пустая строка не попадёт
        # в send_max (MAX API возвращает 400 на пустой текст).
        if answer and isinstance(answer, str) and answer.strip():
            await send_max(bot.max_token, user_id, answer)

    # message_callback — нажатие на inline-кнопку
    elif update_type == "message_callback":
        cb = body.get("callback", {}) or {}
        payload = cb.get("payload", "")
        user = cb.get("user", {}) or {}
        user_id = str(user.get("user_id", ""))
        if payload and user_id:
            answer = await handle_message(bot, user_id, payload, "max",
                                          user.get("name", ""),
                                          extra_ctx={"max_token": bot.max_token,
                                                     "max_user_id": user_id,
                                                     "callback_data": payload,
                                                     "is_max": True,
                                                     "is_callback": True})
            if answer and isinstance(answer, str) and answer.strip():
                await send_max(bot.max_token, user_id, answer)

    return {"ok": True}


# ── Management bot webhook ────────────────────────────────────────────────
# Отдельный TG-бот для управления АГЕНТАМИ (не клиентский, а для владельца).
# Рекомендуемый URL (header-only, секрет НЕ светится в логах nginx/proxy):
#   curl -F url=https://aiche.ru/webhook/tg-mgmt \
#        -F secret_token={SECRET} \
#        https://api.telegram.org/bot{TOKEN}/setWebhook
# Старый URL /webhook/tg-mgmt/{secret} остаётся для backward-compat.


async def _tg_mgmt_handle(request: Request) -> dict:
    """Общая логика обработки апдейта от management-бота.

    Idempotency: TG re-delivers update при timeout/5xx. process_message
    списывает токены, создаёт Transaction и AgentMessage — повторная
    обработка = double-charge. Используем _is_duplicate_update по update_id
    (bot_id=0 — management-бот один на платформу).
    """
    from server.tg_management import handle_update
    try:
        body = await _safe_json(request)
    except Exception:
        return {"ok": True}
    update_id = body.get("update_id") if isinstance(body, dict) else None
    if _is_duplicate_update("tg-mgmt", 0, update_id):
        log.info(f"[tg-mgmt] duplicate update_id={update_id} — skipped")
        return {"ok": True}
    try:
        await handle_update(body)
    except Exception as e:
        log.error(f"[tg-mgmt] update handler error: {type(e).__name__}: {e}")
    return {"ok": True}


def _tg_mgmt_check_header(request: Request) -> None:
    """Проверка X-Telegram-Bot-Api-Secret-Token (constant-time)."""
    import hmac
    from server.tg_management import _bot_token, is_configured
    if not is_configured():
        raise HTTPException(503, "Management bot not configured (TG_MGMT_BOT_TOKEN missing)")
    expected = tg_webhook_secret(_bot_token() or "")
    if not expected:
        raise HTTPException(503, "Webhook secret not derivable")
    got_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(got_header, expected):
        raise HTTPException(401, "Invalid secret header")


@router.post("/tg-mgmt")
async def telegram_mgmt_webhook_v2(request: Request):
    """Header-only webhook (рекомендуется). Secret в `X-Telegram-Bot-Api-Secret-Token`."""
    _tg_mgmt_check_header(request)
    return await _tg_mgmt_handle(request)


@router.post("/tg-mgmt/{path_secret}")
async def telegram_mgmt_webhook_legacy(path_secret: str, request: Request):
    """Legacy webhook с secret в path. Header-проверка — основная,
    path сравнивается, но не критичен (path может уйти в access-log).
    Оставлен для уже настроенных Telegram webhook'ов.
    """
    _tg_mgmt_check_header(request)
    return await _tg_mgmt_handle(request)


# ── MAX management bot webhook ────────────────────────────────────────────
# Симметрично tg-mgmt, но для MAX (max.ru). MAX webhook регистрируется
# через POST https://botapi.max.ru/subscriptions {url}. Защищаем через
# path-secret (MAX не поддерживает Telegram-style header secret).
#
# Установить webhook (после получения MAX_MGMT_BOT_TOKEN):
#   curl -X POST -H "Authorization: $MAX_MGMT_BOT_TOKEN" \
#        -H "Content-Type: application/json" \
#        -d '{"url":"https://aiche.ru/webhook/max-mgmt/<SECRET>","update_types":["message_created"]}' \
#        https://botapi.max.ru/subscriptions


def _max_mgmt_check_secret(path_secret: str) -> None:
    """Проверка что path-secret совпадает с derived из MAX_MGMT_BOT_TOKEN."""
    import hmac
    from server.max_management import _bot_token, is_configured
    if not is_configured():
        raise HTTPException(503, "MAX management bot not configured (MAX_MGMT_BOT_TOKEN missing)")
    expected = tg_webhook_secret(_bot_token() or "")  # та же derivation что и для TG
    if not expected:
        raise HTTPException(503, "Webhook secret not derivable")
    if not hmac.compare_digest(path_secret, expected):
        raise HTTPException(401, "Invalid path secret")


async def _max_mgmt_handle(request: Request) -> dict:
    """Общая логика обработки апдейта от MAX management-бота.

    Idempotency: MAX тоже re-deliver'ит. Update_id для MAX —
    message.timestamp + sender.user_id (MAX-update нет общего ID,
    используем составной ключ). Если оба отсутствуют — обрабатываем
    каждый раз (не идемпотентно, но это лучше чем drop legitimate).
    """
    from server.max_management import handle_update
    try:
        body = await _safe_json(request)
    except Exception:
        return {"ok": True}
    # Составной id: timestamp + sender.user_id (если message есть)
    update_id = None
    if isinstance(body, dict):
        msg = body.get("message") or {}
        sender = msg.get("sender") or {}
        ts = msg.get("timestamp") or msg.get("created_at")
        uid = sender.get("user_id")
        if ts and uid:
            update_id = f"{ts}:{uid}"
    if update_id and _is_duplicate_update("max-mgmt", 0, update_id):
        log.info(f"[max-mgmt] duplicate update={update_id} — skipped")
        return {"ok": True}
    try:
        await handle_update(body)
    except Exception as e:
        log.error(f"[max-mgmt] update handler error: {type(e).__name__}: {e}")
    return {"ok": True}


@router.post("/max-mgmt/{path_secret}")
async def max_mgmt_webhook(path_secret: str, request: Request):
    """MAX webhook. Path-secret — derived из MAX_MGMT_BOT_TOKEN."""
    _max_mgmt_check_secret(path_secret)
    return await _max_mgmt_handle(request)


# ── Personal-боты юзеров (модель «свой бот», 2026-05-22) ──────────────────
# Заменяет общие tg-mgmt/max-mgmt. Каждый юзер создаёт свой бот в @BotFather,
# сохраняет токен через /user/personal-bot/tg/connect → платформа ставит
# webhook на /webhook/personal-tg/<sha256(JWT+token)[:24]>. При POST'е сюда
# находим юзера по hash → relay в Че от его имени.
#
# Безопасность: hash не обратим в token (нужен JWT_SECRET для проверки).
# Идемпотентность: дедуп через _is_duplicate_update("personal-tg", user_id,
# update_id) — защита от re-delivery при 5xx/timeout.


@router.post("/personal-tg/{token_hash}")
async def personal_tg_webhook(token_hash: str, request: Request,
                               db: Session = Depends(get_db)):
    """Webhook для personal TG-бота юзера. Routing по token_hash."""
    from server.personal_bot_relay import find_user_by_tg_token_hash, handle_personal_tg_update
    user = find_user_by_tg_token_hash(db, token_hash)
    if not user:
        # 200 OK без работы — Telegram не должен retry'ить (плохой hash = leak/stale)
        return {"ok": True}
    try:
        body = await _safe_json(request)
    except Exception:
        return {"ok": True}
    update_id = body.get("update_id") if isinstance(body, dict) else None
    if _is_duplicate_update("personal-tg", user.id, update_id):
        log.info(f"[personal-tg] duplicate user={user.id} update={update_id}")
        return {"ok": True}
    try:
        await handle_personal_tg_update(body, user.id)
    except Exception as e:
        log.error(f"[personal-tg] handler error user={user.id}: {type(e).__name__}: {e}")
    return {"ok": True}


@router.post("/personal-max/{token_hash}")
async def personal_max_webhook(token_hash: str, request: Request,
                                db: Session = Depends(get_db)):
    """Webhook для personal MAX-бота юзера."""
    from server.personal_bot_relay import find_user_by_max_token_hash, handle_personal_max_update
    user = find_user_by_max_token_hash(db, token_hash)
    if not user:
        return {"ok": True}
    try:
        body = await _safe_json(request)
    except Exception:
        return {"ok": True}
    # MAX не присылает update_id — используем (timestamp, sender.user_id)
    update_id = None
    if isinstance(body, dict):
        msg = body.get("message") or {}
        sender = msg.get("sender") or {}
        ts = msg.get("timestamp") or msg.get("created_at")
        uid = sender.get("user_id")
        if ts and uid:
            update_id = f"{ts}:{uid}"
    if update_id and _is_duplicate_update("personal-max", user.id, update_id):
        log.info(f"[personal-max] duplicate user={user.id} update={update_id}")
        return {"ok": True}
    try:
        await handle_personal_max_update(body, user.id)
    except Exception as e:
        log.error(f"[personal-max] handler error user={user.id}: {type(e).__name__}: {e}")
    return {"ok": True}


# ── WhatsApp через Wazzup24 ────────────────────────────────────────────────


@router.post("/wazzup/{bot_id}")
async def wazzup_webhook(bot_id: int, request: Request,
                          db: Session = Depends(get_db)):
    """Webhook от Wazzup24 — приходит при новом сообщении в WhatsApp.

    Wazzup24 шлёт массив `messages` где каждый объект содержит:
      - chatId — телефон собеседника (E.164)
      - text — текст сообщения
      - status='inbound'/'outbound' — нам нужны inbound
      - channelId — наш канал (проверяем что совпадает)

    Защита: путь содержит `bot_id`, мы матчим channelId. Подмена возможна
    только если знаешь и bot_id, и channelId — обычно достаточно.

    Secret проверяется ДВУМЯ путями (по приоритету):
      1. HTTP-header `X-Wazzup-Signature: <secret>` — рекомендуемый способ
         (не попадает в nginx access-логи).
      2. Query-param `?secret=<secret>` — legacy fallback (DEPRECATED:
         попадает в логи nginx и Referer-заголовки. Поддерживаем для
         плавной миграции — старые URL продолжают работать).

    Все секреты — стандартные tg_webhook_secret(api_key).
    """
    from server.security import tg_webhook_secret
    bot = db.query(ChatBot).filter_by(id=bot_id).first()
    if not bot or not bot.wazzup_api_key or not bot.wazzup_channel_id:
        return {"ok": True}
    # Проверка secret (computed = HMAC от api_key)
    expected = tg_webhook_secret(bot.wazzup_api_key or "")
    if expected:
        # Сначала пробуем header (рекомендуемый), потом query (legacy)
        got = (request.headers.get("x-wazzup-signature")
                or request.query_params.get("secret", ""))
        import hmac as _hmac
        if not got or not _hmac.compare_digest(got, expected):
            log.warning(f"[Wazzup Bot {bot_id}] invalid secret")
            raise HTTPException(401, "Invalid secret")
    if bot.status != "active":
        return {"ok": True}
    try:
        body = await _safe_json(request)
    except Exception:
        return {"ok": True}
    msgs = body.get("messages") or []
    for m in msgs:
        if m.get("status") not in ("inbound", "incoming"):
            continue
        if str(m.get("channelId", "")) != str(bot.wazzup_channel_id):
            continue
        chat_id = str(m.get("chatId") or "").strip()
        text = (m.get("text") or "").strip()
        if not chat_id or not text:
            continue
        # Обработка через стандартный handle_message (платформа='whatsapp')
        try:
            answer = await handle_message(bot, chat_id, text, "whatsapp", chat_id)
        except Exception as e:
            log.error(f"[Wazzup Bot {bot_id}] handle_message error: {e}")
            continue
        if answer:
            await send_whatsapp(bot.wazzup_api_key, bot.wazzup_channel_id,
                                  chat_id, answer)
    return {"ok": True}
