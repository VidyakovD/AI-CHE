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
    """Базовый вызов Telegram API."""
    token = _bot_token()
    if not token:
        log.warning(f"[aiche-tg] no token, skipping {method}")
        return None
    url = f"{TG_API_BASE}{token}/{method}"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
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
    """Показать «бот печатает...» в TG. Без await ответа."""
    await _tg_call("sendChatAction", {"chat_id": str(chat_id), "action": action})


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
    from server.db import db_session
    from server.models import User
    with db_session() as db:
        u = db.query(User).filter_by(tg_user_id=tg_uid).first()
        if u:
            # Обновим username если изменился
            if tg_username and u.tg_username != tg_username:
                u.tg_username = tg_username
                db.commit()
            return int(u.id)

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
            tokens_balance=0,
            referral_code=_secrets.token_hex(4).upper(),
        )
        db.add(u)
        db.commit()
        db.refresh(u)
        log.info(f"[aiche-tg] auto-created user_id={u.id} for tg_uid={tg_uid}")
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
    if state and state.get("mode") == "chat":
        user_id = _find_or_create_user(tg_uid, tg_username, full_name)
        await _do_chat_message(chat_id, user_id, tg_uid, text,
                                state.get("model") or "claude")
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

    # Заглушки для будущих стадий
    if data in ("menu_image", "menu_video", "menu_settings"):
        labels = {
            "menu_image": "🎨 Картинка",
            "menu_video": "🎬 Видео",
            "menu_settings": "⚙ Настройки",
        }
        await edit_message(chat_id, msg_id,
            f"<b>{labels[data]}</b>\n\n"
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
