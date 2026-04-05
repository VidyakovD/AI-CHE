"""
Rate limiting + input validation middleware.
Uses in-memory store (works for single-process; swap for Redis in prod).
"""
import time, re
from collections import defaultdict
from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse

# ── in-memory rate limit store ────────────────────────────────────────────────
# { key: [timestamp, ...] }
_store: dict[str, list[float]] = defaultdict(list)

def _check(key: str, max_calls: int, window_sec: int) -> bool:
    now = time.time()
    calls = [t for t in _store[key] if now - t < window_sec]
    _store[key] = calls
    if len(calls) >= max_calls:
        return False
    _store[key].append(now)
    return True

RULES = {
    # path_prefix: (max_calls, window_seconds)
    "/auth/login":               (100,  60),    # 10 попыток/мин на IP
    "/auth/register":            (5,   60),
    "/auth/forgot-password":     (5,  300),
    "/auth/verify-email":        (10,  60),
    "/auth/resend-verify":       (3,   60),
    "/auth/reset-password":      (10,  60),
    "/message":                  (60,  60),    # 60 сообщений/мин
    "/upload":                   (20,  60),
}

async def rate_limit_middleware(request: Request, call_next):
    path = request.url.path
    ip   = request.client.host if request.client else "unknown"

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
