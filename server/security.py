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
    # Кабинет / поддержка / транзакции — защита от scrape
    "/user/cabinet/stats":       (60,  60),
    "/user/transactions.csv":    (10, 300),    # тяжёлый CSV-экспорт
    "/user/support/refund":      (3,  3600),   # 3 заявки в час
    "/user/support/delete-data": (3,  3600),
    # Контекстный помощник по разделам — глобальный лимит на IP против DoS.
    # На юзера ещё 60 / 12ч проверяется отдельно в /assistant/ask.
    "/assistant/":               (120, 60),
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


async def rate_limit_middleware(request: Request, call_next):
    path = request.url.path
    ip = _get_client_ip(request)

    for prefix, (max_c, win) in RULES.items():
        if path.startswith(prefix):
            key = f"{ip}:{prefix}"
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
    """Политика паролей: длина + минимум 2 разных класса символов.
    Не запрещаем passphrase'ы (длинные слабее по entropy не становятся), но
    отсекаем «12345678», «aaaaaaaa», «password», «qwerty12».
    """
    if len(password) < 10:
        raise HTTPException(400, "Пароль должен быть не менее 10 символов")
    if len(password) > 128:
        raise HTTPException(400, "Пароль слишком длинный (макс 128)")
    classes = 0
    if any(c.islower() for c in password): classes += 1
    if any(c.isupper() for c in password): classes += 1
    if any(c.isdigit() for c in password): classes += 1
    if any(not c.isalnum() for c in password): classes += 1
    if classes < 2:
        raise HTTPException(
            400,
            "Пароль слишком простой. Используйте минимум 2 типа символов: "
            "буквы (a-z), заглавные (A-Z), цифры (0-9) или знаки (!@#$%)."
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
_SVG_BAD_TOKENS = (
    "<script", "</script", "<foreignobject", "javascript:",
    " onload=", " onerror=", " onclick=", " onmouseover=",
    " onfocus=", " onblur=", " onanimation", " ontoggle=",
    " onbegin=", " onend=", " onrepeat=", " onactivate=",
    " onloadstart=", " onloadend=", " onpointer", " onmessage=",
    "<iframe", "<embed", "<object",
)

def sanitize_svg_or_raise(data: bytes) -> None:
    """
    Бьёт по содержимому SVG/XML на наличие исполняемого кода.
    Поднимает HTTPException 400, если найдены опасные токены.
    Используется в /upload и /assets/upload.
    """
    try:
        text_lower = data[:65536].decode("utf-8", errors="ignore").lower()
    except Exception:
        text_lower = ""
    if any(tok in text_lower for tok in _SVG_BAD_TOKENS):
        raise HTTPException(400, "SVG содержит исполняемый код (script/on-handler/iframe) — отклонено")

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
