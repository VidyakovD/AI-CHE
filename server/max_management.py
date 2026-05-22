"""MAX-бот управления АГЕНТАМИ И ПЛАТФОРМОЙ (симметрично tg_management.py).

MAX (max.ru) — российский мессенджер от VK. Важен для пользователей без VPN.

Это НЕ клиентский бот (не тот что отвечает покупателям через workflow в
chatbot_engine), а ОТДЕЛЬНЫЙ бот через который владелец общается со своим
Че (singleton-агентом) прямо из MAX-чата.

Бот создаётся в MAX-эквиваленте BotFather (точное имя сервиса узнаём в
панели разработчика max.ru). Токен → env MAX_MGMT_BOT_TOKEN.

Команды бота:
  /start — приветствие + инструкция привязки
  /start LINK_<code> — deep-link с кодом
  /link XXXXXX — привязка вручную
  /unlink — отвязать
  /me — мой профиль/баланс
  /help — справка

Любое сообщение НЕ начинающееся с / → relay к Че через tg_che_relay.process_message
(та же функция что используется для TG — переиспользуем код).
"""
import os
import re
import secrets as _secrets
import logging
from datetime import datetime, timedelta

import httpx

log = logging.getLogger("max-mgmt")

MAX_API_BASE = "https://botapi.max.ru"
LINK_CODE_TTL_MINUTES = 10
LINK_CODE_LEN = 6


def _bot_token() -> str | None:
    """Токен management-бота из env. None если не сконфигурирован."""
    t = os.getenv("MAX_MGMT_BOT_TOKEN", "").strip()
    return t or None


def is_configured() -> bool:
    return bool(_bot_token())


async def send_message(max_user_id: str | int, text: str) -> bool:
    """Отправить сообщение юзеру через management MAX-бота.

    MAX API: POST /messages?user_id=<>, body {text, format}. Auth — Authorization
    header с голым токеном (БЕЗ префикса 'Bearer ', проверено живьём в
    server/messaging/senders.py:send_max).
    """
    token = _bot_token()
    if not token or not max_user_id:
        return False
    url = f"{MAX_API_BASE}/messages"
    headers = {"Authorization": token, "Content-Type": "application/json"}
    params = {"user_id": str(max_user_id)}
    body = {"text": text[:4000], "format": "markdown"}
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.post(url, headers=headers, params=params, json=body)
            if r.status_code != 200:
                log.warning(f"[max-mgmt] send to {max_user_id} failed: {r.status_code} {r.text[:200]}")
                return False
        return True
    except Exception as e:
        log.warning(f"[max-mgmt] send to {max_user_id} error: {type(e).__name__}")
        return False


# ── Привязка через код ────────────────────────────────────────────────────


def generate_link_code(db, user_id: int) -> str:
    """Сгенерировать одноразовый 6-значный код для привязки MAX к юзеру."""
    from server.models import User
    code = "".join(_secrets.choice("ACDEFGHJKLMNPQRTUVWXYZ234679") for _ in range(LINK_CODE_LEN))
    u = db.query(User).filter_by(id=user_id).first()
    if not u:
        raise ValueError("User not found")
    u.max_link_code = code
    u.max_link_expires = datetime.utcnow() + timedelta(minutes=LINK_CODE_TTL_MINUTES)
    db.commit()
    return code


