"""JWT auth + verification token helpers + cookie/CSRF helpers."""
import os, secrets, string
from datetime import datetime, timedelta
from passlib.context import CryptContext
from jose import JWTError, jwt
from fastapi import Response, Request

# ВАЖНО: load_dotenv ДО _get_jwt_secret — иначе race condition.
# Если auth.py импортируется раньше ai.py (где раньше был load_dotenv),
# os.getenv("JWT_SECRET") вернёт None → возьмётся ключ из server/.jwt_secret.
# При следующей перезагрузке порядок мог быть другой → разные ключи →
# зашифрованные секреты в БД (max_token, IMAP пароли) не расшифровываются.
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except Exception:
    pass


def _get_jwt_secret() -> str:
    """Стабильный JWT-секрет: из env или сохранённый файл (генерируется один раз)."""
    env_secret = os.getenv("JWT_SECRET")
    if env_secret:
        return env_secret
    secret_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".jwt_secret")
    if os.path.exists(secret_path):
        with open(secret_path) as f:
            return f.read().strip()
    new_secret = secrets.token_hex(32)
    try:
        with open(secret_path, "w") as f:
            f.write(new_secret)
    except Exception:
        pass
    return new_secret


SECRET_KEY = _get_jwt_secret()
ALGORITHM  = "HS256"
ACCESS_TTL  = 60 * 24        # 1 day in minutes (short-lived access token)
REFRESH_TTL = 60 * 24 * 30   # 30 days in minutes (long-lived refresh token)
JWT_ISS     = os.getenv("JWT_ISS", "aiche")
JWT_AUD     = os.getenv("JWT_AUD", "aiche-web")


def _all_jwt_secrets() -> list[str]:
    """
    Все возможные ключи для verify JWT — на случай race в _get_jwt_secret
    или ротации secret. Раньше токены могли быть подписаны:
      - текущим JWT_SECRET (из env через load_dotenv)
      - содержимым файла server/.jwt_secret (если auth.py импортировался
        раньше load_dotenv())
      - ключом из LEGACY_JWT_SECRETS

    decode_token пробует их все по очереди — до первого успешного verify.
    Так уже выданные браузерные сессии не отвалятся при изменении
    последовательности импортов.
    """
    seen = set()
    out = []
    for s in (SECRET_KEY, os.getenv("JWT_SECRET", "")):
        if s and s not in seen:
            seen.add(s); out.append(s)
    # Файловый ключ
    try:
        secret_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    ".jwt_secret")
        if os.path.exists(secret_path):
            with open(secret_path) as f:
                fk = f.read().strip()
            if fk and fk not in seen:
                seen.add(fk); out.append(fk)
    except Exception:
        pass
    # Legacy секреты для ротации
    for s in os.getenv("LEGACY_JWT_SECRETS", "").split(","):
        s = s.strip()
        if s and s not in seen:
            seen.add(s); out.append(s)
    return out

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain: str) -> str:
    return pwd_ctx.hash(plain)

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_ctx.verify(plain, hashed)

def create_token(user_id: int, email: str) -> str:
    """Create short-lived access token (1 day)."""
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TTL)
    return jwt.encode(
        {"sub": str(user_id), "email": email, "exp": expire, "type": "access",
         "iss": JWT_ISS, "aud": JWT_AUD},
        SECRET_KEY, algorithm=ALGORITHM
    )


def _new_jti() -> str:
    """Уникальный JWT ID для refresh-токена (для single-use rotation)."""
    return secrets.token_urlsafe(16)


def create_refresh_token(user_id: int, email: str, jti: str | None = None) -> str:
    """Create long-lived refresh token (30 days).

    Если передан `jti` — кладём в payload. Иначе генерим новый. Caller
    должен зарегистрировать jti в `User.refresh_jtis` (через
    `register_refresh_jti`) — иначе токен не пройдёт verify в `decode_token`.
    """
    expire = datetime.utcnow() + timedelta(minutes=REFRESH_TTL)
    if jti is None:
        jti = _new_jti()
    return jwt.encode(
        {"sub": str(user_id), "email": email, "exp": expire, "type": "refresh",
         "iss": JWT_ISS, "aud": JWT_AUD, "jti": jti},
        SECRET_KEY, algorithm=ALGORITHM
    )


# ── Refresh-token rotation: single-use JTI tracking ─────────────────────────
# Чтобы украденный refresh нельзя было использовать после законной ротации,
# в БД храним set активных jti на юзера (User.refresh_jtis, JSON-список).
# При login/refresh — добавляем новый jti, при ротации — удаляем старый.
# decode_token(require_type='refresh') дополнительно проверяет, что jti
# присутствует в текущем наборе. Multi-device: до 10 параллельных сессий.

