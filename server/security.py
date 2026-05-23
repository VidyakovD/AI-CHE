"""
Rate limiting + input validation middleware.
Shared store в SQLite — работает между несколькими uvicorn workers.
"""
import time, re, os, sqlite3
from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse

# ── SQLite-based rate limit store (shared across workers) ─────────────────────
# Используем отдельный файл чтобы не блокировать основную БД chat.db
_RL_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".rate_limit.db")
_RL_INITIALIZED = False


def _rl_conn():
    """Открывает соединение к SQLite rate-limit БД. WAL для конкурентности."""
    global _RL_INITIALIZED
    conn = sqlite3.connect(_RL_DB_PATH, timeout=5.0, isolation_level=None)
    if not _RL_INITIALIZED:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS rl (k TEXT NOT NULL, t REAL NOT NULL)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_rl_k_t ON rl(k, t)")
        _RL_INITIALIZED = True
    return conn


def _check(key: str, max_calls: int, window_sec: int) -> bool:
    """Атомарная проверка через SQLite: DELETE старых → COUNT → INSERT."""
    now = time.time()
    try:
        conn = _rl_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            # Чистим старые записи этого ключа
            conn.execute("DELETE FROM rl WHERE k=? AND t < ?", (key, now - window_sec))
            count = conn.execute("SELECT COUNT(*) FROM rl WHERE k=?", (key,)).fetchone()[0]
            if count >= max_calls:
                conn.execute("COMMIT")
                return False
            conn.execute("INSERT INTO rl(k, t) VALUES (?, ?)", (key, now))
            conn.execute("COMMIT")
            return True
        finally:
            conn.close()
    except Exception:
        # При сбое БД пропускаем (лучше доступно, чем падать)
        return True

def link_code_attempt_check(channel: str, identifier: str) -> tuple[bool, str]:
    """Прогрессивная защита от brute-force одноразовых кодов привязки
    (Telegram/MAX management-боты — /link XXXXXX).

    Двухуровневая sliding-window:
      - 5 попыток / минуту (защита от быстрого спам-бота)
      - 30 попыток / час   (защита от медленной атаки)

    При исчерпании лимита возвращает (False, "human-readable причина").
    При успехе — увеличивает счётчик и возвращает (True, "").

    Args:
      channel: "tg" | "max" — для namespace ключа.
      identifier: tg_user_id / max_user_id / IP — кого ограничиваем.

    Также пишет log.warning при срабатывании лимита — для алерта в audit
    (юзер с >30 fails/час подозрителен, можно мониторить).

    Шифр кода 6 chars из алфавита 27 символов = ~387 млн комбинаций. TTL 10 мин.
    Без лимитов хватит секунд за 5 минут на пинг 1мс. С лимитом 30/час —
    практически невозможно вытащить.
    """
    import logging as _logging
    log = _logging.getLogger("rate-limit")
    # Fast window: 5 попыток / 60 сек
    if not _check(f"link-fast:{channel}:{identifier}",
                  max_calls=5, window_sec=60):
        log.warning(f"[link-fast-limit] {channel}:{identifier}")
        return False, "Слишком много попыток за минуту. Подожди 60 секунд и попробуй снова."
    # Slow window: 30 попыток / час
    if not _check(f"link-slow:{channel}:{identifier}",
                  max_calls=30, window_sec=3600):
        log.warning(f"[link-slow-limit] {channel}:{identifier}")
        return False, "Слишком много попыток за час. Подожди 1 час."
    return True, ""