def consume_link_code(db, code: str, max_user_id: str,
                      max_username: str | None) -> int | None:
    """Применить код: найти юзера, привязать max_user_id, сбросить код."""
    from server.models import User
    from server.security import link_code_attempt_check
    code = (code or "").strip().upper()
    if not code or len(code) != LINK_CODE_LEN:
        return None
    # Прогрессивная защита 5/мин + 30/час (см. tg_management.consume_link_code)
    allowed, _reason = link_code_attempt_check("max", max_user_id)
    if not allowed:
        log.warning(f"[max-mgmt] consume_link_code rate-limit hit for max_user_id={max_user_id}")
        return None
    u = db.query(User).filter_by(max_link_code=code).first()
    if not u:
        return None
    if not u.max_link_expires or u.max_link_expires < datetime.utcnow():
        return None
    # Если этот max_user_id уже привязан к другому — отвязываем
    other = db.query(User).filter(User.max_user_id == str(max_user_id),
                                    User.id != u.id).first()
    if other:
        other.max_user_id = None
        other.max_username = None
    is_relink = bool(u.max_user_id and u.max_user_id != str(max_user_id))
    u.max_user_id = str(max_user_id)
    u.max_username = (max_username or "")[:100] or None
    u.max_link_code = None
    u.max_link_expires = None
    db.commit()
    # Email-уведомление о привязке (security). MAX username NOT constrained
    # как TG ([A-Za-z0-9_]) — обязательно HTML-escape, иначе injection в email.
    from html import escape as _html_escape
    try:
        from server.email_service import _send, _base_template
        verb = "перепривязан" if is_relink else "привязан"
        safe_username = _html_escape((max_username or "—")[:40])
        body = (
            f'<p style="color:rgba(199,196,215,0.8);line-height:1.6">'
            f'К вашему аккаунту {verb} MAX-бот управления.<br/>'
            f'MAX-юзер: <b>@{safe_username}</b><br/>'
            f'Время: {datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}</p>'
            f'<p style="color:rgba(199,196,215,0.7);font-size:13px">'
            f'Если это были не вы — отвяжите в кабинете и смените пароль.</p>'
        )
        _send(u.email, "🔔 MAX-бот привязан — AI Студия Че",
              _base_template("MAX-бот привязан", body))
    except Exception as e:
        log.warning(f"[max-mgmt] link-alert email failed: {type(e).__name__}: {e}")
    return u.id


def unlink(db, user_id: int) -> bool:
    from server.models import User
    u = db.query(User).filter_by(id=user_id).first()
    if not u:
        return False
    u.max_user_id = None
    u.max_username = None
    u.max_link_code = None
    u.max_link_expires = None
    db.commit()
    return True


# ── Webhook handler ────────────────────────────────────────────────────────


async def handle_update(update: dict) -> None:
    """Обработка одного update от MAX.

    MAX update format (по докам https://dev.max.ru/docs-api):
      {
        "update_type": "message_created",
        "message": {
          "sender": {"user_id": 123, "name": "Денис", "username": "denis"},
          "recipient": {"chat_id": ..., "user_id": ...},
          "body": {"text": "привет"}
        }
      }
    """
    if not isinstance(update, dict):
        return
    msg = update.get("message")
    if not msg:
        return
    sender = msg.get("sender") or {}
    body = msg.get("body") or {}
    max_uid = str(sender.get("user_id", ""))
    max_username = sender.get("username") or sender.get("name") or ""
    text = (body.get("text") or "").strip()

    if not max_uid:
        return

    if text.startswith("/start"):
        m = re.match(r"^/start(?:\s+(.+))?$", text)
        arg = (m.group(1) if m else "").strip()
        if arg.upper().startswith("LINK_"):
            code = arg[5:].strip().upper()
            await _do_link(max_uid, max_username, code)
            return
        await send_message(max_uid,
            "👋 **AI Студия Че — управление**\n\n"
            "Этот бот связывает тебя с твоим аккаунтом на aiche.ru.\n\n"
            "Чтобы начать: открой aiche.ru → 📲 «Где использовать Че» → "
            "«Подключить MAX», скопируй код и пришли его сюда:\n"
            "`/link XXXXXX`\n\n"
            "После привязки доступны команды:\n"
            "/me — баланс и профиль\n"
            "/unlink — отвязать аккаунт\n\n"
            "Или просто пиши боту — Че ответит как на сайте.")
        return
    if text.startswith("/link"):
        m = re.match(r"^/link\s+([A-Z0-9]+)$", text.upper())
        if not m:
            await send_message(max_uid, "Формат: `/link XXXXXX`\nКод получи на сайте.")
            return
        await _do_link(max_uid, max_username, m.group(1))
        return
    if text.startswith("/unlink"):
        await _do_unlink(max_uid)
        return
    if text.startswith("/me"):
        await _do_me(max_uid)
        return
    if text.startswith("/help"):
        await send_message(max_uid,
            "Команды: /me /unlink /help\n\nИли пиши обычным текстом — Че ответит.")
        return
    # Команда не распознана?
    if text.startswith("/"):
        await send_message(max_uid,
            "Не понял команду. Доступные: /me /unlink /help.\n"
            "Или пиши обычным текстом — Че ответит.")
        return
    # ── Relay к Че ─────────────────────────────────────────────────────────
    await _do_relay_to_che(max_uid, text)