_MAX_ACTIVE_REFRESH_JTIS = 10  # cap на user — больше 10 параллельных устройств?


def _load_jtis(user) -> list[str]:
    raw = getattr(user, "refresh_jtis", None) or ""
    if not raw:
        return []
    try:
        import json as _json
        v = _json.loads(raw)
        return [str(x) for x in v if isinstance(x, (str, int))][:_MAX_ACTIVE_REFRESH_JTIS]
    except Exception:
        return []


def _save_jtis(user, jtis: list[str]) -> None:
    import json as _json
    user.refresh_jtis = _json.dumps(jtis[-_MAX_ACTIVE_REFRESH_JTIS:],
                                     separators=(",", ":"))


def _atomic_jtis_update(db, user, mutate):
    """Read-modify-write набора jti с защитой от race condition.
    Делаем re-fetch юзера в текущей сессии (фиксирует строку для UPDATE) +
    re-load jti после fetch — это сериализует параллельные login/refresh
    одного юзера. Без этого два процесса могли бы потерять чужой jti."""
    from server.models import User as _U
    fresh = db.query(_U).filter_by(id=user.id).with_for_update().first() \
        if db.bind.dialect.name not in ("sqlite",) \
        else db.query(_U).filter_by(id=user.id).first()
    target = fresh or user
    raw = getattr(target, "refresh_jtis", None) or ""
    try:
        import json as _json
        current = [str(x) for x in (_json.loads(raw) or [])
                   if isinstance(x, (str, int))][:_MAX_ACTIVE_REFRESH_JTIS]
    except Exception:
        current = []
    new_list = mutate(current)
    if new_list == current:
        return  # ничего не поменялось — даже не commit'им
    _save_jtis(target, new_list)
    db.commit()
    # Синхронизируем переданный объект (чтобы caller видел актуальное)
    if target is not user:
        try: user.refresh_jtis = target.refresh_jtis
        except Exception: pass


def register_refresh_jti(db, user, jti: str) -> None:
    """Зарегистрировать новый jti в наборе активных у юзера. Сохраняет до
    `_MAX_ACTIVE_REFRESH_JTIS` последних — старые выпадают (вытесняются
    при превышении лимита, юзеру с 11-го устройства придётся перелогиниться).

    Race-safe: re-fetch юзера + with_for_update (на не-SQLite). Без этого
    два одновременных login могут потерять jti друг друга."""
    def _mutate(jtis):
        if jti in jtis:
            return jtis  # уже есть — идемпотентно
        return jtis + [jti]
    _atomic_jtis_update(db, user, _mutate)


def revoke_refresh_jti(db, user, jti: str) -> bool:
    """Удалить jti из активных. Возвращает True если был — False иначе."""
    found = [False]
    def _mutate(jtis):
        if jti not in jtis:
            return jtis
        found[0] = True
        return [x for x in jtis if x != jti]
    _atomic_jtis_update(db, user, _mutate)
    return found[0]


def revoke_all_refresh_jtis(db, user) -> None:
    """Logout-everywhere: сброс всех refresh-токенов юзера (кроме access,
    который проживёт до своего exp). Используется при смене пароля
    или security-инциденте."""
    user.refresh_jtis = None
    db.commit()


def is_refresh_jti_active(user, jti: str | None) -> bool:
    """Проверка: jti из payload присутствует в наборе активных юзера.
    Если у юзера ещё нет refresh_jtis (legacy токены, выданные до миграции
    single-use rotation) — пропускаем проверку (grace-period). Эту grace
    можно убрать после 30-дней + миграции существующих токенов."""
    if not jti:
        # Legacy refresh без jti — пропускаем (grace).
        return True
    raw = getattr(user, "refresh_jtis", None) or ""
    if not raw:
        # У юзера ещё нет registered jti — это legacy сессия. Grace-период.
        return True
    return jti in _load_jtis(user)

