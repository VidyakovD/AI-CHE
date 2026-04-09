"""Auth router — registration, login, verification, password reset, email change, me."""
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import or_

from server.routes.deps import get_db, current_user, optional_user, _user_dict, _make_verify_token, _use_verify_token
from server.models import User, Transaction, VerifyToken
from server.auth import hash_password, verify_password, create_token, create_refresh_token, decode_token, generate_code, VERIFY_TTL_MINUTES
from server.security import validate_email, validate_password
from server.email_service import send_verification, send_password_reset, send_welcome
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
    user_id: int
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

    ref_code, referred_by = uuid.uuid4().hex[:8].upper(), None
    if req.referral_code:
        referrer = db.query(User).filter_by(referral_code=req.referral_code.upper()).first()
        if referrer:
            referred_by = req.referral_code.upper()
            referrer.tokens_balance += 10_000
            db.add(Transaction(user_id=referrer.id, type="bonus", tokens_delta=10_000,
                               description=f"Реферальный бонус за {email}"))

    user = User(email=email, password_hash=hash_password(req.password),
                name=req.name or email.split("@")[0], tokens_balance=0,
                agreed_to_terms=True, is_verified=False,
                referral_code=ref_code, referred_by=referred_by)
    db.add(user); db.commit(); db.refresh(user)

    code = _make_verify_token(db, user.id, "verify_email", generate_code, VERIFY_TTL_MINUTES)
    try:
        send_verification(user.email, code)
    except Exception as e:
        log.error(f"Email error: {e}")

    return {"status": "pending_verification", "user_id": user.id,
            "message": "На ваш email отправлен 6-значный код подтверждения"}


@router.post("/verify-email")
def verify_email(req: VerifyEmailRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter_by(id=req.user_id).first()
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    if user.is_verified:
        raise HTTPException(400, "Email уже подтверждён")
    if not _use_verify_token(db, user.id, req.code, "verify_email"):
        raise HTTPException(400, "Неверный или истёкший код")
    user.is_verified = True
    user.tokens_balance = 5_000
    db.add(Transaction(user_id=user.id, type="bonus", tokens_delta=5_000,
                       description="Приветственный бонус"))
    db.commit()
    try:
        send_welcome(user.email, user.name or "")
    except Exception as e:
        log.error(f"Welcome email error: {e}")
    return {"token": create_token(user.id, user.email),
            "refresh_token": create_refresh_token(user.id, user.email),
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


@router.post("/login")
def login(req: LoginRequest, db: Session = Depends(get_db)):
    email = validate_email(req.email)
    user = db.query(User).filter_by(email=email).first()
    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(401, "Неверный email или пароль")
    if not user.is_verified:
        return {"status": "pending_verification", "user_id": user.id,
                "message": "Подтвердите email. Выслать код повторно?"}
    return {"token": create_token(user.id, user.email),
            "refresh_token": create_refresh_token(user.id, user.email),
            "user": _user_dict(user)}


@router.post("/forgot-password")
def forgot_password(req: ForgotPasswordRequest, db: Session = Depends(get_db)):
    try:
        email = validate_email(req.email)
    except Exception:
        return {"message": "Если аккаунт существует — письмо отправлено", "user_id": None}
    user = db.query(User).filter_by(email=email).first()
    if user and user.is_verified:
        code = _make_verify_token(db, user.id, "reset_password", generate_code, VERIFY_TTL_MINUTES)
        try:
            send_password_reset(user.email, code)
        except Exception as e:
            log.error(f"Reset email error: {e}")
    return {"message": "Если аккаунт существует — письмо отправлено",
            "user_id": user.id if user else None}


@router.post("/reset-password")
def reset_password(req: ResetPasswordRequest, db: Session = Depends(get_db)):
    validate_password(req.new_password)
    user = db.query(User).filter_by(id=req.user_id).first()
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    if not _use_verify_token(db, user.id, req.code, "reset_password"):
        raise HTTPException(400, "Неверный или истёкший код")
    user.password_hash = hash_password(req.new_password)
    db.commit()
    return {"token": create_token(user.id, user.email),
            "refresh_token": create_refresh_token(user.id, user.email),
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
    refresh_token: str


@router.post("/refresh")
def refresh_token(req: RefreshRequest, db: Session = Depends(get_db)):
    """Обновить access токен по refresh токену."""
    payload = decode_token(req.refresh_token, require_type="refresh")
    if not payload:
        raise HTTPException(401, "Недействительный refresh токен")
    user = db.query(User).filter_by(id=int(payload["sub"])).first()
    if not user:
        raise HTTPException(401, "Пользователь не найден")
    if getattr(user, 'is_banned', False):
        raise HTTPException(403, "Аккаунт заблокирован. Обратитесь в поддержку.")
    # Return new access token AND new refresh token (rotation)
    return {
        "access_token": create_token(user.id, user.email),
        "refresh_token": create_refresh_token(user.id, user.email),
        "token_type": "bearer"
    }