RULES = {
    # path_prefix: (max_calls, window_seconds)
    "/auth/login":               (10,  300),   # 10 попыток/5мин на IP (анти-брутфорс)
    "/auth/register":            (5,   60),
    "/auth/forgot-password":     (5,  300),
    "/auth/verify-email":        (10,  60),
    "/auth/resend-verify":       (3,   60),
    "/auth/reset-password":      (10,  60),
    "/auth/oauth/exchange":      (30,  60),   # один юзер может пополнить несколько провайдеров
    "/message":                  (60,  60),    # 60 сообщений/мин
    "/upload":                   (20,  60),
    # Webhook endpoints — анти-DDoS / анти-подделка (макс 60/мин на IP = 1 в секунду)
    # ЮKassa может слать много retry, TG тоже — но 60/мин более чем достаточно
    "/webhook/tg/":              (120, 60),
    "/webhook/vk/":              (120, 60),
    "/webhook/avito/":           (120, 60),
    "/webhook/max/":             (120, 60),
    # Management-боты (мы один бот на платформу — не сотни клиентских)
    # 240/мин достаточно: один юзер в чате с Че редко превысит 1 msg/сек,
    # но при leaked-секрете нужна анти-flood защита. Дублирует per-tg_uid
    # rate-limit внутри _do_relay_to_che (20/мин на юзера).
    "/webhook/tg-mgmt":          (240, 60),
    "/webhook/max-mgmt/":        (240, 60),
    "/payment/webhook":          (60,  60),
    # Платежи — защита от перебора чужих payment_id и от спама создания платежей
    "/payment/buy-tokens":       (20,  60),
    "/payment/confirm-tokens":   (30,  60),
    # Админ-эндпоинты + deploy — защита от брут-форса DEPLOY_TOKEN
    "/internal/deploy":          (10, 3600),
    # Агент / воркфлоу / генерации — дорогие
    "/agent/run":                (30,  60),
    # AI-конструктор бота — стоит реальных копеек, лимит на спам
    "/chatbots/ai-create":       (10, 300),
    "/chatbots/ai-build-workflow": (20, 300),
    # Точечная AI-правка блоков сайта — 5 ₽ за вызов, лимит против перебора
    "/sites/projects":           (120, 60),
    # Привязка TG/MAX — анти-spam регенерации кодов
    # (юзер может нажимать «Подключить» много раз, но не 100 раз в минуту).
    "/user/tg-link/code":        (10, 600),
    "/user/max-link/code":       (10, 600),
    # Кабинет / поддержка / транзакции — защита от scrape
    "/user/cabinet/stats":       (60,  60),
    "/user/transactions.csv":    (10, 300),    # тяжёлый CSV-экспорт
    "/user/support/refund":      (3,  3600),   # 3 заявки в час
    "/user/support/delete-data": (3,  3600),
    # Контекстный помощник по разделам — глобальный лимит на IP против DoS.
    # На юзера ещё 60 / 12ч проверяется отдельно в /assistant/ask.
    "/assistant/":               (120, 60),
    "/assistant/feedback":       (180, 60),  # клик по 👍/👎/💡 — частая операция
    # QR-логин: poll частит (каждые 1.5 сек), но в рамках одной QR-сессии
    # макс ~80 запросов за 120 секунд жизни. Ставим запас.
    "/qr-login/init":            (10,  60),
    "/qr-login/poll":            (300, 60),   # одна страница может пушить 80, плюс параллельные сессии
    "/qr-login/approve":         (30,  60),
    # Mobile: лента и голос. Голос — дороже (Whisper-вызов), отдельный лимит.
    "/mobile/feed":              (60,  60),
    "/mobile/voice/parse":       (30,  60),
    "/mobile/voice/transcribe":  (15, 300),
    # TTS: каждый вызов = OpenAI-запрос + минимум 50 коп списания. Анти-DoS
    # на «слить весь баланс юзера за минуту автоматизированной отправкой»:
    # 30 запросов / 5 минут на IP — в реальном UX этого хватит с запасом.
    "/mobile/voice/tts":         (30, 300),
    # 2FA админки: брутфорс TOTP теоретически возможен через /admin/2fa/enable
    # (хоть и требует валидного auth cookie). Жёсткий лимит на IP для defense.
    "/admin/2fa/":               (20, 300),
    # Knowledge: загрузка дорогая (embeddings-вызов в фоне). Не пускаем спам.
    "/knowledge/upload":          (15,  300),
    "/knowledge/search":          (60,  60),
    "/knowledge":                 (60,  60),
    # Публичные КП: /p/{token}, /p/{token}/pdf, /p/{token}/sign — анти-спам.
    # Защищает от: 1) брутфорса токенов (хоть и 16 chars = ~95 bit, минимизируем
    # noise в логах), 2) спама подписей с раздутым data:image body (2MB).
    # 240/мин — 4 req/s, хватит для офисной сети с несколькими просмотрами,
    # но блокирует автоматизированные атаки.
    "/p/":                        (240, 60),
}

