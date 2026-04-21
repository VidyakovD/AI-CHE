"""
Rate limiting + input validation middleware.
Persistent store: saves to JSON file for crash/restart resilience.
"""
import time, re, os, json, threading
from collections import defaultdict
from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse

# ── persistent rate limit store ───────────────────────────────────────────────
# { key: [timestamp, ...] }
_store: dict[str, list[float]] = defaultdict(list)
_store_lock = threading.Lock()
_STORE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".rate_limit_store.json")

def _persist_store():
    """Save active (non-expired) entries to disk."""
    now = time.time()
    data = {k: [t for t in v if now - t < 600] for k, v in _store.items()}
    # Remove empty entries
    data = {k: v for k, v in data.items() if v}
    try:
        with open(_STORE_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass

def _load_store():
    """Restore entries from disk on startup."""
    if not os.path.exists(_STORE_FILE):
        return
    try:
        with open(_STORE_FILE, "r") as f:
            data = json.load(f)
        now = time.time()
        for k, v in data.items():
            _store[k] = [t for t in v if now - t < 600]
    except Exception:
        pass

# Load persisted data at module import
_load_store()

def _check(key: str, max_calls: int, window_sec: int) -> bool:
    with _store_lock:
        now = time.time()
        calls = [t for t in _store[key] if now - t < window_sec]
        _store[key] = calls
        if len(calls) >= max_calls:
            return False
        _store[key].append(now)
        # Persist periodically (every 10th call approximately)
        if len(calls) % 10 == 0:
            _persist_store()
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
    "/payment/webhook":          (60,  60),
    # Админ-эндпоинты + deploy — защита от брут-форса DEPLOY_TOKEN
    "/internal/deploy":          (10, 3600),
    # Агент / воркфлоу / генерации — дорогие
    "/agent/run":                (30,  60),
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
    if len(password) < 8:
        raise HTTPException(400, "Пароль должен быть не менее 8 символов")
    if len(password) > 128:
        raise HTTPException(400, "Пароль слишком длинный")

def validate_upload_filename(filename: str) -> None:
    import os
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in ALLOWED_UPLOAD_EXT:
        raise HTTPException(400, f"Тип файла не разрешён. Допустимы: {', '.join(ALLOWED_UPLOAD_EXT)}")

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

def tg_webhook_secret(tg_token: str) -> str:
    """
    Производный secret для X-Telegram-Bot-Api-Secret-Token.
    Не требует хранения в БД — выводится из tg_token + JWT_SECRET.
    Меняется только если меняется JWT_SECRET или tg_token.
    """
    import hmac, hashlib
    base = os.getenv("JWT_SECRET", "")
    if not base:
        return ""
    return hmac.new(
        base.encode(), f"tg-webhook:{tg_token}".encode(), hashlib.sha256
    ).hexdigest()[:32]
