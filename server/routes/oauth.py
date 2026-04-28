"""
OAuth регистрация/вход через Google и ВКонтакте.

Flow:
  GET /auth/oauth/{provider}/start                 — редирект на consent-экран
                                                     (с CSRF-токеном `state`)
  GET /auth/oauth/{provider}/callback?code=&state= — обмен code → user
                                                     → создаёт одноразовый exchange-код
                                                     → редирект назад с code (не токеном)
  POST /auth/oauth/exchange { code }               — фронт обменивает code на access/refresh
"""
import os, uuid, logging, urllib.parse
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
import httpx

from server.routes.deps import get_db, _user_dict
from server.models import User, Transaction, VerifyToken, OAuthState
from server.auth import create_token, create_refresh_token, hash_password, set_auth_cookies

log = logging.getLogger("oauth")
router = APIRouter(prefix="/auth/oauth", tags=["auth"])

APP_URL = os.getenv("APP_URL", "https://aiche.ru")

GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
VK_CLIENT_ID         = os.getenv("VK_CLIENT_ID", "")
VK_CLIENT_SECRET     = os.getenv("VK_CLIENT_SECRET", "")

# Whitelist провайдеров — иначе через `provider` в URL можно подсунуть путь
# с `..` и обойти валидацию OAuth-сервера на стороне провайдера.
_ALLOWED_PROVIDERS = {"google", "vk"}

# TTL state-параметра — должен быть >= наибольшего разумного времени consent screen
_STATE_TTL_SEC = 600


def _redirect_uri(provider: str) -> str:
    if provider not in _ALLOWED_PROVIDERS:
        raise HTTPException(400, "unknown OAuth provider")
    return f"{APP_URL}/auth/oauth/{provider}/callback"


def _issue_state(db: Session, provider: str, code_verifier: str | None = None) -> str:
    """Сгенерировать одноразовый CSRF-state, сохранить в БД, вернуть.
    code_verifier — для PKCE (нужно VK ID); для Google не передаётся.
    """
    state = uuid.uuid4().hex
    db.add(OAuthState(
        state=state, provider=provider,
        code_verifier=code_verifier,
        expires_at=datetime.utcnow() + timedelta(seconds=_STATE_TTL_SEC),
    ))
    db.commit()
    return state


def _consume_state(db: Session, provider: str, state: str | None) -> OAuthState | None:
    """
    Проверить state-параметр и пометить как использованный.
    Возвращает строку OAuthState (с code_verifier) или None.
    """
    if not state or not isinstance(state, str) or len(state) != 32:
        return None
    row = db.query(OAuthState).filter_by(
        state=state, provider=provider, used=False,
    ).first()
    if not row or row.expires_at < datetime.utcnow():
        return None
    row.used = True
    db.commit()
    return row