_TRUSTED_PROXIES = {p.strip() for p in os.getenv("TRUSTED_PROXIES", "127.0.0.1,::1").split(",") if p.strip()}


def _get_client_ip(request: Request) -> str:
    """
    Возвращает IP клиента. X-Forwarded-For доверяем только если запрос пришёл
    от proxy из TRUSTED_PROXIES (по умолчанию — только 127.0.0.1).
    Иначе атакующий мог бы подделать заголовок и обойти rate-limit.
    """
    direct_ip = request.client.host if request.client else "unknown"
    if direct_ip in _TRUSTED_PROXIES:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip.strip()
    return direct_ip


def _rl_user_id(request: Request) -> str | None:
    """Извлечь user_id из cookie access_token либо Authorization header.
    Используется как rate-limit ключ для аутентифицированных запросов —
    иначе все юзеры за NAT/CGNAT/корпоративным IP делят один лимит, а
    атакующий с пулом IP тривиально обходит per-IP rate-limit.
    """
    try:
        from server.auth import extract_token, decode_token
        tok = extract_token(request)
        if not tok:
            return None
        payload = decode_token(tok, require_type="access")
        if payload and payload.get("sub"):
            return str(payload["sub"])
    except Exception:
        pass
    return None


async def rate_limit_middleware(request: Request, call_next):
    path = request.url.path
    ip = _get_client_ip(request)

    for prefix, (max_c, win) in RULES.items():
        if path.startswith(prefix):
            # Для аутентифицированных запросов ключ = user_id, для анонимных = IP.
            # Это убирает unfairness за NAT/CGNAT и одновременно блокирует
            # IP-pool атаки на дорогие эндпоинты юзера.
            uid = _rl_user_id(request)
            actor = f"u:{uid}" if uid else f"ip:{ip}"
            key = f"{actor}:{prefix}"
            if not _check(key, max_c, win):
                return JSONResponse(
                    status_code=429,
                    content={"detail": f"Слишком много запросов. Подождите {win} секунд."}
                )
            break

    return await call_next(request)


# ── validators ────────────────────────────────────────────────────────────────

EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")
ALLOWED_UPLOAD_MIME = {
    "image/jpeg", "image/png", "image/gif", "image/webp",
    "application/pdf", "text/plain",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
ALLOWED_UPLOAD_EXT = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp",
    ".pdf", ".txt", ".doc", ".docx",
}

def validate_email(email: str) -> str:
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "Некорректный формат email")
    if len(email) > 254:
        raise HTTPException(400, "Email слишком длинный")
    return email

def validate_password(password: str) -> None:
    """Политика паролей: длина 10+ символов + ВСЕ 4 класса:
    строчная (a-z), заглавная (A-Z), цифра (0-9), знак (!@#$%…).

    Plus блок-лист самых распространённых ("password", "qwerty12345" и т.д.).
    Раньше требовали только 2 класса из 4 — теперь все 4.
    """
    if len(password) < 10:
        raise HTTPException(400, "Пароль должен быть не менее 10 символов")
    if len(password) > 128:
        raise HTTPException(400, "Пароль слишком длинный (макс 128)")
    has_lower = any(c.islower() for c in password)
    has_upper = any(c.isupper() for c in password)
    has_digit = any(c.isdigit() for c in password)
    has_special = any(not c.isalnum() for c in password)
    missing = []
    if not has_lower:   missing.append("строчная буква (a-z)")
    if not has_upper:   missing.append("заглавная буква (A-Z)")
    if not has_digit:   missing.append("цифра (0-9)")
    if not has_special: missing.append("знак (!@#$%^&*…)")
    if missing:
        raise HTTPException(
            400,
            "Пароль должен содержать: " + ", ".join(missing) + ".",
        )
    # Топ-список самых распространённых — отсекаем явные «password», «qwerty», «12345678»
    _COMMON = {
        "password", "qwerty", "12345678", "123456789", "qwerty123",
        "1q2w3e4r5t", "abc123456", "password1", "iloveyou1",
        "admin12345", "letmein123",
    }
    if password.lower() in _COMMON:
        raise HTTPException(400, "Этот пароль слишком распространён, выберите другой")

