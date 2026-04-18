"""Webhook обработчики для входящих сообщений мессенджеров."""
import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from server.routes.deps import get_db
from server.models import ChatBot
from server.chatbot_engine import handle_message, send_telegram, send_vk, send_avito

log = logging.getLogger("webhook")
router = APIRouter(prefix="/webhook", tags=["webhook"])


def _get_active_bot(bot_id: int, db: Session) -> ChatBot | None:
    bot = db.query(ChatBot).filter_by(id=bot_id).first()
    if not bot or bot.status != "active":
        return None
    return bot


# ── Telegram ─────────────────────────────────────────────────────────────────

@router.post("/tg/{bot_id}")
async def telegram_webhook(bot_id: int, request: Request,
                           db: Session = Depends(get_db)):
    """Обработка входящих от Telegram Bot API."""
    bot = _get_active_bot(bot_id, db)
    if not bot or not bot.tg_token:
        return {"ok": True}  # Telegram ожидает 200

    try:
        body = await request.json()
    except Exception:
        return {"ok": True}

    # Обработка callback_query (нажатие inline-кнопки)
    cb = body.get("callback_query")
    if cb:
        data = cb.get("data", "")
        chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))
        user_name = cb.get("from", {}).get("first_name", "")
        cb_id = cb.get("id")
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
        body = await request.json()
    except Exception:
        raise HTTPException(400)

    event_type = body.get("type")

    # Подтверждение сервера
    if event_type == "confirmation":
        if not bot.vk_confirmation:
            # Нужно получить confirmation code через VK API
            return "ok"
        return bot.vk_confirmation

    # Проверка secret
    if bot.vk_secret and body.get("secret") != bot.vk_secret:
        log.warning(f"[VK Bot {bot_id}] Invalid secret")
        return "ok"

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

        answer = await handle_message(bot, user_id, text, "vk", user_id)
        if answer and bot.vk_token:
            await send_vk(bot.vk_token, user_id, answer)

    return "ok"


# ── Авито Messenger ──────────────────────────────────────────────────────────

@router.post("/avito/{bot_id}")
async def avito_webhook(bot_id: int, request: Request,
                        db: Session = Depends(get_db)):
    """Обработка Avito Messenger webhook."""
    bot = _get_active_bot(bot_id, db)
    if not bot or not bot.avito_client_id:
        return {"ok": True}

    try:
        body = await request.json()
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

    answer = await handle_message(bot, chat_id, text, "avito", user_id,
                                  extra_ctx={"avito_user_id": user_id,
                                             "avito_chat_id": chat_id,
                                             "is_avito": True})
    if answer:
        await send_avito(bot, chat_id, answer)

    return {"ok": True}