def _pkce_pair() -> tuple[str, str]:
    """Сгенерировать PKCE (code_verifier, code_challenge).
    code_verifier — random URL-safe строка 43-128 символов.
    code_challenge — base64url(sha256(verifier)) без padding.
    """
    import secrets, hashlib, base64
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def _login_or_create(db: Session, email: str, name: str, provider: str, sub: str) -> User:
    """Найти юзера по oauth_sub, или создать. НЕ линкуем по email (account takeover)."""
    email = (email or "").strip().lower()
    # 1. Точный матч oauth_sub + provider — уже линкованный OAuth-юзер
    user = db.query(User).filter_by(oauth_provider=provider, oauth_sub=sub).first()
    if user:
        return user
    # 2. Account Takeover защита: если email уже существует — НЕ линкуем автоматически.
    # Иначе злоумышленник с чужим email на Google получит доступ к чужому акку.
    # Возвращаем generic ошибку без раскрытия самого email — иначе это user-enumeration
    # через OAuth (можно собрать список зарегистрированных корпоративных адресов).
    if email:
        existing = db.query(User).filter_by(email=email).first()
        if existing:
            log.info(f"[oauth] {provider} login blocked: email collision (existing user {existing.id})")
            raise HTTPException(
                400,
                "Не удалось завершить вход. Если у вас уже есть аккаунт — войдите "
                "паролем и привяжите этот способ входа в личном кабинете.",
            )
    # 3. Создаём нового. Бонус — в копейках, единая логика с обычной регистрацией.
    if not email:
        email = f"{provider}_{sub}@oauth.local"
    _welcome_rub = float(os.getenv("WELCOME_BONUS_RUB", "50"))
    _welcome_kop = int(round(_welcome_rub * 100))
    user = User(
        email=email,
        password_hash=hash_password(uuid.uuid4().hex),  # случайный, нельзя залогиниться паролем
        name=name or email.split("@")[0],
        tokens_balance=0,
        is_active=True,
        is_verified=True,
        agreed_to_terms=True,
        referral_code=uuid.uuid4().hex[:8].upper(),
        oauth_provider=provider,
        oauth_sub=sub,
    )
    db.add(user); db.commit(); db.refresh(user)
    # Атомарный gate — даже при гонке двух callback бонус выплатится 1 раз.
    from server.billing import claim_welcome_bonus
    if claim_welcome_bonus(db, user.id, _welcome_kop):
        db.add(Transaction(user_id=user.id, type="bonus", tokens_delta=_welcome_kop,
                           description=f"Приветственный бонус: {_welcome_rub:.0f} ₽ (через {provider})"))
        db.commit()
    return user


def _frontend_redirect(db: Session, user: User) -> RedirectResponse:
    """
    Создаёт одноразовый exchange-код (TTL 60s) и редиректит фронт с этим кодом.
    Фронт меняет код на токены через POST /auth/oauth/exchange.
    Так токены не попадают ни в URL, ни в браузерную историю, ни в referrer.
    """
    code = uuid.uuid4().hex
    db.add(VerifyToken(
        user_id=user.id, token=code, purpose="oauth_exchange",
        expires_at=datetime.utcnow() + timedelta(seconds=60),
    ))
    db.commit()
    return RedirectResponse(f"{APP_URL}/?oauth_code={code}")


class OAuthExchangeRequest(BaseModel):
    code: str


@router.post("/exchange")
def oauth_exchange(req: OAuthExchangeRequest, response: Response,
                   db: Session = Depends(get_db)):
    """Обмен одноразового OAuth-кода на access/refresh токены."""
    vt = db.query(VerifyToken).filter_by(
        token=req.code, purpose="oauth_exchange", used=False,
    ).first()
    if not vt or vt.expires_at < datetime.utcnow():
        raise HTTPException(400, "Код недействителен или истёк")
    vt.used = True
    db.commit()
    user = db.query(User).filter_by(id=vt.user_id).first()
    if not user:
        raise HTTPException(404, "User not found")
    access = create_token(user.id, user.email)
    refresh = create_refresh_token(user.id, user.email)
    csrf = set_auth_cookies(response, access, refresh)
    return {
        "access": access,
        "refresh": refresh,
        "csrf_token": csrf,
        "user": _user_dict(user),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  GOOGLE
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/google/start")
def google_start(request: Request, db: Session = Depends(get_db)):
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(503, "Google OAuth не настроен")
    state = _issue_state(db, "google")
    params = urllib.parse.urlencode({
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": _redirect_uri("google"),
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "online",
        "prompt": "select_account",
        "state": state,
    })
    return RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{params}")


@router.get("/google/callback")
async def google_callback(code: str | None = None, state: str | None = None,
                          error: str | None = None,
                          db: Session = Depends(get_db)):
    if error or not code:
        return RedirectResponse(f"{APP_URL}/?oauth_error={error or 'no_code'}")
    if _consume_state(db, "google", state) is None:
        log.warning(f"[Google] invalid state: {state!r}")
        return RedirectResponse(f"{APP_URL}/?oauth_error=state")
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise HTTPException(503, "Google OAuth не настроен")
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            tok = await c.post("https://oauth2.googleapis.com/token", data={
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": _redirect_uri("google"),
                "grant_type": "authorization_code",
            })
            tok_data = tok.json()
            access_token = tok_data.get("access_token")
            if not access_token:
                # Логируем только error-код, не весь tok_data (там может быть refresh_token).
                log.error(f"[Google] token exchange failed: error={tok_data.get('error')!r}")
                return RedirectResponse(f"{APP_URL}/?oauth_error=token")
            info = await c.get("https://www.googleapis.com/oauth2/v3/userinfo",
                               headers={"Authorization": f"Bearer {access_token}"})
            u = info.json()
    except Exception as e:
        log.error(f"[Google] callback exception: {type(e).__name__}")
        return RedirectResponse(f"{APP_URL}/?oauth_error=exchange")

    sub = str(u.get("sub") or "")
    email = u.get("email") or ""
    name = u.get("name") or u.get("given_name") or ""
    if not sub:
        return RedirectResponse(f"{APP_URL}/?oauth_error=no_sub")

    user = _login_or_create(db, email, name, "google", sub)
    return _frontend_redirect(db, user)


# ══════════════════════════════════════════════════════════════════════════════
#  VK ID (новый OAuth от VK с PKCE — заменил классический oauth.vk.com в 2024)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/vk/start")
def vk_start(db: Session = Depends(get_db)):
    if not VK_CLIENT_ID:
        raise HTTPException(503, "VK OAuth не настроен")
    verifier, challenge = _pkce_pair()
    state = _issue_state(db, "vk", code_verifier=verifier)
    params = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": VK_CLIENT_ID,
        "redirect_uri": _redirect_uri("vk"),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": "email",
        "prompt": "login",
    })
    return RedirectResponse(f"https://id.vk.com/authorize?{params}")