async def _do_link(max_uid: str, max_username: str, code: str) -> None:
    # Прогрессивная защита от brute-force (см. tg_management._do_link)
    from server.security import link_code_attempt_check
    allowed, reason = link_code_attempt_check("max", max_uid)
    if not allowed:
        await send_message(max_uid, f"🛑 {reason}")
        return
    from server.db import db_session
    with db_session() as db:
        user_id = consume_link_code(db, code, max_uid, max_username)
        if not user_id:
            await send_message(max_uid,
                "❌ Код не подходит или истёк (10 мин). Сгенерируй новый на сайте.")
            return
        from server.models import User
        u = db.query(User).filter_by(id=user_id).first()
        email = u.email if u else "(?)"
    await send_message(max_uid,
        f"✅ Привязано! Аккаунт: `{email}`\n\n"
        f"Теперь можешь писать Че прямо здесь — он помнит тебя как на сайте.\n"
        f"/me — баланс и профиль")


async def _do_unlink(max_uid: str) -> None:
    from server.db import db_session
    from server.models import User
    with db_session() as db:
        u = db.query(User).filter_by(max_user_id=max_uid).first()
        if not u:
            await send_message(max_uid, "Этот MAX не привязан к аккаунту.")
            return
        unlink(db, u.id)
    await send_message(max_uid, "🔓 Отвязано.")


def _kop_to_rub(kop: int) -> str:
    return f"{(kop or 0) / 100:.0f} ₽"


async def _do_me(max_uid: str) -> None:
    from server.db import db_session
    from server.models import User
    with db_session() as db:
        u = db.query(User).filter_by(max_user_id=max_uid).first()
        if not u:
            await send_message(max_uid, "Сначала привяжи аккаунт: /start или /link XXXXXX")
            return
        email = u.email
        balance = _kop_to_rub(u.tokens_balance or 0)
    await send_message(max_uid,
        f"👤 **{email}**\n💰 Баланс: **{balance}**")


