"""
OAuth регистрация/вход через Google и ВКонтакте.

Flow:
  GET /auth/oauth/{provider}/start?return_url=...  — редирект на consent-экран
  GET /auth/oauth/{provider}/callback?code=...     — обмен code → token → user
                                                     → создание/логин → редирект
                                                     назад на APP_URL с токеном
"""
import os, uuid, logging, urllib.parse
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
import httpx

from server.routes.deps import get_db
from server.models import User, Transaction
from server.auth import create_token, create_refresh_token, hash_password

log = logging.getLogger("oauth")
router = APIRouter(prefix="/auth/oauth", tags=["auth"])

APP_URL = os.getenv("APP_URL", "https://aiche.ru")

GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
VK_CLIENT_ID         = os.getenv("VK_CLIENT_ID", "")
VK_CLIENT_SECRET     = os.getenv("VK_CLIENT_SECRET", "")


def _redirect_uri(provider: str) -> str:
    return f"{APP_URL}/auth/oauth/{provider}/callback"


def _login_or_create(db: Session, email: str, name: str, provider: str, sub: str) -> User:
    """Найти юзера по oauth_sub, или по email, или создать."""
    email = (email or "").strip().lower()
    # 1. Точный матч oauth_sub + provider
    user = db.query(User).filter_by(oauth_provider=provider, oauth_sub=sub).first()
    if user:
        return user
    # 2. По email (линкуем OAuth)
    if email:
        user = db.query(User).filter_by(email=email).first()
        if user:
            user.oauth_provider = provider
            user.oauth_sub = sub
            user.is_verified = True
            db.commit()
            return user
    # 3. Создаём нового
    if not email:
        email = f"{provider}_{sub}@oauth.local"
    user = User(
        email=email,
        password_hash=hash_password(uuid.uuid4().hex),  # случайный, нельзя залогиниться паролем
        name=name or email.split("@")[0],
        tokens_balance=5_000,
        is_active=True,
        is_verified=True,
        agreed_to_terms=True,
        referral_code=uuid.uuid4().hex[:8].upper(),
        oauth_provider=provider,
        oauth_sub=sub,
    )
    db.add(user); db.commit(); db.refresh(user)
    db.add(Transaction(user_id=user.id, type="bonus", tokens_delta=5_000,
                       description=f"Приветственный бонус (через {provider})"))
    db.commit()
    return user


def _frontend_redirect(user: User) -> RedirectResponse:
    """Редирект на главную с токеном в URL-фрагменте."""
    tok = create_token(user.id, user.email)
    refresh = create_refresh_token(user.id, user.email)
    # Токены передаём во фрагменте (не в query) — чтобы не светить в access логах
    params = urllib.parse.urlencode({
        "access": tok, "refresh": refresh, "email": user.email,
    })
    return RedirectResponse(f"{APP_URL}/#oauth={params}")


# ══════════════════════════════════════════════════════════════════════════════
#  GOOGLE
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/google/start")
def google_start(request: Request):
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(503, "Google OAuth не настроен")
    params = urllib.parse.urlencode({
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": _redirect_uri("google"),
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "online",
        "prompt": "select_account",
    })
    return RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{params}")


@router.get("/google/callback")
async def google_callback(code: str | None = None, error: str | None = None,
                          db: Session = Depends(get_db)):
    if error or not code:
        return RedirectResponse(f"{APP_URL}/?oauth_error={error or 'no_code'}")
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
                log.error(f"[Google] no token: {tok_data}")
                return RedirectResponse(f"{APP_URL}/?oauth_error=token")
            info = await c.get("https://www.googleapis.com/oauth2/v3/userinfo",
                               headers={"Authorization": f"Bearer {access_token}"})
            u = info.json()
    except Exception as e:
        log.error(f"[Google] {e}")
        return RedirectResponse(f"{APP_URL}/?oauth_error=exchange")

    sub = str(u.get("sub") or "")
    email = u.get("email") or ""
    name = u.get("name") or u.get("given_name") or ""
    if not sub:
        return RedirectResponse(f"{APP_URL}/?oauth_error=no_sub")

    user = _login_or_create(db, email, name, "google", sub)
    return _frontend_redirect(user)


# ══════════════════════════════════════════════════════════════════════════════
#  VK (OAuth 2.0 classic)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/vk/start")
def vk_start():
    if not VK_CLIENT_ID:
        raise HTTPException(503, "VK OAuth не настроен")
    params = urllib.parse.urlencode({
        "client_id": VK_CLIENT_ID,
        "redirect_uri": _redirect_uri("vk"),
        "response_type": "code",
        "scope": "email",
        "v": "5.131",
    })
    return RedirectResponse(f"https://oauth.vk.com/authorize?{params}")


@router.get("/vk/callback")
async def vk_callback(code: str | None = None, error: str | None = None,
                      db: Session = Depends(get_db)):
    if error or not code:
        return RedirectResponse(f"{APP_URL}/?oauth_error={error or 'no_code'}")
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
                log.error(f"[VK] no token: {tok_data}")
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
        log.error(f"[VK] {e}")
        return RedirectResponse(f"{APP_URL}/?oauth_error=exchange")

    user = _login_or_create(db, email, name, "vk", str(user_id))
    return _frontend_redirect(user)
