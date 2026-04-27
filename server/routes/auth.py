"""Auth router — registration, login, verification, password reset, email change, me."""
import os
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Response, Cookie, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import or_

from server.routes.deps import get_db, current_user, optional_user, _user_dict, _make_verify_token, _use_verify_token
from server.models import User, Transaction, VerifyToken
from server.auth import (
    hash_password, verify_password, create_token, create_refresh_token,
    decode_token, generate_code, VERIFY_TTL_MINUTES,
    set_auth_cookies, clear_auth_cookies,
)
from server.security import validate_email, validate_password
from server.email_service import send_verification, send_password_reset, send_welcome
from server.billing import credit_atomic, claim_welcome_bonus, claim_referral_signup_bonus
import uuid
import logging

log = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


# ── Request models ────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email: str
    password: str
    name: str | None = None
    agreed_to_terms: bool = False
    referral_code: str | None = None


class LoginRequest(BaseModel):
    email: str
    password: str


class VerifyEmailRequest(BaseModel):
    user_id: int
    code: str


class ResendVerifyRequest(BaseModel):
    user_id: int


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    # user_id оставлен для совместимости со старым фронтом, но основной путь — email
    email: str | None = None
    user_id: int | None = None
    code: str
    new_password: str


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


class ChangeEmailRequest(BaseModel):
    new_email: str
    password: str


class ConfirmChangeEmailRequest(BaseModel):
    code: str


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/register")
def register(req: RegisterRequest, db: Session = Depends(get_db)):
    if not req.agreed_to_terms:
        raise HTTPException(400, "Необходимо принять оферту")
    email = validate_email(req.email)
    validate_password(req.password)
    if db.query(User).filter_by(email=email).first():
        raise HTTPException(400, "Email уже зарегистрирован")

    ref_code = uuid.uuid4().hex[:8].upper()
    referrer_id = None
    referred_by = None
    if req.referral_code:
        referrer = db.query(User).filter_by(referral_code=req.referral_code.upper()).first()
        if referrer:
            referred_by = req.referral_code.upper()
            referrer_id = referrer.id

    user = User(email=email, password_hash=hash_password(req.password),
                name=req.name or email.split("@")[0], tokens_balance=0,
                agreed_to_terms=True, is_verified=False,
                referral_code=ref_code, referred_by=referred_by)
    db.add(user); db.commit(); db.refresh(user)

    from server.audit_log import log_action
    log_action("auth.register", user_id=user.id, target_type="user", target_id=user.id,
               details={"email_domain": email.split("@")[-1], "ref": bool(referrer_id)})

    # Реферальный бонус — atomic gate на User.referral_signup_bonus_paid_at:
    # даже при гонке двух concurrent /register с одним email (что невозможно
    # из-за UNIQUE на email, но защищаемся в depth) — бонус начислится 1 раз.
    # Сам бонус выплачивается рефереру СРАЗУ при регистрации (а не при verify).
    if referrer_id:
        _ref_bonus = int(os.getenv("REFERRAL_SIGNUP_BONUS", "1000"))
        if claim_referral_signup_bonus(db, user.id, referrer_id, _ref_bonus):
            db.add(Transaction(user_id=referrer_id, type="bonus",
                               tokens_delta=_ref_bonus,
                               description=f"Реферальный бонус за {email}"))
            db.commit()

    code = _make_verify_token(db, user.id, "verify_email", generate_code, VERIFY_TTL_MINUTES)
    try:
        send_verification(user.email, code)
    except Exception as e:
        log.error(f"Email error: {e}")

    return {"status": "pending_verification", "user_id": user.id,
            "message": "На ваш email отправлен 6-значный код подтверждения"}


@router.post("/verify-email")
def verify_email(req: VerifyEmailRequest, response: Response, db: Session = Depends(get_db)):
    user = db.query(User).filter_by(id=req.user_id).first()
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    if user.is_verified:
        raise HTTPException(400, "Email уже подтверждён")
    if not _use_verify_token(db, user.id, req.code, "verify_email"):
        raise HTTPException(400, "Неверный или истёкший код")
    user.is_verified = True
    db.commit()
    # Бонус задаётся в рублях через env, по умолчанию 50 ₽ = 5000 копеек.
    # Atomic gate на User.welcome_bonus_claimed_at — даже при гонке двух
    # /verify-email бонус начислится ровно один раз (UPDATE ... WHERE IS NULL).
    _welcome_rub = float(os.getenv("WELCOME_BONUS_RUB", "50"))
    _welcome_kop = int(round(_welcome_rub * 100))
    if claim_welcome_bonus(db, user.id, _welcome_kop):
        db.add(Transaction(user_id=user.id, type="bonus", tokens_delta=_welcome_kop,
                           description=f"Приветственный бонус: {_welcome_rub:.0f} ₽"))
        db.commit()
    db.refresh(user)
    from server.audit_log import log_action
    log_action("auth.verify_email", user_id=user.id, target_type="user", target_id=user.id,
               details={"welcome_bonus_kop": _welcome_kop})
    try:
        send_welcome(user.email, user.name or "")
    except Exception as e:
        log.error(f"Welcome email error: {e}")
    access = create_token(user.id, user.email)
    refresh = create_refresh_token(user.id, user.email)
    csrf = set_auth_cookies(response, access, refresh)
    return {"token": access, "refresh_token": refresh, "csrf_token": csrf,
            "user": _user_dict(user)}