async def _do_relay_to_che(max_uid: str, text: str) -> None:
    """Любое сообщение от привязанного юзера → Че через tg_che_relay.process_message.

    Используем тот же relay что и для TG — функция БД-агностичная, не зависит
    от мессенджера. Форматирование вывода для MAX — без HTML, через markdown
    (MAX поддерживает format="markdown" в send_message).
    """
    from server.db import db_session
    from server.models import User
    from server.tg_che_relay import process_message
    from server.security import _check as _rl_check

    # Rate-limit
    if not _rl_check(f"max-che:{max_uid}", max_calls=20, window_sec=60):
        await send_message(max_uid, "🛑 Слишком много сообщений. Подожди минуту.")
        return
    if not _rl_check(f"max-che-h:{max_uid}", max_calls=200, window_sec=3600):
        await send_message(max_uid, "🛑 Слишком много сообщений за час.")
        return

    # Привязка
    with db_session() as db:
        u = db.query(User).filter_by(max_user_id=max_uid).first()
        if not u:
            await send_message(max_uid,
                "👋 Чтобы общаться с Че через MAX, привяжи аккаунт:\n\n"
                "1. Открой aiche.ru → 📲 «Где использовать Че»\n"
                "2. Нажми «Подключить MAX», скопируй код\n"
                "3. Пришли сюда: `/link XXXXXX`")
            return
        user_id = u.id

    # Sync обработка в thread (БД-операции)
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
        log.exception(f"[max-mgmt] relay to Che failed: {e}")
        await send_message(max_uid,
            "😔 Что-то пошло не так. Попробуй ещё раз через минуту.")
        return

    if result.get("error") == "insufficient_funds":
        await send_message(max_uid, result.get("reply", "Недостаточно средств."))
        return

    # Форматирование для MAX (markdown, без HTML)
    parts = _format_for_max(result)
    for i, part in enumerate(parts):
        ok = await send_message(max_uid, part)
        if not ok:
            log.warning(f"[max-mgmt] failed to send part {i} to max_uid={max_uid}")


# Markdown-спецсимволы которые экранируются перед отправкой в MAX. LLM-output
# может содержать `[click](https://phish.example)`, сломанные `**`, fenced code
# и прочее — без escape MAX интерпретирует это как разметку, что может
# а) подменить ссылки на фишинг, б) сломать рендер сообщения.
# Список — те символы что MAX считает значимыми в format=markdown.
_MAX_MD_ESCAPE_RE = None


def _escape_md(text: str) -> str:
    """Экранировать markdown-спецсимволы для безопасной вставки в MAX-сообщение.

    Прибавляет `\\` перед каждым из: * _ ` [ ] ( ) ~ > # + - = | { } . !
    Список взят из CommonMark/Telegram MarkdownV2 (MAX наследует похожую
    грамматику). Не трогает буквы / цифры / пунктуацию-разделители — текст
    остаётся читаемым.
    """
    global _MAX_MD_ESCAPE_RE
    if _MAX_MD_ESCAPE_RE is None:
        import re as _re
        _MAX_MD_ESCAPE_RE = _re.compile(r"([\\\*_`\[\]\(\)~>#+=|{}.!\-])")
    return _MAX_MD_ESCAPE_RE.sub(r"\\\1", str(text or ""))


def _format_for_max(result: dict) -> list[str]:
    """Превратить result в 1-2 MAX-сообщения (markdown-формат).

    Отличие от TG-формата (tg_che_relay.format_for_tg): MAX использует
    markdown, не HTML. LLM-output (reply, module_reply) — НЕ доверенный
    источник: prompt-injection может вернуть [phish](https://evil), сломать
    разметку или подменить ссылки. Escape ВСЕ спецсимволы.

    Заголовки которые контролирует backend (🧩, level-up бейдж) рендерятся
    как настоящий markdown — bold через **, и эти ** мы НЕ escape'аем.
    """
    parts: list[str] = []

    reply = (result.get("reply") or "").strip()
    if reply:
        # Reply от Che — может содержать prompt-injected markdown
        parts.append(_escape_md(reply))

    mod_reply = (result.get("module_reply") or "").strip()
    mod_slug = result.get("module_slug")
    if mod_reply and mod_slug:
        level = result.get("new_level", 0)
        level_label = f"L{level}"
        level_up = result.get("level_up", False)
        # Header — backend-controlled, можем использовать markdown bold (**).
        # mod_slug — из enabled AgentModule.slug → стандартизированный.
        # Но всё равно escape, defense-in-depth.
        header = f"🧩 **{_escape_md(mod_slug)}** · {_escape_md(level_label)}"
        if level_up:
            header += f" ⬆ прокачался до {_escape_md(level_label)}\\!"
        # Module reply — LLM-output, escape всё
        parts.append(f"{header}\n\n{_escape_md(mod_reply)}")

    return parts or ["\\(пустой ответ\\)"]
