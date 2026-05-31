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


def send_personal_tg_sync(bot_token: str, chat_id: str, text: str,
                           reply_markup: dict | None = None,
                           parse_mode: str = "HTML") -> bool:
    """Sync send через PERSONAL бот юзера (его собственный @BotFather токен).

    Используется в notify_user для модели «юзер подключает свой бот»
    (см. server/personal_bot_relay.py). Отличается от send_message_sync
    тем что bot_token передаётся явно, а не берётся из env TG_MGMT_BOT_TOKEN.
    """
    if not bot_token or not chat_id:
        return False
    payload = {
        "chat_id": str(chat_id),
        "text": text[:4000],
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if reply_markup:
        import json as _json
        payload["reply_markup"] = _json.dumps(reply_markup, ensure_ascii=False)
    url = f"{TG_API_BASE}{bot_token.strip()}/sendMessage"
    try:
        with httpx.Client(timeout=12) as client:
            r = client.post(url, json=payload)
            if r.status_code != 200:
                log.warning(f"[personal-tg-sync] {chat_id}: {r.status_code} {r.text[:200]}")
                return False
        return True
    except Exception as e:
        log.warning(f"[personal-tg-sync] {chat_id} error: {type(e).__name__}")
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
    Возвращает user_id или None если код не валиден/истёк/rate-limit.

    Защита от brute-force: двухуровневая sliding-window через
    server.security.link_code_attempt_check — 5 попыток/мин + 30 попыток/час
    на tg_user_id. Раньше было только 10/10мин — этого мало против медленной
    атаки с распределённого ботнета (TG-боты могут rotate user_id).

    Также шлём email-alert владельцу аккаунта при успешном link/relink —
    иначе угнавший аккаунт код атакер привяжет свой TG тихо.
    """
    from server.models import User
    from server.security import link_code_attempt_check
    code = (code or "").strip().upper()
    if not code or len(code) != LINK_CODE_LEN:
        return None
    # Прогрессивная защита от brute-force 6-знач кода
    # (~28 бит энтропии + 10 мин TTL = brute-force-able без лимита).
    allowed, _reason = link_code_attempt_check("tg", tg_user_id)
    if not allowed:
        log.warning(f"[tg-mgmt] consume_link_code rate-limit hit for tg_user_id={tg_user_id}")
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
    is_relink = bool(u.tg_user_id and u.tg_user_id != str(tg_user_id))
    u.tg_user_id = str(tg_user_id)
    u.tg_username = (tg_username or "")[:100] or None
    u.tg_link_code = None
    u.tg_link_expires = None
    db.commit()
    # Email-уведомление об успешной привязке (важная security-операция:
    # TG-бот после этого может управлять подписками и видеть push'и).
    # tg_username — HTML-escape перед вставкой в email-body. TG username
    # constrained to [A-Za-z0-9_], но defense-in-depth не лишнее (никогда
    # не знаешь когда TG расширит формат).
    from html import escape as _html_escape
    try:
        from server.email_service import _send, _base_template
        verb = "перепривязан" if is_relink else "привязан"
        safe_username = _html_escape((tg_username or "—")[:40])
        body = (
            f'<p style="color:rgba(199,196,215,0.8);line-height:1.6">'
            f'К вашему аккаунту {verb} Telegram-бот управления.<br/>'
            f'TG-юзер: <b>@{safe_username}</b><br/>'
            f'Время: {datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}</p>'
            f'<p style="color:rgba(199,196,215,0.7);font-size:13px">'
            f'Если это были не вы — отвяжите в кабинете → Настройки → '
            f'«🤖 Telegram-бот» и смените пароль.</p>'
        )
        _send(u.email, "🔔 Telegram-бот привязан — AI Студия Че",
              _base_template("Telegram-бот привязан", body))
    except Exception as e:
        log.warning(f"[tg-mgmt] link-alert email failed: {type(e).__name__}: {e}")
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
    # Команда не распознана (начинается со /)?
    if text.startswith("/"):
        await send_message(chat_id,
            "Не понял команду. Доступные: /menu, /me, /stats, /link, /unlink.\n"
            "Если ещё не привязан — открой /start.\n\n"
            "А если хочешь поговорить с Че (AI-помощником) — пиши без слэша.")
        return
    # ── Обычное сообщение → relay к Че ──────────────────────────────────────
    # Если юзер не привязан — просим привязать через /start или /link.
    # Если привязан — пропускаем текст через ту же логику что web-чат:
    # build_reply_personal + опционально invoke_module → ответ обратно в TG.
    await _do_relay_to_che(chat_id, tg_uid, text)


async def _do_link(chat_id: str, tg_uid: str, tg_username: str, code: str) -> None:
    # Прогрессивная защита от brute-force через link_code_attempt_check
    # (двухуровневая sliding-window: 5/мин + 30/час). Дублируем то же что
    # делает consume_link_code, чтобы дать юзеру понятную причину отказа
    # (не silent None как в БД-функции).
    from server.security import link_code_attempt_check
    allowed, reason = link_code_attempt_check("tg", tg_uid)
    if not allowed:
        await send_message(chat_id, f"🛑 {reason}")
        return
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


# ── Relay входящего сообщения к Che (singleton-агент модуля 23) ───────────


async def _send_typing(chat_id: str) -> None:
    """Показать «typing...» индикатор в TG. Авто-исчезает через 5 сек или
    при следующем sendMessage. Делаем перед длинной операцией build_reply."""
    token = _bot_token()
    if not token:
        return
    url = f"{TG_API_BASE}{token}/sendChatAction"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(url, json={"chat_id": str(chat_id),
                                          "action": "typing"})
    except Exception:
        pass


async def _do_relay_to_che(chat_id: str, tg_uid: str, text: str) -> None:
    """Любое сообщение от привязанного юзера → Че.

    Юзер пишет в management-бот «напиши пост про X» — мы дёргаем ту же
    логику что и web-чат /api/agents/me/messages: build_reply_personal +
    invoke_module если нужно, ответ обратно в TG.

    Если юзер НЕ привязан — отправляем подсказку как привязаться.
    """
    from server.db import db_session
    from server.models import User
    from server.tg_che_relay import process_message, format_for_tg
    from server.security import _check as _rl_check

    # Rate-limit: 20 сообщ/мин, 200/час — те же что в web (защита от спама)
    if not _rl_check(f"tg-che:{tg_uid}", max_calls=20, window_sec=60):
        await send_message(chat_id, "🛑 Слишком много сообщений. Подожди минуту.")
        return
    if not _rl_check(f"tg-che-h:{tg_uid}", max_calls=200, window_sec=3600):
        await send_message(chat_id, "🛑 Слишком много сообщений за час. Подожди.")
        return

    # Привязка
    with db_session() as db:
        u = db.query(User).filter_by(tg_user_id=tg_uid).first()
        if not u:
            await send_message(chat_id,
                "👋 Чтобы общаться с Че через Telegram, привяжи аккаунт:\n\n"
                "1. Открой aiche.ru → Кабинет → 📲 Telegram\n"
                "2. Скопируй код привязки\n"
                "3. Пришли его сюда командой <code>/link XXXXXX</code>\n\n"
                "После привязки Че будет помнить тебя как на сайте.")
            return
        user_id = u.id
        agent_name = "Че"  # будет получено из process_message

    # Typing-индикатор (юзер видит что бот «печатает», build_reply может занять 5-15 сек)
    await _send_typing(chat_id)

    # Сам relay — синхронная функция (БД-операции), запускаем в thread.
    import asyncio
    loop = asyncio.get_event_loop()

    def _do_in_thread():
        with db_session() as db:
            u = db.query(User).filter_by(id=user_id).first()
            if not u:
                return {"reply": "Аккаунт не найден.", "error": "no_user"}
            return process_message(db, u, text)

    try:
        result = await loop.run_in_executor(None, _do_in_thread)
    except Exception as e:
        log.exception(f"[tg-mgmt] relay to Che failed: {e}")
        await send_message(chat_id,
            "😔 Что-то пошло не так при обработке. Попробуй ещё раз через минуту.")
        return

    # Возможные ошибки от relay
    if result.get("error") == "insufficient_funds":
        await send_message(chat_id, result.get("reply", "Недостаточно средств."))
        return

    # Форматируем 1-2 сообщения и шлём в TG
    parts = format_for_tg(result, agent_name=agent_name)
    for i, part in enumerate(parts):
        ok = await send_message(chat_id, part)
        if not ok:
            log.warning(f"[tg-mgmt] failed to send part {i} to chat={chat_id}")


# ── Хелперы для отправки push-уведомлений (вызываются из chatbot_engine etc) ──


def notify_user(user_id: int, text: str, kind: str = "info",
                 reply_markup: dict | None = None) -> bool:
    """Отправить push-уведомление юзеру в его привязанный TG.

    Приоритет канала (2026-05 миграция «общий бот» → «свой бот»):
      1. personal_tg_bot_token + personal_tg_chat_id — современный flow,
         юзер подключил свой @BotFather бот в /agents-modular.html
      2. Fallback: legacy tg_user_id + global TG_MGMT_BOT_TOKEN — для
         юзеров кто ещё не мигрировал на свой бот

    kind: 'proposals' | 'records' | 'errors' | 'info' — соответствует
    toggle-флагам (tg_notify_*). 'info' — без toggle-проверки (системное).
    Возвращает True если сообщение ушло хоть одним способом.
    """
    from server.db import db_session
    from server.models import User
    field = {"proposals":"tg_notify_proposals", "records":"tg_notify_records",
             "errors":"tg_notify_errors", "info":None}.get(kind)
    with db_session() as db:
        u = db.query(User).filter_by(id=user_id).first()
        if not u:
            return False
        if field and not getattr(u, field, True):
            return False  # юзер выключил этот тип уведомления
        # Снимаем значения здесь — после выхода из with объект отвяжется от сессии
        personal_token = u.personal_tg_bot_token
        personal_chat = u.personal_tg_chat_id
        legacy_uid = u.tg_user_id

    # Современный канал: personal bot
    if personal_token and personal_chat:
        if send_personal_tg_sync(personal_token, personal_chat, text,
                                  reply_markup=reply_markup):
            return True
        # Personal bot не доставил (заблокирован/удалён юзером) →
        # пробуем legacy если есть. Иначе считаем что юзер недоступен.

    # Legacy fallback: общий бот платформы
    if legacy_uid:
        return send_message_sync(legacy_uid, text, reply_markup=reply_markup)
    return False
