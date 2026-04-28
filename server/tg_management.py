"""
Telegram-бот управления АГЕНТАМИ И ПЛАТФОРМОЙ для владельцев.

Это НЕ клиентский бот (не тот что отвечает покупателям через workflow),
а ОТДЕЛЬНЫЙ бот через который владелец управляет своим аккаунтом
платформы прямо из Telegram:

  • Привязка по одноразовому коду (выдаём в /index.html → кабинет → Telegram)
  • Получение push'ей: новый КП, новая заявка от чат-бота, ошибки/refund'ы
  • Inline-меню: статус агентов, баланс, последние события, статистика
  • Быстрые действия: отметить КП как «выигран», ответить на заявку

Регистрация: создать бот через @BotFather → токен в env TG_MGMT_BOT_TOKEN.
Webhook: POST /webhook/tg-mgmt/{secret} (см. server/routes/webhook.py).
"""
import os
import re
import secrets as _secrets
import logging
from datetime import datetime, timedelta

import httpx

log = logging.getLogger("tg-mgmt")

TG_API_BASE = "https://api.telegram.org/bot"
LINK_CODE_TTL_MINUTES = 10
LINK_CODE_LEN = 6


def _bot_token() -> str | None:
    """Токен management-бота из env. None если не сконфигурирован."""
    t = os.getenv("TG_MGMT_BOT_TOKEN", "").strip()
    return t or None


def is_configured() -> bool:
    return bool(_bot_token())