def validate_upload_filename(filename: str) -> None:
    import os
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in ALLOWED_UPLOAD_EXT:
        raise HTTPException(400, f"Тип файла не разрешён. Допустимы: {', '.join(ALLOWED_UPLOAD_EXT)}")


# SVG-санитайзер: SVG-файл может содержать <script>, on*-handlers и javascript:-URI,
# которые выполнятся в браузере при рендере как <img>/<object> или прямом открытии.
# Поскольку у нас есть public-эндпоинт /assets/public/{token}, это потенциальный XSS.
#
# Подход: парсим XML через defusedxml (XXE-safe), потом обходим дерево и режектим
# по чёткому blocklist'у. Раньше был подстрочный scan по lower-case — мисс-кейсы:
#   • encoded entities (&#x6a;avascript: → не ловится)
#   • SVG-animation: <set onbegin=> / <animate from="javascript:...">
#   • <style>@import 'http://evil/'</style>
#   • <use xlink:href="javascript:...">
# Парсинг + walk покрывает все варианты, потому что мы смотрим на структурные
# теги/атрибуты ПОСЛЕ XML-нормализации (entity expansion).

# Запрещённые SVG/XML-теги (любой из них = отклонение файла)
_SVG_BLOCKED_TAGS = {
    # Скриптинг
    "script",
    # Embedded HTML/обходы (foreignObject позволяет вставить произвольный HTML)
    "foreignobject", "iframe", "embed", "object",
    # SMIL animation triggers — onbegin/onend/onrepeat исполняют JS при анимации
    "set", "animate", "animatetransform", "animatemotion",
    # <style> может содержать @import 'http://evil/' или CSS-expression
    "style",
    # <handler> и др. редкие event-теги
    "handler", "listener",
}

# Атрибуты-href с javascript:/vbscript: схемами (для use, image, a, etc.)
_SVG_HREF_ATTRS = {"href", "xlink:href", "{http://www.w3.org/1999/xlink}href"}

# Запрещённые префиксы значений href
_HREF_BLOCKED_SCHEMES = ("javascript:", "vbscript:", "data:text/", "data:application/")