@router.get("/vk/callback")
async def vk_callback(code: str | None = None, state: str | None = None,
                      device_id: str | None = None,
                      error: str | None = None,
                      db: Session = Depends(get_db)):
    if error or not code:
        return RedirectResponse(f"{APP_URL}/?oauth_error={error or 'no_code'}")
    state_row = _consume_state(db, "vk", state)
    if state_row is None or not state_row.code_verifier:
        log.warning(f"[VK] invalid state: {state!r}")
        return RedirectResponse(f"{APP_URL}/?oauth_error=state")
    if not VK_CLIENT_ID:
        raise HTTPException(503, "VK OAuth не настроен")
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            # VK ID допускает PKCE-only (без client_secret) — это вариант для
            # public clients. Если client_secret задан — добавляем для дополнительной
            # верификации (confidential client).
            payload = {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": state_row.code_verifier,
                "redirect_uri": _redirect_uri("vk"),
                "client_id": VK_CLIENT_ID,
                "device_id": device_id or "",
                "state": state,
            }
            if VK_CLIENT_SECRET:
                payload["client_secret"] = VK_CLIENT_SECRET
            tok = await c.post(
                "https://id.vk.com/oauth2/auth",
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            tok_data = tok.json()
            access_token = tok_data.get("access_token")
            user_id = tok_data.get("user_id")
            if not access_token:
                log.error(f"[VK ID] token exchange failed: error={tok_data.get('error')!r} desc={tok_data.get('error_description')!r}")
                return RedirectResponse(f"{APP_URL}/?oauth_error=token")
            # Профиль через VK ID user_info
            info = await c.post(
                "https://id.vk.com/oauth2/user_info",
                data={"access_token": access_token, "client_id": VK_CLIENT_ID},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            info_data = info.json()
            u = info_data.get("user") or {}
            if not user_id:
                user_id = u.get("user_id") or u.get("id")
            email = u.get("email") or ""
            first = u.get("first_name") or ""
            last = u.get("last_name") or ""
            name = f"{first} {last}".strip()
    except Exception as e:
        log.error(f"[VK ID] callback exception: {type(e).__name__}: {e}")
        return RedirectResponse(f"{APP_URL}/?oauth_error=exchange")

    if not user_id:
        return RedirectResponse(f"{APP_URL}/?oauth_error=no_user")

    user = _login_or_create(db, email, name, "vk", str(user_id))
    return _frontend_redirect(db, user)