async def send_message(tg_user_id: str, text: str,
                        reply_markup: dict | None = None,
                        parse_mode: str = "HTML") -> bool:
    """Отправить сообщение юзеру через management-бота.
    Возвращает True если успех. Не кидает exception — логирует и идёт дальше."""
    token = _bot_token()
    if not token or not tg_user_id:
        return False
    payload = {
        "chat_id": str(tg_user_id),
        "text": text[:4000],
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if reply_markup:
        import json as _json
        payload["reply_markup"] = _json.dumps(reply_markup, ensure_ascii=False)
    url = f"{TG_API_BASE}{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.post(url, json=payload)
            if r.status_code != 200:
                log.warning(f"[tg-mgmt] send to {tg_user_id} failed: {r.status_code} {r.text[:200]}")
                return False
        return True
    except Exception as e:
        log.warning(f"[tg-mgmt] send to {tg_user_id} error: {type(e).__name__}")
        return False


def send_message_sync(tg_user_id: str, text: str,
                       reply_markup: dict | None = None,
                       parse_mode: str = "HTML") -> bool:
    """Sync версия send_message (для вызова из не-async кода — например
    из chatbot_engine.auto_proposal node, scheduler и т.д.)."""
    token = _bot_token()
    if not token or not tg_user_id:
        return False
    payload = {
        "chat_id": str(tg_user_id),
        "text": text[:4000],
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if reply_markup:
        import json as _json
        payload["reply_markup"] = _json.dumps(reply_markup, ensure_ascii=False)
    url = f"{TG_API_BASE}{token}/sendMessage"
    try:
        with httpx.Client(timeout=12) as client:
            r = client.post(url, json=payload)
            if r.status_code != 200:
                log.warning(f"[tg-mgmt] send_sync {tg_user_id}: {r.status_code} {r.text[:200]}")
                return False
        return True
    except Exception as e:
        log.warning(f"[tg-mgmt] send_sync {tg_user_id} error: {type(e).__name__}")
        return False


async def answer_callback(callback_query_id: str, text: str = "",
                           show_alert: bool = False) -> None:
    """Ответ на нажатие inline-кнопки (убирает «часики» в Telegram)."""
    token = _bot_token()
    if not token:
        return
    url = f"{TG_API_BASE}{token}/answerCallbackQuery"
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            await client.post(url, json={
                "callback_query_id": callback_query_id,
                "text": text[:200], "show_alert": show_alert,
            })
    except Exception:
        pass


# ── Привязка через код ────────────────────────────────────────────────────


def generate_link_code(db, user_id: int) -> str:
    """Сгенерировать одноразовый 6-значный код для привязки TG к юзеру.
    Юзер видит код в /кабинет → Telegram, отправляет его в management-бот
    командой /link <CODE>.
    Код действует 10 минут, после привязки — сбрасывается."""
    from server.models import User
    code = "".join(_secrets.choice("ACDEFGHJKLMNPQRTUVWXYZ234679") for _ in range(LINK_CODE_LEN))
    u = db.query(User).filter_by(id=user_id).first()
    if not u:
        raise ValueError("User not found")
    u.tg_link_code = code
    u.tg_link_expires = datetime.utcnow() + timedelta(minutes=LINK_CODE_TTL_MINUTES)
    db.commit()
    return code


def consume_link_code(db, code: str, tg_user_id: str, tg_username: str | None) -> int | None:
    """Применить код: найти юзера, привязать tg_user_id, сбросить код.
    Возвращает user_id или None если код не валиден/истёк."""
    from server.models import User
    code = (code or "").strip().upper()
    if not code or len(code) != LINK_CODE_LEN:
        return None
    u = db.query(User).filter_by(tg_link_code=code).first()
    if not u:
        return None
    if not u.tg_link_expires or u.tg_link_expires < datetime.utcnow():
        return None
    # Если у юзера уже привязан другой TG — заменяем.
    # Если этот tg_user_id уже привязан к другому юзеру — отвязываем.
    other = db.query(User).filter(User.tg_user_id == str(tg_user_id),
                                    User.id != u.id).first()
    if other:
        other.tg_user_id = None
        other.tg_username = None
    u.tg_user_id = str(tg_user_id)
    u.tg_username = (tg_username or "")[:100] or None
    u.tg_link_code = None
    u.tg_link_expires = None
    db.commit()
    return u.id


def unlink(db, user_id: int) -> bool:
    """Отвязать TG от юзера."""
    from server.models import User
    u = db.query(User).filter_by(id=user_id).first()
    if not u:
        return False
    u.tg_user_id = None
    u.tg_username = None
    u.tg_link_code = None
    u.tg_link_expires = None
    db.commit()
    return True


# ── Webhook handler: команды бота ──────────────────────────────────────────


def _kop_to_rub(kop: int) -> str:
    return f"{(kop or 0) / 100:.0f} ₽"


async def handle_update(update: dict) -> None:
    """Обработка одного update от Telegram. Команды:
       /start — приветствие + инструкция
       /link <CODE> — привязка к аккаунту платформы
       /unlink — отвязать
       /me — мой профиль/баланс
       /stats — статистика последних 7 дней
       /menu — главное inline-меню
    Также callback_query для inline-кнопок."""
    if not isinstance(update, dict):
        return
    # Callback query (нажатие inline-кнопки)
    if "callback_query" in update:
        await _handle_callback(update["callback_query"])
        return
    msg = update.get("message")
    if not msg:
        return
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    if not chat_id:
        return
    text = (msg.get("text") or "").strip()
    from_user = msg.get("from") or {}
    tg_uid = str(from_user.get("id", ""))
    tg_username = from_user.get("username", "")

    if text.startswith("/start"):
        # /start LINK_<code> — deep-link с кодом для автопривязки
        m = re.match(r"^/start(?:\s+(.+))?$", text)
        arg = (m.group(1) if m else "").strip()
        if arg.upper().startswith("LINK_"):
            code = arg[5:].strip().upper()
            await _do_link(chat_id, tg_uid, tg_username, code)
            return
        await send_message(chat_id,
            "👋 <b>AI Студия Че — управление</b>\n\n"
            "Этот бот связывает тебя с твоим аккаунтом на aiche.ru.\n\n"
            "Чтобы начать — открой <b>aiche.ru → Кабинет → 📲 Приложение → "
            "Привязать Telegram</b>, скопируй код и отправь его сюда командой:\n"
            "<code>/link XXXXXX</code>\n\n"
            "После привязки доступны команды:\n"
            "/menu — главное меню\n"
            "/me — баланс и профиль\n"
            "/stats — статистика за 7 дней\n"
            "/unlink — отвязать аккаунт")
        return
    if text.startswith("/link"):
        m = re.match(r"^/link\s+([A-Z0-9]+)$", text.upper())
        if not m:
            await send_message(chat_id, "Формат: <code>/link XXXXXX</code>\nКод получи в кабинете на сайте.")
            return
        await _do_link(chat_id, tg_uid, tg_username, m.group(1))
        return
    if text.startswith("/unlink"):
        await _do_unlink(chat_id, tg_uid)
        return
    if text.startswith("/me"):
        await _do_me(chat_id, tg_uid)
        return
    if text.startswith("/stats"):
        await _do_stats(chat_id, tg_uid)
        return
    if text.startswith("/menu") or text.startswith("/help"):
        await _do_menu(chat_id, tg_uid)
        return
    # Незнакомая команда
    await send_message(chat_id,
        "Не понял команду. Доступные: /menu, /me, /stats, /link, /unlink.\n"
        "Если ещё не привязан — открой /start.")


async def _do_link(chat_id: str, tg_uid: str, tg_username: str, code: str) -> None:
    from server.db import db_session
    with db_session() as db:
        user_id = consume_link_code(db, code, tg_uid, tg_username)
        if not user_id:
            await send_message(chat_id,
                "❌ Код не подходит или истёк (10 мин). Сгенерируй новый в кабинете.")
            return
        from server.models import User
        u = db.query(User).filter_by(id=user_id).first()
        email = u.email if u else "(?)"
    await send_message(chat_id,
        f"✅ Привязано! Аккаунт: <code>{email}</code>\n\n"
        f"Теперь ты будешь получать уведомления о новых КП, заявках и ошибках.\n"
        f"/menu — главное меню")


async def _do_unlink(chat_id: str, tg_uid: str) -> None:
    from server.db import db_session
    from server.models import User
    with db_session() as db:
        u = db.query(User).filter_by(tg_user_id=tg_uid).first()
        if not u:
            await send_message(chat_id, "Этот Telegram не привязан к аккаунту.")
            return
        unlink(db, u.id)
    await send_message(chat_id, "🔓 Отвязано. Уведомления больше не будут приходить.")


async def _do_me(chat_id: str, tg_uid: str) -> None:
    from server.db import db_session
    from server.models import User
    with db_session() as db:
        u = db.query(User).filter_by(tg_user_id=tg_uid).first()
        if not u:
            await send_message(chat_id, "Сначала привяжи аккаунт: /start или /link XXXXXX")
            return
        email = u.email
        balance = _kop_to_rub(u.tokens_balance or 0)
    await send_message(chat_id,
        f"👤 <b>{email}</b>\n💰 Баланс: <b>{balance}</b>\n\n/menu — действия")


async def _do_stats(chat_id: str, tg_uid: str) -> None:
    from server.db import db_session
    from server.models import User, ProposalProject, BotRecord, Transaction
    from sqlalchemy import func
    week_ago = datetime.utcnow() - timedelta(days=7)
    with db_session() as db:
        u = db.query(User).filter_by(tg_user_id=tg_uid).first()
        if not u:
            await send_message(chat_id, "Сначала /link XXXXXX")
            return
        proposals_total = db.query(ProposalProject).filter(
            ProposalProject.user_id == u.id,
            ProposalProject.created_at >= week_ago).count()
        proposals_sent = db.query(ProposalProject).filter(
            ProposalProject.user_id == u.id,
            ProposalProject.sent_at >= week_ago).count()
        proposals_won = db.query(ProposalProject).filter(
            ProposalProject.user_id == u.id,
            ProposalProject.crm_stage == "won",
            ProposalProject.won_at >= week_ago).count()
        records = db.query(BotRecord).filter(
            BotRecord.user_id == u.id,
            BotRecord.created_at >= week_ago).count()
        spent = db.query(func.coalesce(func.sum(Transaction.tokens_delta), 0)).filter(
            Transaction.user_id == u.id,
            Transaction.type == "usage",
            Transaction.created_at >= week_ago).scalar() or 0
        spent = abs(int(spent))
    await send_message(chat_id,
        f"📊 <b>За последние 7 дней</b>\n\n"
        f"📄 КП создано: <b>{proposals_total}</b>\n"
        f"📨 Отправлено клиентам: <b>{proposals_sent}</b>\n"
        f"✅ Выиграно: <b>{proposals_won}</b>\n"
        f"📥 Заявок от ботов: <b>{records}</b>\n"
        f"💸 Расход: <b>{_kop_to_rub(spent)}</b>")


async def _do_menu(chat_id: str, tg_uid: str) -> None:
    from server.db import db_session
    from server.models import User
    with db_session() as db:
        u = db.query(User).filter_by(tg_user_id=tg_uid).first()
        if not u:
            await send_message(chat_id, "Сначала /link XXXXXX")
            return
        notif_p = "🟢" if u.tg_notify_proposals else "⚪"
        notif_r = "🟢" if u.tg_notify_records else "⚪"
        notif_e = "🟢" if u.tg_notify_errors else "⚪"
    keyboard = {
        "inline_keyboard": [
            [{"text": "👤 Профиль", "callback_data": "me"},
             {"text": "📊 Статистика", "callback_data": "stats"}],
            [{"text": "📄 Последние КП", "callback_data": "recent_proposals"},
             {"text": "📥 Последние заявки", "callback_data": "recent_records"}],
            [{"text": f"{notif_p} КП-уведомления", "callback_data": "toggle:proposals"}],
            [{"text": f"{notif_r} Заявки бота", "callback_data": "toggle:records"}],
            [{"text": f"{notif_e} Ошибки/refund", "callback_data": "toggle:errors"}],
        ]
    }
    await send_message(chat_id, "🎛 <b>Главное меню</b>\nЧто открыть?",
                        reply_markup=keyboard)


async def _handle_callback(cb: dict) -> None:
    cb_id = cb.get("id")
    data = (cb.get("data") or "").strip()
    from_user = cb.get("from") or {}
    tg_uid = str(from_user.get("id", ""))
    msg = cb.get("message") or {}
    chat_id = str((msg.get("chat") or {}).get("id", ""))
    if not chat_id or not tg_uid:
        if cb_id:
            await answer_callback(cb_id)
        return

    if data == "me":
        await _do_me(chat_id, tg_uid)
    elif data == "stats":
        await _do_stats(chat_id, tg_uid)
    elif data == "recent_proposals":
        await _do_recent_proposals(chat_id, tg_uid)
    elif data == "recent_records":
        await _do_recent_records(chat_id, tg_uid)
    elif data.startswith("toggle:"):
        kind = data.split(":", 1)[1]
        await _do_toggle(chat_id, tg_uid, kind)
    elif data.startswith("proposal:"):
        # proposal:<id>:<action>  — действия с КП (won/lost/sent)
        parts = data.split(":")
        if len(parts) >= 3:
            await _do_proposal_action(chat_id, tg_uid, int(parts[1]), parts[2])
    if cb_id:
        await answer_callback(cb_id)


async def _do_recent_proposals(chat_id: str, tg_uid: str) -> None:
    from server.db import db_session
    from server.models import User, ProposalProject
    with db_session() as db:
        u = db.query(User).filter_by(tg_user_id=tg_uid).first()
        if not u:
            return
        rows = (db.query(ProposalProject)
                  .filter_by(user_id=u.id)
                  .order_by(ProposalProject.created_at.desc())
                  .limit(5).all())
    if not rows:
        await send_message(chat_id, "Пока нет КП.")
        return
    lines = ["📄 <b>Последние 5 КП:</b>", ""]
    stage_emo = {"new":"🆕", "sent":"📨", "opened":"👁", "replied":"💬", "won":"✅", "lost":"❌"}
    for p in rows:
        emo = stage_emo.get(p.crm_stage or "new", "📄")
        client = p.client_name or "(без имени)"
        # Ограничение длины имени КП
        nm = (p.name or "")[:50]
        lines.append(f"{emo} <b>{nm}</b> · {client}")
    await send_message(chat_id, "\n".join(lines))


async def _do_recent_records(chat_id: str, tg_uid: str) -> None:
    from server.db import db_session
    from server.models import User, BotRecord
    with db_session() as db:
        u = db.query(User).filter_by(tg_user_id=tg_uid).first()
        if not u:
            return
        rows = (db.query(BotRecord)
                  .filter_by(user_id=u.id)
                  .order_by(BotRecord.created_at.desc())
                  .limit(5).all())
    if not rows:
        await send_message(chat_id, "Пока нет заявок от ботов.")
        return
    lines = ["📥 <b>Последние 5 заявок:</b>", ""]
    for r in rows:
        nm = r.customer_name or "(аноним)"
        ph = r.customer_phone or ""
        em = r.customer_email or ""
        contact = ph or em or ""
        rt = r.record_type or "lead"
        lines.append(f"• {rt}: <b>{nm}</b> {contact}")
    await send_message(chat_id, "\n".join(lines))


async def _do_toggle(chat_id: str, tg_uid: str, kind: str) -> None:
    from server.db import db_session
    from server.models import User
    fields = {"proposals": "tg_notify_proposals",
              "records": "tg_notify_records",
              "errors": "tg_notify_errors"}
    if kind not in fields:
        return
    field = fields[kind]
    with db_session() as db:
        u = db.query(User).filter_by(tg_user_id=tg_uid).first()
        if not u:
            return
        cur = bool(getattr(u, field, True))
        setattr(u, field, not cur)
        db.commit()
        new_val = not cur
    state = "🟢 включены" if new_val else "⚪ отключены"
    label = {"proposals":"КП-уведомления","records":"Заявки бота","errors":"Ошибки/refund"}[kind]
    await send_message(chat_id, f"{label}: {state}")


async def _do_proposal_action(chat_id: str, tg_uid: str, project_id: int, action: str) -> None:
    from server.db import db_session
    from server.models import User, ProposalProject
    valid = {"won", "lost", "sent"}
    if action not in valid:
        return
    with db_session() as db:
        u = db.query(User).filter_by(tg_user_id=tg_uid).first()
        if not u:
            return
        p = db.query(ProposalProject).filter_by(id=project_id, user_id=u.id).first()
        if not p:
            await send_message(chat_id, "КП не найдено")
            return
        now = datetime.utcnow()
        p.crm_stage = action
        if action == "won" and not p.won_at:
            p.won_at = now
        elif action == "lost" and not p.lost_at:
            p.lost_at = now
        elif action == "sent" and not p.sent_at:
            p.sent_at = now
        db.commit()
    label = {"won":"✅ Выиграно","lost":"❌ Отказ","sent":"📨 Отправлено"}[action]
    await send_message(chat_id, f"КП #{project_id}: {label}")


# ── Хелперы для отправки push-уведомлений (вызываются из chatbot_engine etc) ──


def notify_user(user_id: int, text: str, kind: str = "info",
                 reply_markup: dict | None = None) -> bool:
    """Отправить push-уведомление юзеру в его привязанный TG.
    kind: 'proposals' | 'records' | 'errors' — соответствует toggle-флагам.
    Возвращает True если сообщение ушло."""
    from server.db import db_session
    from server.models import User
    field = {"proposals":"tg_notify_proposals", "records":"tg_notify_records",
             "errors":"tg_notify_errors", "info":None}.get(kind)
    with db_session() as db:
        u = db.query(User).filter_by(id=user_id).first()
        if not u or not u.tg_user_id:
            return False
        if field and not getattr(u, field, True):
            return False  # юзер выключил этот тип
        tg_uid = u.tg_user_id
    return send_message_sync(tg_uid, text, reply_markup=reply_markup)