def sanitize_svg_or_raise(data: bytes) -> None:
    """
    Парсит SVG как XML (defusedxml — XXE-safe), обходит дерево, отклоняет
    при любом из:
      • Запрещённый тег (script / foreignObject / animate / set / style / ...)
      • Любой атрибут начинающийся с `on` (onload, onclick, onbegin, ...)
      • href/xlink:href с javascript:/vbscript:/data:text схемой
      • DOCTYPE (защита от XXE — defusedxml её и так блокирует)

    Поднимает HTTPException 400 при нарушении.
    Используется в /upload и /assets/upload перед сохранением.
    """
    if not data:
        return
    # Лимит — 1 МБ для парсинга. Большие SVG обычно не валидны и тратят CPU.
    payload = data[:1_000_000]
    try:
        from defusedxml import ElementTree as _dET
    except ImportError:
        # Fallback: если defusedxml отсутствует, используем строгий substring scan
        text_lower = payload.decode("utf-8", errors="ignore").lower()
        bad = ("<script", "<foreignobject", "<iframe", "<embed", "<object",
               "<style", "<animate", "<set ", "<set>", "javascript:", "vbscript:",
               " onload=", " onerror=", " onclick=", " onbegin=", " onend=",
               " onmouseover=", " onfocus=", " onblur=", " onpointer", " ontoggle=",
               " onmessage=", " onactivate=", " onloadstart=", " onloadend=",
               " onrepeat=", " onanimation")
        if any(tok in text_lower for tok in bad):
            raise HTTPException(400,
                "SVG содержит исполняемый код (script/on-handler/iframe) — отклонено")
        return

    try:
        # forbid_dtd=True — блокирует <!DOCTYPE (XXE / billion-laughs).
        # forbid_entities=True — блокирует custom entity expansion.
        tree = _dET.fromstring(payload, forbid_dtd=True, forbid_entities=True)
    except Exception:
        # Невалидный XML — отклоняем (а не пропускаем «как-есть»)
        raise HTTPException(400, "Не удалось распарсить SVG (невалидный XML)")

    def _local(tag: str) -> str:
        # SVG в namespace: '{http://www.w3.org/2000/svg}script' → 'script'
        if "}" in tag:
            tag = tag.rsplit("}", 1)[1]
        return tag.lower()

    def _walk(elem):
        if _local(elem.tag) in _SVG_BLOCKED_TAGS:
            raise HTTPException(400,
                f"SVG содержит запрещённый тег <{_local(elem.tag)}> — отклонено")
        for attr_name, attr_val in (elem.attrib or {}).items():
            an = attr_name.lower()
            if "}" in an:
                an_local = an.rsplit("}", 1)[1]
            else:
                an_local = an
            # Любой on*-handler — отклоняем
            if an_local.startswith("on"):
                raise HTTPException(400,
                    f"SVG содержит event-handler {an_local}= — отклонено")
            # href/xlink:href с javascript: и подобным
            if (an in _SVG_HREF_ATTRS or an_local in {"href"}):
                v_low = (attr_val or "").strip().lower()
                # Strip whitespace и контрольные символы (обход через табы/CR)
                v_clean = "".join(c for c in v_low if c.isprintable() and c not in " \t\r\n")
                for bad_scheme in _HREF_BLOCKED_SCHEMES:
                    if v_clean.startswith(bad_scheme):
                        raise HTTPException(400,
                            f"SVG содержит {bad_scheme} в href — отклонено")
        for child in list(elem):
            _walk(child)

    _walk(tree)

# ── admin check ───────────────────────────────────────────────────────────────

ADMIN_EMAILS = set(
    e.strip().lower()
    for e in __import__("os").getenv("ADMIN_EMAILS", "").split(",")
    if e.strip()
)

def require_admin(user) -> None:
    if user.email.lower() not in ADMIN_EMAILS:
        raise HTTPException(403, "Доступ запрещён")


# ── webhook signing ───────────────────────────────────────────────────────────

def mask_email(email: str | None) -> str:
    """
    Маскировка email для логов/алертов: vidyakov@obsidian.ai → vi***@obsidian.ai.
    PII-safe: не выводим локальную часть, домен оставляем (нужно для debug).
    """
    if not email:
        return "—"
    s = str(email).strip()
    if "@" not in s:
        return s[:2] + "***" if len(s) > 3 else "***"
    local, _, domain = s.partition("@")
    if len(local) <= 2:
        return "***@" + domain
    return local[:2] + "***@" + domain


def tg_webhook_secret(tg_token: str) -> str:
    """
    Производный secret для X-Telegram-Bot-Api-Secret-Token.
    Не требует хранения в БД — выводится из tg_token + JWT_SECRET.
    Меняется только если меняется JWT_SECRET или tg_token.

    [:32] — 128 бит, на грани best-practice. Не увеличиваем до полного
    SHA-256 (64 hex), потому что это сломает все уже выставленные
    Telegram webhook'и: setWebhook был вызван со старым 32-char secret,
    и пока бот не пере-настроится, все апдейты будут падать с 401.
    Защита от тайминг-атаки уже даёт hmac.compare_digest в проверке.
    """
    import hmac, hashlib
    base = os.getenv("JWT_SECRET", "")
    if not base:
        return ""
    return hmac.new(
        base.encode(), f"tg-webhook:{tg_token}".encode(), hashlib.sha256
    ).hexdigest()[:32]
