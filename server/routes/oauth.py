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
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
import httpx

from server.routes.deps import get_db, _user_dict
from server.models import User, Transaction, VerifyToken, OAuthState
from server.auth import create_token, create_refresh_token, hash_password

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


def _issue_state(db: Session, provider: str) -> str:
    """Сгенерировать одноразовый CSRF-state, сохранить в БД, вернуть."""
    state = uuid.uuid4().hex
    db.add(OAuthState(
        state=state, provider=provider,
        expires_at=datetime.utcnow() + timedelta(seconds=_STATE_TTL_SEC),
    ))
    db.commit()
    return state


def _consume_state(db: Session, provider: str, state: str | None) -> bool:
    """
    Проверить state-параметр и пометить как использованный.
    True если state валидный, False иначе.
    """
    if not state or not isinstance(state, str) or len(state) != 32:
        return False
    row = db.query(OAuthState).filter_by(
        state=state, provider=provider, used=False,
    ).first()
    if not row or row.expires_at < datetime.utcnow():
        return False
    row.used = True
    db.commit()
    return True


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
def oauth_exchange(req: OAuthExchangeRequest, db: Session = Depends(get_db)):
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
    return {
        "access": create_token(user.id, user.email),
        "refresh": create_refresh_token(user.id, user.email),
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
    if not _consume_state(db, "google", state):
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
#  VK (OAuth 2.0 classic)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/vk/start")
def vk_start(db: Session = Depends(get_db)):
    if not VK_CLIENT_ID:
        raise HTTPException(503, "VK OAuth не настроен")
    state = _issue_state(db, "vk")
    params = urllib.parse.urlencode({
        "client_id": VK_CLIENT_ID,
        "redirect_uri": _redirect_uri("vk"),
        "response_type": "code",
        "scope": "email",
        "v": "5.131",
        "state": state,
    })
    return RedirectResponse(f"https://oauth.vk.com/authorize?{params}")


@router.get("/vk/callback")
async def vk_callback(code: str | None = None, state: str | None = None,
                      error: str | None = None,
                      db: Session = Depends(get_db)):
    if error or not code:
        return RedirectResponse(f"{APP_URL}/?oauth_error={error or 'no_code'}")
    if not _consume_state(db, "vk", state):
        log.warning(f"[VK] invalid state: {state!r}")
        return RedirectResponse(f"{APP_URL}/?oauth_error=state")
    if not VK_CLIENT_ID or not VK_CLIENT_SECRET:
        raise HTTPException(503, "VK OAuth не настроен")
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            tok = await c.get("https://oauth.vk.com/access_token", params={
                "client_id": VK_CLIENT_ID,
                "client_secret": VK_CLIENT_SECRET,
                "redirect_uri": _redirect_uri("vk"),
                "code": code,
            })
            tok_data = tok.json()
            access_token = tok_data.get("access_token")
            user_id = tok_data.get("user_id")
            email = tok_data.get("email", "")
            if not access_token or not user_id:
                # Не логируем tok_data целиком — там может быть access_token при частичном ответе.
                log.error(f"[VK] token exchange failed: error={tok_data.get('error')!r}")
                return RedirectResponse(f"{APP_URL}/?oauth_error=token")
            # Имя через users.get
            info = await c.get("https://api.vk.com/method/users.get", params={
                "user_ids": user_id, "fields": "first_name,last_name",
                "access_token": access_token, "v": "5.131",
            })
            arr = info.json().get("response") or []
            name = ""
            if arr:
                u0 = arr[0]
                name = f"{u0.get('first_name','')} {u0.get('last_name','')}".strip()
    except Exception as e:
        log.error(f"[VK] callback exception: {type(e).__name__}")
        return RedirectResponse(f"{APP_URL}/?oauth_error=exchange")

    user = _login_or_create(db, email, name, "vk", str(user_id))
    return _frontend_redirect(db, user)
