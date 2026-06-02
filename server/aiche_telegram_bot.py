"""Общий @aiche_bot — Telegram-бот платформы AI Студия Че.

Отличается от tg_management.py / tg_che_relay.py / personal_bot_relay.py:
  - tg_management.py    — legacy общий бот для УПРАВЛЕНИЯ юзерами уже-юзерами
                          сайта (привязка через /link <code>, push-уведомления).
  - personal_bot_relay  — каждый юзер подключает СВОЙ бот через @BotFather.
  - aiche_telegram_bot  — ЭТО общий бот платформы (@aiche_bot) для НОВЫХ
                          юзеров: пишет /start → auto-create User по
                          tg_user_id → пользуется AI с балансом aiche.ru.

UX:
  /start  → главное меню с кнопками: Баланс / Пополнить / Чат / Картинка /
            Видео / Настройки
  callback_query → роутинг по data в pluggable handlers

Webhook: POST /webhook/aiche-tg/<secret>
ENV:
  AICHE_TG_BOT_TOKEN — токен бота от @BotFather
  AICHE_TG_BOT_WEBHOOK_SECRET — секрет в URL (для защиты webhook'а)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Optional

import httpx

log = logging.getLogger("aiche-tg-bot")

TG_API_BASE = "https://api.telegram.org/bot"


# ── Configuration ─────────────────────────────────────────────────────────


def _bot_token() -> Optional[str]:
    t = (os.getenv("AICHE_TG_BOT_TOKEN") or "").strip()
    return t or None


def _webhook_secret() -> Optional[str]:
    s = (os.getenv("AICHE_TG_BOT_WEBHOOK_SECRET") or "").strip()
    return s or None


def is_configured() -> bool:
    return bool(_bot_token()) and bool(_webhook_secret())


# ── Low-level Telegram API ────────────────────────────────────────────────


async def _tg_call(method: str, payload: dict) -> Optional[dict]:
    """Базовый вызов Telegram API.

    Проксирование: api.telegram.org с РФ-IP заблокирован с 2026-05-28 —
    прокси берётся из TG_HTTPS_PROXY env (тот же что у personal_bot_relay).
    Если env пуст — ходим напрямую (вернётся если разблокируют)."""
    token = _bot_token()
    if not token:
        log.warning(f"[aiche-tg] no token, skipping {method}")
        return None
    proxy = (os.getenv("TG_HTTPS_PROXY") or "").strip() or None
    url = f"{TG_API_BASE}{token}/{method}"
    try:
        kwargs: dict = {"timeout": 20}
        if proxy:
            kwargs["proxy"] = proxy
        async with httpx.AsyncClient(**kwargs) as client:
            r = await client.post(url, json=payload)
            if r.status_code != 200:
                log.warning(f"[aiche-tg] {method} → {r.status_code}: {r.text[:200]}")
                return None
            return r.json()
    except Exception as e:
        log.warning(f"[aiche-tg] {method} exception: {type(e).__name__}: {e}")
        return None


async def send_message(chat_id: str, text: str,
                        reply_markup: Optional[dict] = None,
                        parse_mode: str = "HTML") -> Optional[dict]:
    payload = {
        "chat_id": str(chat_id),
        "text": text[:4000],
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return await _tg_call("sendMessage", payload)


async def edit_message(chat_id: str, message_id: int, text: str,
                        reply_markup: Optional[dict] = None,
                        parse_mode: str = "HTML") -> Optional[dict]:
    payload = {
        "chat_id": str(chat_id),
        "message_id": int(message_id),
        "text": text[:4000],
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return await _tg_call("editMessageText", payload)


async def answer_callback(callback_query_id: str, text: str = "",
                           show_alert: bool = False) -> None:
    await _tg_call("answerCallbackQuery", {
        "callback_query_id": callback_query_id,
        "text": text[:200],
        "show_alert": show_alert,
    })


async def send_chat_action(chat_id: str, action: str = "typing") -> None:
    """Показать «бот печатает...» в TG. Без await ответа.
    action: typing | upload_photo | upload_video | record_video..."""
    await _tg_call("sendChatAction", {"chat_id": str(chat_id), "action": action})


async def send_photo(chat_id: str, photo_url: str,
                      caption: Optional[str] = None,
                      reply_markup: Optional[dict] = None) -> Optional[dict]:
    """sendPhoto через URL (TG скачает сам). photo_url должен быть публично
    доступен — у нас /uploads/* проксируется через nginx из aiche.ru."""
    payload: dict = {"chat_id": str(chat_id), "photo": photo_url}
    if caption:
        payload["caption"] = caption[:1024]
        payload["parse_mode"] = "HTML"
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return await _tg_call("sendPhoto", payload)


async def send_video(chat_id: str, video_url: str,
                      caption: Optional[str] = None,
                      reply_markup: Optional[dict] = None) -> Optional[dict]:
    """sendVideo через URL. TG скачает MP4 и пришлёт юзеру."""
    payload: dict = {"chat_id": str(chat_id), "video": video_url}
    if caption:
        payload["caption"] = caption[:1024]
        payload["parse_mode"] = "HTML"
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return await _tg_call("sendVideo", payload)


# ── Conversation state (in-memory, TTL 10 мин) ────────────────────────────
# tg_user_id → {mode: "chat"|"image"|"video", model: "claude-sonnet", expires_at}
# Для multi-worker: каждый воркер свой dict — если юзер попал на другой воркер
# при следующем сообщении, режим теряется (UX: «Я ожидал текст для X»). Это
# некритично — юзер просто перевыберет модель. Redis-backed state — отдельный
# спринт когда станет важно.

import threading

_STATE_LOCK = threading.Lock()
_USER_STATE: dict[str, dict] = {}
_STATE_TTL_SEC = 600  # 10 минут


def _set_state(tg_uid: str, **kwargs) -> None:
    with _STATE_LOCK:
        # GC устаревших
        now = time.time()
        expired = [k for k, v in _USER_STATE.items()
                   if v.get("expires_at", 0) < now]
        for k in expired:
            _USER_STATE.pop(k, None)
        kwargs["expires_at"] = now + _STATE_TTL_SEC
        _USER_STATE[tg_uid] = kwargs


def _get_state(tg_uid: str) -> Optional[dict]:
    with _STATE_LOCK:
        st = _USER_STATE.get(tg_uid)
        if not st:
            return None
        if st.get("expires_at", 0) < time.time():
            _USER_STATE.pop(tg_uid, None)
            return None
        return dict(st)


def _clear_state(tg_uid: str) -> None:
    with _STATE_LOCK:
        _USER_STATE.pop(tg_uid, None)


# ── Identity: find-or-create user by tg_user_id ───────────────────────────


def _find_or_create_user(tg_uid: str, tg_username: str = "",
                          display_name: str = "") -> int:
    """Найти юзера по tg_user_id или создать нового. Возвращает user_id.

    Используем server.db.db_session (контекст-менеджер с rollback).
    Идемпотентно: повторный /start не плодит дубликатов.

    Возвращаем int а не User — чтобы избежать DetachedInstanceError при
    обращении к атрибутам объекта вне сессии.
    """
    import secrets as _secrets
    from datetime import timedelta
    from server.db import db_session
    from server.models import User, Transaction
    from server.pricing import get_price
    with db_session() as db:
        u = db.query(User).filter_by(tg_user_id=tg_uid).first()
        if u:
            # Обновим username если изменился
            if tg_username and u.tg_username != tg_username:
                u.tg_username = tg_username
                db.commit()
            return int(u.id)

        # Trial-кредит — та же политика что в Internal API /identify.
        trial_kop = max(0, int(get_price("multi_surface.trial_credits_kop",
                                          default=50_000)))
        trial_days = max(0, int(get_price("multi_surface.trial_days",
                                            default=14)))
        from datetime import datetime as _dt
        trial_ends = (_dt.utcnow() + timedelta(days=trial_days)
                      if trial_days > 0 else None)

        # Auto-create: синтетический email чтобы UNIQUE NOT NULL констрейнт прошёл
        synthetic_email = f"tg-{tg_uid}@aiche.local"
        u = User(
            email=synthetic_email,
            password_hash="!",  # неактивный, login только через TG
            name=display_name or tg_username or f"TG user {tg_uid}",
            tg_user_id=tg_uid,
            tg_username=tg_username,
            is_verified=False,
            is_active=True,
            agreed_to_terms=True,
            tokens_balance=trial_kop,
            trial_ends_at=trial_ends,
            referral_code=_secrets.token_hex(4).upper(),
        )
        db.add(u)
        db.flush()
        if trial_kop > 0:
            db.add(Transaction(
                user_id=u.id, type="bonus", tokens_delta=trial_kop,
                description="[aiche_bot] trial credits on first /start",
            ))
        db.commit()
        db.refresh(u)
        log.info(f"[aiche-tg] auto-created user_id={u.id} for tg_uid={tg_uid} "
                 f"trial_kop={trial_kop}")
        return int(u.id)


def get_user_balance_kop(user_id: int) -> int:
    from server.db import db_session
    from server.billing import get_balance
    with db_session() as db:
        return get_balance(db, user_id)


# ── Menu builders ─────────────────────────────────────────────────────────


def _main_menu_kb() -> dict:
    """Inline keyboard главного меню."""
    return {"inline_keyboard": [
        [{"text": "💰 Баланс", "callback_data": "balance"}],
        [{"text": "💳 Пополнить", "callback_data": "topup"}],
        [{"text": "🤖 Чат с AI", "callback_data": "menu_chat"}],
        [{"text": "🎨 Картинка (GPT-image)", "callback_data": "menu_image"}],
        [{"text": "🎬 Видео (Kling)", "callback_data": "menu_video"}],
        [{"text": "⚙ Настройки", "callback_data": "menu_settings"}],
    ]}


def _back_kb() -> dict:
    """Только кнопка «← Назад»."""
    return {"inline_keyboard": [
        [{"text": "← Назад", "callback_data": "menu_main"}],
    ]}


# ── Chat-with-AI submenu (Stage 2) ────────────────────────────────────────
# 5 моделей. Внутреннее имя соответствует server.ai.MODELS keys.

CHAT_MODELS = [
    ("claude",          "🟠 Claude Haiku (быстрый, дешёвый)"),
    ("claude-sonnet",   "🟠 Claude Sonnet (флагман Anthropic)"),
    ("openai",          "🔵 GPT-4o (OpenAI)"),
    ("grok",            "⚡ Grok 3 (xAI)"),
    ("perplexity",      "🌐 Perplexity (с поиском в интернете)"),
]


def _chat_models_kb() -> dict:
    rows = [
        [{"text": label, "callback_data": f"chat:{mid}"}]
        for mid, label in CHAT_MODELS
    ]
    rows.append([{"text": "← Назад", "callback_data": "menu_main"}])
    return {"inline_keyboard": rows}


def _chat_active_kb() -> dict:
    """Клавиатура когда юзер «в чате» — позволяет сменить модель или выйти."""
    return {"inline_keyboard": [
        [{"text": "🔄 Сменить модель", "callback_data": "menu_chat"}],
        [{"text": "🏠 Главное меню", "callback_data": "menu_main"}],
    ]}


def _get_model_label(model_id: str) -> str:
    for mid, label in CHAT_MODELS:
        if mid == model_id:
            return label
    return model_id


def _format_balance(balance_kop: int) -> str:
    rub = balance_kop / 100.0
    return f"{rub:.2f} ₽"


def _greet_text(balance_kop: int) -> str:
    return (
        f"👋 <b>AI Студия Че</b>\n\n"
        f"💰 Баланс: <b>{_format_balance(balance_kop)}</b>\n\n"
        f"Выбери что нужно:"
    )


# ── Handlers ──────────────────────────────────────────────────────────────


async def handle_update(update: dict) -> None:
    """Главная точка входа. Принимает Telegram update, диспатчит."""
    if not isinstance(update, dict):
        return
    if "callback_query" in update:
        await _handle_callback(update["callback_query"])
        return
    msg = update.get("message")
    if msg:
        await _handle_message(msg)


async def _handle_message(msg: dict) -> None:
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    if not chat_id:
        return
    text = (msg.get("text") or "").strip()
    from_user = msg.get("from") or {}
    tg_uid = str(from_user.get("id", ""))
    tg_username = from_user.get("username", "")
    full_name = ((from_user.get("first_name") or "") + " " +
                  (from_user.get("last_name") or "")).strip()
    if not tg_uid:
        return

    # /start [arg]
    if text.startswith("/start"):
        # /start LINK_<code> — deeplink с сайта для привязки tg_user_id
        # к существующему aiche.ru-аккаунту (см. server/link_codes.py).
        m = re.match(r"^/start\s+LINK_([A-Z0-9]+)$", text, re.IGNORECASE)
        if m:
            code = m.group(1).upper()
            await _do_deeplink(chat_id, tg_uid, tg_username, full_name, code)
            return
        await _do_start(chat_id, tg_uid, tg_username, full_name)
        return
    if text == "/menu":
        await _do_start(chat_id, tg_uid, tg_username, full_name)
        return
    if text == "/balance":
        user_id = _find_or_create_user(tg_uid, tg_username, full_name)
        balance_kop = get_user_balance_kop(user_id)
        await send_message(chat_id,
            f"💰 Баланс: <b>{_format_balance(balance_kop)}</b>",
            reply_markup=_back_kb())
        return

    # Если юзер в режиме «жду промпта» — обрабатываем как чат с AI.
    state = _get_state(tg_uid)
    if state:
        mode = state.get("mode")
        user_id = _find_or_create_user(tg_uid, tg_username, full_name)
        if mode == "chat":
            await _do_chat_message(chat_id, user_id, tg_uid, text,
                                    state.get("model") or "claude")
            return
        if mode == "image":
            await _do_image_message(chat_id, user_id, text,
                                     state.get("model") or "gpt-image")
            return
        if mode == "video":
            await _do_video_message(chat_id, user_id, text,
                                     state.get("model") or "kling")
            return

    # Иначе — короткая подсказка с меню
    await send_message(chat_id,
        "Я понимаю команды /start /menu /balance.\n\n"
        "Для AI-чата / картинок / видео — выбери из меню:",
        reply_markup=_main_menu_kb())


async def _do_start(chat_id: str, tg_uid: str, tg_username: str,
                     full_name: str) -> None:
    """Identify (auto-create) + показ главного меню."""
    user_id = _find_or_create_user(tg_uid, tg_username, full_name)
    balance_kop = get_user_balance_kop(user_id)
    await send_message(
        chat_id, _greet_text(balance_kop),
        reply_markup=_main_menu_kb(),
    )


def _link_tg_to_existing_user(target_user_id: int, tg_uid: str,
                                tg_username: str) -> tuple[bool, str]:
    """Привязать tg_user_id к существующему юзеру aiche.ru.

    Если у tg_user_id уже был auto-created анонимный аккаунт — переносит
    баланс на основной (target_user_id) и помечает auto-аккаунт ban'ом.

    Returns (ok, message). Message — для показа юзеру в TG.
    """
    from server.db import db_session
    from server.models import User, Transaction
    with db_session() as db:
        target = db.query(User).filter_by(id=target_user_id).first()
        if not target:
            return False, "Аккаунт не найден на сайте."
        # Существующий auto-account с этим tg_uid (если был)?
        prev = db.query(User).filter_by(tg_user_id=tg_uid).first()
        if prev and prev.id == target.id:
            # Уже привязан — просто подтверждаем
            return True, "Уже привязано."
        if prev and prev.id != target.id:
            # Перенос баланса с auto-account на основной
            transfer = int(prev.tokens_balance or 0)
            if transfer > 0:
                target.tokens_balance = (target.tokens_balance or 0) + transfer
                tx = Transaction(
                    user_id=target.id, type="bonus",
                    tokens_delta=transfer,
                    description=f"[merge] перенос с auto-account #{prev.id} при привязке TG",
                )
                db.add(tx)
            # Освобождаем tg_user_id на старом account'е и помечаем archived
            prev.tg_user_id = None
            prev.is_active = False
            prev.email = f"merged-{prev.id}-{prev.email}"  # освобождаем UNIQUE
        target.tg_user_id = tg_uid
        if tg_username:
            target.tg_username = tg_username
        db.commit()
        msg = f"✅ Привязано к аккаунту <code>{target.email}</code>.\n\n💰 Баланс: <b>{_format_balance(int(target.tokens_balance or 0))}</b>"
        return True, msg


async def _do_deeplink(chat_id: str, tg_uid: str, tg_username: str,
                        full_name: str, code: str) -> None:
    """Обработка /start LINK_<code> — обмен кода на привязку."""
    from server.link_codes import redeem_code
    result = redeem_code(code, tg_uid)
    if not result:
        await send_message(chat_id,
            "⏱ Код устарел или уже использован.\n\n"
            "Сгенерируй новый на aiche.ru → Кабинет → Привязать @aiche_bot.",
            reply_markup=_back_kb())
        return
    target_user_id, kind = result
    if kind != "tg_user_id":
        await send_message(chat_id,
            "❌ Этот код не для Telegram-привязки.",
            reply_markup=_back_kb())
        return
    ok, msg = _link_tg_to_existing_user(target_user_id, tg_uid, tg_username)
    if not ok:
        await send_message(chat_id, f"❌ {msg}", reply_markup=_back_kb())
        return
    await send_message(chat_id, msg, reply_markup=_main_menu_kb())


async def _handle_callback(cb: dict) -> None:
    data = (cb.get("data") or "").strip()
    cb_id = cb.get("id", "")
    from_user = cb.get("from") or {}
    tg_uid = str(from_user.get("id", ""))
    tg_username = from_user.get("username", "")
    full_name = ((from_user.get("first_name") or "") + " " +
                  (from_user.get("last_name") or "")).strip()
    msg = cb.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    msg_id = msg.get("message_id")

    if not (tg_uid and chat_id and msg_id):
        await answer_callback(cb_id, "Ошибка контекста")
        return

    # Auto-identify
    user_id = _find_or_create_user(tg_uid, tg_username, full_name)

    await answer_callback(cb_id)  # убрать «часики»

    if data == "menu_main":
        balance_kop = get_user_balance_kop(user_id)
        await edit_message(chat_id, msg_id, _greet_text(balance_kop),
                            reply_markup=_main_menu_kb())
        return

    if data == "balance":
        balance_kop = get_user_balance_kop(user_id)
        await edit_message(chat_id, msg_id,
            f"💰 Баланс: <b>{_format_balance(balance_kop)}</b>\n\n"
            f"Все траты в чате/картинках/видео — отсюда.",
            reply_markup=_back_kb())
        return

    if data == "topup":
        app_url = os.getenv("APP_URL", "https://aiche.ru").rstrip("/")
        # Прямой deeplink на страницу пополнения сайта. После оплаты юзер
        # вернётся в браузер; чтобы вернуть его в бота, добавляем return-link.
        link = f"{app_url}/index.html#topup"
        await edit_message(chat_id, msg_id,
            "💳 <b>Пополнение баланса</b>\n\n"
            f"Открой ссылку на сайте: <a href=\"{link}\">{link}</a>\n\n"
            "Залогинься (если ещё нет) — Telegram-аккаунт привяжется автоматически "
            "по твоему id. После оплаты через ЮKassa баланс обновится в течение "
            "нескольких секунд.",
            reply_markup=_back_kb())
        return

    # Stage 2: чат с AI
    if data == "menu_chat":
        _clear_state(tg_uid)  # выходим из старого режима если был
        await edit_message(chat_id, msg_id,
            "🤖 <b>Выбери модель для чата</b>\n\n"
            "<i>После выбора отправь сообщение — модель ответит с учётом цены.</i>",
            reply_markup=_chat_models_kb())
        return

    if data.startswith("chat:"):
        model_id = data.split(":", 1)[1]
        if model_id not in {m for m, _ in CHAT_MODELS}:
            await edit_message(chat_id, msg_id, "Неизвестная модель.",
                                reply_markup=_chat_models_kb())
            return
        _set_state(tg_uid, mode="chat", model=model_id)
        label = _get_model_label(model_id)
        await edit_message(chat_id, msg_id,
            f"✏ <b>Чат с {label}</b>\n\n"
            "Отправь сообщение в этот чат — я перешлю модели и пришлю ответ.\n\n"
            "💡 Списание происходит по фактической стоимости запроса × 3 "
            "(минимум 1 ₽ за сообщение).\n"
            "Чтобы сменить модель или выйти — /menu.",
            reply_markup=_chat_active_kb())
        return

    # Stage 3a: GPT-image
    if data == "menu_image":
        _clear_state(tg_uid)
        _set_state(tg_uid, mode="image", model="gpt-image")
        await edit_message(chat_id, msg_id,
            "🎨 <b>Генерация картинки (GPT-image)</b>\n\n"
            "Отправь текст-описание (на русском или английском).\n"
            "Пример: <i>«Лиса на закате в стиле акварели»</i>\n\n"
            "💸 Цена: 60 ₽ за картинку.\n"
            "Можешь продолжить присылать промпты — каждый = новая картинка.\n"
            "Выход — /menu.",
            reply_markup=_chat_active_kb())
        return

    # Stage 3b: Kling video — submenu (2 модели)
    if data == "menu_video":
        _clear_state(tg_uid)
        await edit_message(chat_id, msg_id,
            "🎬 <b>Видео (Kling AI)</b>\n\n"
            "Выбери модель — Kling v1 быстрее и дешевле, Pro качественнее.\n"
            "<i>Видео делается 2-5 минут. Я пришлю как только готово.</i>",
            reply_markup={"inline_keyboard": [
                [{"text": "🎬 Kling v1 (50 ₽)", "callback_data": "video:kling"}],
                [{"text": "🎬 Kling Pro v1.6 (80 ₽)", "callback_data": "video:kling-pro"}],
                [{"text": "← Назад", "callback_data": "menu_main"}],
            ]})
        return

    if data.startswith("video:"):
        model_id = data.split(":", 1)[1]
        if model_id not in ("kling", "kling-pro"):
            await edit_message(chat_id, msg_id, "Неизвестная модель видео.",
                                reply_markup=_back_kb())
            return
        _set_state(tg_uid, mode="video", model=model_id)
        await edit_message(chat_id, msg_id,
            f"🎬 <b>Видео через {model_id}</b>\n\n"
            "Опиши что должно быть в видео. 5-секундный ролик 16:9.\n"
            "Пример: <i>«Кот гуляет по крыше под луной, плавная панорама»</i>\n\n"
            "💸 Цена: " + ("50" if model_id == "kling" else "80") + " ₽ за видео.\n"
            "После старта подожди 2-5 минут — пришлю готовое.\n"
            "Выход — /menu.",
            reply_markup=_chat_active_kb())
        return

    # Заглушки для оставшихся стадий
    if data == "menu_settings":
        await edit_message(chat_id, msg_id,
            "<b>⚙ Настройки</b>\n\n"
            "🚧 В разработке. Скоро будет доступно.",
            reply_markup=_back_kb())
        return

    # Unknown callback
    await edit_message(chat_id, msg_id,
        "Неизвестная команда. Возвращаю в меню.",
        reply_markup=_main_menu_kb())


# ── Webhook utilities ─────────────────────────────────────────────────────


async def _do_chat_message(chat_id: str, user_id: int, tg_uid: str,
                            user_text: str, model_id: str) -> None:
    """Юзер в режиме чата прислал сообщение → вызываем LLM, списываем, отвечаем.

    Алгоритм:
      1. Pre-check баланса (≥ 100 коп, защита от absolute zero)
      2. Indicate «typing...» в TG
      3. generate_response(model, [{role:user, content:text}])
      4. Compute cost via calc_agent_cost_kop (real_cost × 3, min 100 коп)
      5. deduct_strict — если не хватило (race), refund-like flow (Transaction
         не пишется, но ответ всё равно отдаём, потому что мы за него заплатили)
      6. Иначе debit + Transaction, отдаём response

    Состояние юзера НЕ сбрасывается — следующее сообщение продолжит чат
    с той же моделью.
    """
    from server.billing import deduct_strict, get_balance
    from server.db import db_session
    from server.models import Transaction
    from server.pricing import calc_agent_cost_kop
    from server.ai import generate_response

    # 1. Pre-check
    current_balance = get_user_balance_kop(user_id)
    if current_balance < 100:  # < 1 ₽
        await send_message(chat_id,
            f"⚠ <b>Недостаточно средств</b>\n\n"
            f"Баланс: {_format_balance(current_balance)}\n"
            f"Для AI-запроса нужно минимум 1.00 ₽.\n\n"
            f"💳 Нажми кнопку для пополнения.",
            reply_markup=_main_menu_kb())
        return

    # 2. Typing indicator
    await send_chat_action(chat_id, "typing")

    # 3. LLM call
    messages = [{"role": "user", "content": user_text}]
    try:
        result = generate_response(
            model_id, messages,
            extra={"_user_id": user_id, "_purpose": "aiche_bot_chat",
                   "max_tokens": 2000, "temperature": 0.7},
        )
    except Exception as e:
        log.error(f"[aiche-tg] LLM call failed: {type(e).__name__}: {e}")
        await send_message(chat_id,
            "⚠ Сервис временно недоступен. Попробуй через минуту.",
            reply_markup=_chat_active_kb())
        return

    content = ""
    if isinstance(result, dict):
        content = result.get("content", "") or ""
    if not content or content.startswith("Сервис временно недоступен"):
        await send_message(chat_id,
            content or "⚠ Пустой ответ от модели.",
            reply_markup=_chat_active_kb())
        return  # без списания — модель не сработала

    # 4. Cost
    usage = result.get("usage", {}) if isinstance(result, dict) else {}
    in_tok = int(usage.get("input_tokens", 0) or 0)
    out_tok = int(usage.get("output_tokens", 0) or 0)
    try:
        cost_kop = calc_agent_cost_kop(model_id, in_tok, out_tok,
                                         base_min_kop=100)
    except Exception:
        cost_kop = 100  # safe-default 1 ₽

    # 5+6. Debit + Transaction
    with db_session() as db:
        ok = deduct_strict(db, user_id, cost_kop)
        if ok:
            tx = Transaction(
                user_id=user_id, type="usage",
                tokens_delta=-cost_kop,
                description=f"[aiche_bot] chat:{model_id}",
                model=model_id,
            )
            db.add(tx)
            db.commit()
            new_balance = get_balance(db, user_id)
        else:
            new_balance = get_balance(db, user_id)

    # 7. Send response
    cost_rub = cost_kop / 100.0
    bal_rub = new_balance / 100.0
    footer = (f"\n\n— — —\n"
              f"💸 −{cost_rub:.2f} ₽ · 💰 баланс {bal_rub:.2f} ₽ "
              f"· модель: {_get_model_label(model_id).split(' ', 1)[1].split('(')[0].strip()}")
    # Если ответ длинный — режем чтобы войти в 4096 char limit TG с запасом
    body = content[:3700]
    if len(content) > 3700:
        body += "\n…[ответ обрезан]"
    await send_message(chat_id, body + footer,
                        reply_markup=_chat_active_kb())


# ── Stage 3a: GPT-image ──────────────────────────────────────────────────


async def _do_image_message(chat_id: str, user_id: int,
                              prompt_text: str, model_id: str) -> None:
    """Юзер в режиме картинки прислал промпт. Генерируем + списываем."""
    from server.billing import deduct_strict, get_balance
    from server.db import db_session
    from server.models import Transaction
    from server.ai import generate_response

    # Фикс-цена gpt-image-1 = 60 ₽ (см. server.ai TOKEN_COST).
    # Списание после успешной генерации (юзер не платит за упавшие запросы).
    IMAGE_COST_KOP = 6000

    # Pre-check
    current = get_user_balance_kop(user_id)
    if current < IMAGE_COST_KOP:
        await send_message(chat_id,
            f"⚠ <b>Недостаточно средств</b>\n\n"
            f"Баланс: {_format_balance(current)}\n"
            f"Картинка стоит {IMAGE_COST_KOP/100:.0f} ₽.\n\n"
            f"💳 Пополни через главное меню.",
            reply_markup=_main_menu_kb())
        return

    await send_chat_action(chat_id, "upload_photo")
    await send_message(chat_id,
        "🎨 Генерирую картинку, обычно ~10-20 секунд…")

    try:
        result = generate_response(
            model_id, [{"role": "user", "content": prompt_text}],
            extra={"_user_id": user_id, "_purpose": "aiche_bot_image",
                   "size": "1024x1024", "quality": "high"},
        )
    except Exception as e:
        log.error(f"[aiche-tg] image gen failed: {type(e).__name__}: {e}")
        await send_message(chat_id,
            "⚠ Сервис временно недоступен. Не списал, попробуй позже.",
            reply_markup=_chat_active_kb())
        return

    if not isinstance(result, dict):
        await send_message(chat_id, "⚠ Не удалось получить картинку.",
                            reply_markup=_chat_active_kb())
        return

    img_url = result.get("url") or result.get("content") or ""
    if not img_url or img_url.startswith("Сервис временно") or not img_url.startswith("/"):
        # Текстовая ошибка от провайдера → не списываем
        await send_message(chat_id, str(img_url or "Пустой ответ"),
                            reply_markup=_chat_active_kb())
        return

    # Списание
    with db_session() as db:
        ok = deduct_strict(db, user_id, IMAGE_COST_KOP)
        if not ok:
            new_bal = get_balance(db, user_id)
        else:
            db.add(Transaction(
                user_id=user_id, type="usage",
                tokens_delta=-IMAGE_COST_KOP,
                description=f"[aiche_bot] image:{model_id}",
                model=model_id,
            ))
            db.commit()
            new_bal = get_balance(db, user_id)

    # Публичная ссылка на /uploads/* через nginx
    app_url = os.getenv("APP_URL", "https://aiche.ru").rstrip("/")
    public_url = f"{app_url}{img_url}"
    caption = (f"🎨 Готово\n— − {IMAGE_COST_KOP/100:.2f} ₽ · "
                f"💰 баланс {new_bal/100:.2f} ₽")
    await send_photo(chat_id, public_url, caption=caption,
                       reply_markup=_chat_active_kb())


# ── Stage 3b: Kling video ────────────────────────────────────────────────


# Активные задачи Kling: task_id → {user_id, chat_id, model, started_at,
# cost_kop, attempts}. Polled background task'ом, удаляется при success/fail.
_KLING_TASKS: dict[str, dict] = {}
_KLING_POLL_INTERVAL = 30  # сек
_KLING_MAX_ATTEMPTS = 20    # = 10 минут общий таймаут


KLING_COST_KOP = {"kling": 5000, "kling-pro": 8000}


async def _do_video_message(chat_id: str, user_id: int,
                              prompt_text: str, model_id: str) -> None:
    """Submit Kling task → юзеру: «генерируется» → background poller дошлёт видео."""
    from server.ai import generate_response

    cost_kop = KLING_COST_KOP.get(model_id, 5000)
    current = get_user_balance_kop(user_id)
    if current < cost_kop:
        await send_message(chat_id,
            f"⚠ <b>Недостаточно средств</b>\n\n"
            f"Баланс: {_format_balance(current)}\n"
            f"Видео ({model_id}) стоит {cost_kop/100:.0f} ₽.",
            reply_markup=_main_menu_kb())
        return

    await send_chat_action(chat_id, "upload_video")
    await send_message(chat_id,
        "🎬 Принял. Видео генерируется 2-5 минут, я пришлю как только готово.")

    try:
        result = generate_response(
            model_id, [{"role": "user", "content": prompt_text}],
            extra={"_user_id": user_id, "_purpose": "aiche_bot_video",
                   "prompt": prompt_text, "aspect_ratio": "16:9",
                   "duration": 5, "mode": "std",
                   "generation_mode": "text2video"},
        )
    except Exception as e:
        log.error(f"[aiche-tg] kling submit failed: {type(e).__name__}: {e}")
        await send_message(chat_id,
            "⚠ Kling временно недоступен. Не списал, попробуй позже.",
            reply_markup=_chat_active_kb())
        return

    if not isinstance(result, dict) or result.get("type") != "video_task":
        # Текстовая ошибка
        text = (result.get("content") if isinstance(result, dict)
                else str(result))
        await send_message(chat_id, text or "⚠ Не удалось запустить задачу.",
                            reply_markup=_chat_active_kb())
        return

    task_id = result.get("task_id")
    if not task_id:
        await send_message(chat_id, "⚠ Kling не вернул task_id.",
                            reply_markup=_chat_active_kb())
        return

    # Регистрируем в poller'е
    _KLING_TASKS[task_id] = {
        "user_id": user_id, "chat_id": chat_id, "model": model_id,
        "started_at": time.time(), "cost_kop": cost_kop, "attempts": 0,
    }
    # asyncio.create_task — fire-and-forget, мы вернём управление сразу
    asyncio.create_task(_poll_kling_task(task_id))


async def _poll_kling_task(task_id: str) -> None:
    """Background poller. Тикает каждые 30 сек, пока видео не готово или
    не превышен лимит попыток. Когда готово — sendVideo + списание."""
    from server.ai import _get_kling_jwt
    from server.billing import deduct_strict, get_balance
    from server.db import db_session
    from server.models import Transaction

    while True:
        info = _KLING_TASKS.get(task_id)
        if not info:
            return  # удалили извне

        info["attempts"] = info.get("attempts", 0) + 1
        if info["attempts"] > _KLING_MAX_ATTEMPTS:
            await send_message(info["chat_id"],
                "⏱ Видео не удалось получить за 10 минут. "
                "Не списал. Попробуй ещё раз.",
                reply_markup=_chat_active_kb())
            _KLING_TASKS.pop(task_id, None)
            return

        await asyncio.sleep(_KLING_POLL_INTERVAL)
        token = _get_kling_jwt()
        if not token:
            log.warning(f"[aiche-tg] kling JWT failed for task {task_id}")
            continue

        try:
            async with httpx.AsyncClient(timeout=20) as cli:
                r = await cli.get(
                    f"https://api.klingai.com/v1/videos/text2video/{task_id}",
                    headers={"Authorization": f"Bearer {token}"})
                data = r.json() if r.status_code == 200 else {}
        except Exception as e:
            log.warning(f"[aiche-tg] kling poll {task_id} err: {type(e).__name__}")
            continue

        # Парсим ответ Kling: data.data.task_status (succeed/failed/processing)
        task_data = data.get("data") if isinstance(data, dict) else None
        if not isinstance(task_data, dict):
            continue
        status = (task_data.get("task_status") or "").lower()

        if status in ("failed", "error"):
            await send_message(info["chat_id"],
                f"⚠ Kling не смог сгенерировать видео: "
                f"{task_data.get('task_status_msg', 'unknown')}\n"
                "Не списал, попробуй другой промпт.",
                reply_markup=_chat_active_kb())
            _KLING_TASKS.pop(task_id, None)
            return

        if status in ("succeed", "success", "completed"):
            # video URL в data.data.task_result.videos[0].url
            video_url = ""
            try:
                videos = (task_data.get("task_result") or {}).get("videos") or []
                if videos:
                    video_url = videos[0].get("url") or ""
            except Exception:
                pass
            if not video_url:
                await send_message(info["chat_id"],
                    "⚠ Видео готово, но Kling не вернул URL.",
                    reply_markup=_chat_active_kb())
                _KLING_TASKS.pop(task_id, None)
                return

            # Списание
            with db_session() as db:
                ok = deduct_strict(db, info["user_id"], info["cost_kop"])
                if ok:
                    db.add(Transaction(
                        user_id=info["user_id"], type="usage",
                        tokens_delta=-info["cost_kop"],
                        description=f"[aiche_bot] video:{info['model']}",
                        model=info["model"],
                    ))
                    db.commit()
                new_bal = get_balance(db, info["user_id"])

            caption = (f"🎬 Готово\n— − {info['cost_kop']/100:.2f} ₽ · "
                        f"💰 баланс {new_bal/100:.2f} ₽")
            await send_video(info["chat_id"], video_url, caption=caption,
                              reply_markup=_chat_active_kb())
            _KLING_TASKS.pop(task_id, None)
            return
        # status processing → крутимся дальше


async def setup_webhook(public_url: str) -> dict:
    """Зарегистрировать webhook у Telegram. Вызывается из admin endpoint
    или при старте сервера если AICHE_TG_BOT_AUTOSET_WEBHOOK=1.

    public_url — наш публичный URL включая secret, например:
        https://aiche.ru/webhook/aiche-tg/<secret>
    """
    if not is_configured():
        return {"ok": False, "error": "AICHE_TG_BOT_TOKEN/SECRET не заданы"}
    result = await _tg_call("setWebhook", {
        "url": public_url,
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": False,
    })
    return result or {"ok": False, "error": "no response from telegram"}


async def get_webhook_info() -> dict:
    if not is_configured():
        return {"ok": False, "error": "not configured"}
    return (await _tg_call("getWebhookInfo", {})) or {"ok": False}