@router.post("/resend-verify")
def resend_verify(req: ResendVerifyRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter_by(id=req.user_id).first()
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    if user.is_verified:
        raise HTTPException(400, "Email уже подтверждён")
    code = _make_verify_token(db, user.id, "verify_email", generate_code, VERIFY_TTL_MINUTES)
    try:
        send_verification(user.email, code)
    except Exception as e:
        log.error(f"Resend error: {e}")
    return {"message": "Код повторно отправлен"}


# Фиктивный bcrypt-хеш для константного времени при несуществующем юзере.
# Значение ни с чем не совпадёт, но verify_password всё равно проверит и займёт ~250мс.
_DUMMY_BCRYPT = "$2b$12$C6UzMDM.H6dfI/f/IKyt7.Re3vdDe4xD3Z3iVfvjxQ0Pu4sPxc7/e"


@router.post("/login")
def login(req: LoginRequest, response: Response, request: Request,
          db: Session = Depends(get_db)):
    email = validate_email(req.email)
    user = db.query(User).filter_by(email=email).first()
    # Защита от timing-based account enumeration:
    # всегда вызываем verify_password, даже если юзера нет (bcrypt на dummy хеше)
    pw_hash = user.password_hash if user else _DUMMY_BCRYPT
    pw_ok = verify_password(req.password, pw_hash)
    if not user or not pw_ok:
        raise HTTPException(401, "Неверный email или пароль")
    if not user.is_verified:
        return {"status": "pending_verification", "user_id": user.id,
                "message": "Подтвердите email. Выслать код повторно?"}

    # Security alert: вход с нового IP — уведомляем юзера на email.
    # Не блокируем login — это только уведомление. Не шлём при первом входе
    # (last_login_ip == None) и при повторе с того же IP.
    try:
        from server.security import _get_client_ip
        ip = _get_client_ip(request)
        prev_ip = user.last_login_ip
        now_utc = datetime.utcnow()
        if prev_ip and ip and ip != "unknown" and ip != prev_ip:
            try:
                from server.email_service import send_login_alert
                send_login_alert(user.email, user.name or "",
                                 ip, now_utc.strftime("%Y-%m-%d %H:%M"))
            except Exception as e:
                log.warning(f"login-alert email failed: {type(e).__name__}")
            from server.audit_log import log_action
            log_action("auth.login_new_ip", user_id=user.id, target_type="user",
                       target_id=user.id, level="warn",
                       details={"prev_ip_hash": str(hash(prev_ip))[-6:],
                                "new_ip_hash": str(hash(ip))[-6:]})
        user.last_login_ip = ip
        user.last_login_at = now_utc
        db.commit()
    except Exception as e:
        log.warning(f"login-alert flow failed: {type(e).__name__}")

    access = create_token(user.id, user.email)
    refresh = create_refresh_token(user.id, user.email)
    csrf = set_auth_cookies(response, access, refresh)
    return {"token": access, "refresh_token": refresh, "csrf_token": csrf,
            "user": _user_dict(user)}


@router.post("/forgot-password")
def forgot_password(req: ForgotPasswordRequest, db: Session = Depends(get_db)):
    # Константный ответ — не раскрывает существование аккаунта
    try:
        email = validate_email(req.email)
    except Exception:
        return {"message": "Если аккаунт существует — письмо отправлено"}
    user = db.query(User).filter_by(email=email).first()
    if user and user.is_verified:
        code = _make_verify_token(db, user.id, "reset_password", generate_code, VERIFY_TTL_MINUTES)
        try:
            send_password_reset(user.email, code)
        except Exception as e:
            log.error(f"Reset email error: {e}")
    # user_id НЕ возвращаем чтобы не утечь факт существования аккаунта.
    # Фронт для сброса пароля принимает email + code (не user_id).
    return {"message": "Если аккаунт существует — письмо отправлено"}


@router.post("/reset-password")
def reset_password(req: ResetPasswordRequest, response: Response,
                   db: Session = Depends(get_db)):
    validate_password(req.new_password)
    user = None
    if req.email:
        try:
            email = validate_email(req.email)
            user = db.query(User).filter_by(email=email).first()
        except Exception:
            pass
    if not user and req.user_id:
        # legacy-путь для старых клиентов
        user = db.query(User).filter_by(id=req.user_id).first()
    # Generic-ошибка не раскрывает, существует ли email
    if not user or not _use_verify_token(db, user.id, req.code, "reset_password"):
        raise HTTPException(400, "Неверный или истёкший код")
    user.password_hash = hash_password(req.new_password)
    db.commit()
    access = create_token(user.id, user.email)
    refresh = create_refresh_token(user.id, user.email)
    csrf = set_auth_cookies(response, access, refresh)
    return {"token": access, "refresh_token": refresh, "csrf_token": csrf,
            "user": _user_dict(user)}


@router.post("/change-password")
def change_password(req: ChangePasswordRequest, user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    validate_password(req.new_password)
    if not verify_password(req.old_password, user.password_hash):
        raise HTTPException(400, "Неверный текущий пароль")
    user.password_hash = hash_password(req.new_password)
    db.commit()
    return {"message": "Пароль успешно изменён"}


@router.post("/change-email")
def change_email(req: ChangeEmailRequest, user: User = Depends(current_user),
                 db: Session = Depends(get_db)):
    new_email = validate_email(req.new_email)
    if not verify_password(req.password, user.password_hash):
        raise HTTPException(401, "Неверный пароль")
    if db.query(User).filter_by(email=new_email).first():
        raise HTTPException(400, "Email уже используется")
    code = _make_verify_token(db, user.id, f"change_email:{new_email}", generate_code, VERIFY_TTL_MINUTES)
    try:
        send_verification(new_email, code)
    except Exception as e:
        log.error(f"Change email error: {e}")
    return {"message": "Код подтверждения отправлен на новый email"}


@router.post("/change-email/confirm")
def change_email_confirm(req: ConfirmChangeEmailRequest, user: User = Depends(current_user),
                         db: Session = Depends(get_db)):
    vt = db.query(VerifyToken).filter(
        VerifyToken.user_id == user.id, VerifyToken.token == req.code,
        VerifyToken.purpose.like("change_email:%"), VerifyToken.used == False,
        VerifyToken.expires_at > datetime.utcnow()).first()
    if not vt:
        raise HTTPException(400, "Неверный или истёкший код")
    new_email = vt.purpose.split(":", 1)[1]
    vt.used = True
    db_user = db.query(User).filter_by(id=user.id).first()
    db_user.email = new_email
    db.commit()
    return {"token": create_token(user.id, new_email), "user": _user_dict(db_user)}


@router.get("/me")
def me(user: User = Depends(current_user)):
    return {"user": _user_dict(user)}


class RefreshRequest(BaseModel):
    refresh_token: str | None = None  # legacy-клиенты шлют в body, новые — через cookie


@router.post("/refresh")
def refresh_token(req: RefreshRequest, response: Response,
                  db: Session = Depends(get_db),
                  refresh_cookie: str | None = Cookie(None, alias="refresh_token")):
    """Обновить access токен по refresh токену.
    Принимает refresh_token из body (legacy) или из cookie (новый flow)."""
    rt = (req.refresh_token if req and req.refresh_token else None) or refresh_cookie
    if not rt:
        raise HTTPException(401, "Refresh токен отсутствует")
    payload = decode_token(rt, require_type="refresh")
    if not payload:
        raise HTTPException(401, "Недействительный refresh токен")
    user = db.query(User).filter_by(id=int(payload["sub"])).first()
    if not user:
        raise HTTPException(401, "Пользователь не найден")
    if getattr(user, 'is_banned', False):
        raise HTTPException(403, "Аккаунт заблокирован. Обратитесь в поддержку.")
    # Return new access token AND new refresh token (rotation)
    new_access = create_token(user.id, user.email)
    new_refresh = create_refresh_token(user.id, user.email)
    csrf = set_auth_cookies(response, new_access, new_refresh)
    return {
        "access_token": new_access,
        "refresh_token": new_refresh,
        "csrf_token": csrf,
        "token_type": "bearer",
    }


@router.post("/logout")
def logout(response: Response):
    """Стирает auth cookies. JWT в Authorization-header будет работать
    до своего exp — ничего нельзя revoke server-side без revocation list,
    но cookie-based сессия точно завершится."""
    clear_auth_cookies(response)
    return {"status": "logged_out"}