def decode_token(token: str, require_type: str = None) -> dict | None:
    """Verify JWT. Пробует все доступные ключи (текущий + legacy), чтобы
    не разлогинивать юзеров при смене источника JWT_SECRET (env vs файл)."""
    payload = None
    last_exc: Exception | None = None
    for secret in _all_jwt_secrets():
        try:
            # TODO 2026-06-10: после истечения 30-дневного TTL refresh-токенов
            # с момента 2026-05-11 переключить на `verify_aud=True, verify_iss=True`.
            # См. TODO_NEXT.md → «JWT aud/iss strict verify».
            payload = jwt.decode(
                token, secret, algorithms=[ALGORITHM],
                options={"verify_aud": False, "verify_iss": False},
            )
            break  # Успех — выходим из цикла
        except JWTError as e:
            last_exc = e
            continue
    if payload is None:
        return None
    try:
        # Токены выданные после фикса всегда имеют aud+iss — проверяем
        if payload.get("aud") is not None and payload["aud"] != JWT_AUD:
            return None
        if payload.get("iss") is not None and payload["iss"] != JWT_ISS:
            return None
        if require_type and payload.get("type") != require_type:
            return None
        return payload
    except JWTError:
        return None

def generate_code(length: int = 6) -> str:
    """Generate numeric verification code."""
    return "".join(secrets.choice(string.digits) for _ in range(length))

VERIFY_TTL_MINUTES = 15


# ── Cookie / CSRF helpers ──────────────────────────────────────────────────
# JWT в localStorage = XSS = угнан токен. Переходим на httpOnly+Secure+SameSite
# cookie. JS не может читать токен → утечка через XSS блокируется.
# CSRF защита: double-submit cookie pattern. Сервер выставляет ДВА cookie:
#   access_token (HttpOnly) — сам JWT
#   csrf_token  (НЕ HttpOnly) — JS читает и шлёт обратно в X-CSRF-Token
# Middleware на запись-мутирующие методы проверяет совпадение cookie ↔ header.
# Атакующий с кросс-домена не может прочитать csrf_token (CORS) → не сможет
# отправить совпадающий header.

ACCESS_COOKIE_NAME  = "access_token"
REFRESH_COOKIE_NAME = "refresh_token"
CSRF_COOKIE_NAME    = "csrf_token"
CSRF_HEADER_NAME    = "X-CSRF-Token"

# В DEV — Secure=False иначе браузер не сохранит cookie на http://localhost.
_DEV_MODE = os.getenv("DEV_MODE", "").lower() in ("1", "true", "yes")
_COOKIE_SECURE = not _DEV_MODE
_COOKIE_DOMAIN = os.getenv("COOKIE_DOMAIN") or None  # для поддоменов: ".aiche.ru"


def _new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def set_auth_cookies(response: Response, access: str, refresh: str | None = None,
                     csrf: str | None = None) -> str:
    """
    Выставить httpOnly cookies + CSRF-token cookie. Возвращает CSRF-токен
    (его же фронт получит в JSON ответа login и сразу сможет использовать).
    """
    response.set_cookie(
        ACCESS_COOKIE_NAME, access,
        max_age=ACCESS_TTL * 60,
        httponly=True, secure=_COOKIE_SECURE, samesite="lax",
        path="/", domain=_COOKIE_DOMAIN,
    )
    if refresh:
        response.set_cookie(
            REFRESH_COOKIE_NAME, refresh,
            max_age=REFRESH_TTL * 60,
            httponly=True, secure=_COOKIE_SECURE, samesite="lax",
            path="/", domain=_COOKIE_DOMAIN,
        )
    csrf_value = csrf or _new_csrf_token()
    # CSRF cookie НЕ httpOnly — JS должен его прочитать и положить в header.
    # Atakker с другого origin не сможет (CORS блокирует cross-origin
    # чтение cookie через document.cookie).
    response.set_cookie(
        CSRF_COOKIE_NAME, csrf_value,
        max_age=ACCESS_TTL * 60,
        httponly=False, secure=_COOKIE_SECURE, samesite="lax",
        path="/", domain=_COOKIE_DOMAIN,
    )
    return csrf_value


def clear_auth_cookies(response: Response) -> None:
    """На /auth/logout — стираем все три cookie."""
    for name in (ACCESS_COOKIE_NAME, REFRESH_COOKIE_NAME, CSRF_COOKIE_NAME):
        response.delete_cookie(name, path="/", domain=_COOKIE_DOMAIN)


def extract_token(request: Request) -> str | None:
    """
    Получить access-токен запроса. Приоритет:
    1. Cookie `access_token` (новый путь, после миграции).
    2. Header `Authorization: Bearer ...` (legacy + mobile-clients).

    Так старые залогиненные сессии (token в localStorage у фронта)
    продолжают работать до истечения JWT, не ломая UX миграции.
    """
    cookie_tok = request.cookies.get(ACCESS_COOKIE_NAME)
    if cookie_tok:
        return cookie_tok
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return None
