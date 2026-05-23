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
    _new_jti, register_refresh_jti, revoke_refresh_jti,
    revoke_all_refresh_jtis, is_refresh_jti_active,
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
    # 152-ФЗ: согласие на маркетинговую рассылку — отдельно. По умолчанию
    # False (предзаполнять нельзя), юзер сам отмечает чекбокс.
    marketing_consent: bool = False
    referral_code: str | None = None


class LoginRequest(BaseModel):
    email: str
    password: str
    totp_code: str | None = None  # для админов с 2FA


class VerifyEmailRequest(BaseModel):
    user_id: int
    code: str


class ResendVerifyRequest(BaseModel):
    # Поддерживаем оба пути:
    #  - email (новый, не палит enumeration — отвечаем одинаково для несущ. юзера)
    #  - user_id (legacy, для старого фронта во время grace-period)
    email: str | None = None
    user_id: int | None = None


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
                marketing_consent=bool(req.marketing_consent),
                marketing_consent_at=(datetime.utcnow()
                                       if req.marketing_consent else None),
                referral_code=ref_code, referred_by=referred_by)
    db.add(user); db.commit(); db.refresh(user)

    from server.audit_log import log_action
    log_action("auth.register", user_id=user.id, target_type="user", target_id=user.id,
               details={"email_domain": email.split("@")[-1], "ref": bool(referrer_id),
                        "marketing": bool(req.marketing_consent)})

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


def _seed_demo_data(db: Session, user_id: int, user_name: str) -> None:
    """Создаёт минимальные демо-данные новому юзеру:
      • welcome-заметку (KnowledgeFile owner_type='user')
      • событие «познакомиться с Че» на завтра 12:00 (LocalCalendarEvent)
    Финансы НЕ сидируем — это сильно зависит от валюты юзера и
    приватные данные нежелательны без явного согласия.

    Best-effort: любые ошибки только логируются, не raise.
    """
    from datetime import datetime as _dt, timedelta as _td
    from server.models import LocalCalendarEvent
    name = (user_name or "").strip() or "друг"

    # 1. Welcome-заметка через knowledge — попадёт в RAG, Че её увидит в чате
    try:
        from server.knowledge import add_file
        import secrets as _sec
        note_text = (
            f"Привет, {name}! Это твоя первая заметка — пример того как "
            "работает Че.\n\n"
            "💡 Что важно знать:\n"
            "• Любую заметку из /notes.html я (Че) автоматически вижу в чате — "
            "можешь спросить «что я записал про X», и я найду.\n"
            "• В /finance.html импортируешь CSV из банка — я отвечу на "
            "вопросы про расходы.\n"
            "• В /calendar.html подключи Google или Яндекс — я буду в курсе "
            "встреч и напомню.\n"
            "• Скажи мне «внеси в календарь завтра в 15:00» — я сам создам "
            "событие.\n\n"
            "Если эта заметка не нужна — удали её в /notes.html."
        )
        add_file(
            owner_type="user", owner_id=user_id, user_id=user_id,
            name="👋 Добро пожаловать в Че",
            path=f"/uploads/notes/welcome-{_sec.token_urlsafe(8)}.txt",
            mime="text/x-note",
            size=len(note_text.encode("utf-8")),
            content_text=note_text,
            tags="welcome,onboarding",
            skip_embeddings=False,
        )
    except Exception as e:
        log.warning(f"[seed-demo] note skipped: {e}")

    # 2. Демо-событие в календаре «познакомиться с Че» на завтра 12:00 UTC
    try:
        tomorrow = (_dt.utcnow() + _td(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
        ev = LocalCalendarEvent(
            user_id=user_id,
            title="🎯 Познакомиться с возможностями Че",
            start=tomorrow,
            end=tomorrow + _td(minutes=30),
            description=(
                "30 минут на изучение:\n"
                "• /agents-modular.html — каталог из 26 модулей ИИ-агентов\n"
                "• /sites.html — сайт под ключ за 1500 ₽\n"
                "• /proposals.html — КП с e-подписью\n"
                "• Главное — начни диалог с Че, скажи «помоги настроить»."
            ),
            location="",
        )
        db.add(ev)
        db.commit()
    except Exception as e:
        log.warning(f"[seed-demo] event skipped: {e}")
        try: db.rollback()
        except Exception: pass


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
    # Демо-данные для онбординга: одна welcome-заметка + одно событие
    # «познакомиться с Че» в календаре. Без них новый юзер видит пустые
    # экраны (/notes.html, /calendar.html, /finance.html) и не понимает
    # как использовать. Best-effort — никогда не блокируем regular flow.
    try:
        _seed_demo_data(db, user.id, user.name or "")
    except Exception as e:
        log.warning(f"[verify-email] demo data seed skipped: {type(e).__name__}: {e}")
    access = create_token(user.id, user.email)
    rt_jti = _new_jti()
    refresh = create_refresh_token(user.id, user.email, jti=rt_jti)
    register_refresh_jti(db, user, rt_jti)
    csrf = set_auth_cookies(response, access, refresh)
    return {"token": access, "refresh_token": refresh, "csrf_token": csrf,
            "user": _user_dict(user)}


@router.post("/resend-verify")
def resend_verify(req: ResendVerifyRequest, db: Session = Depends(get_db)):
    """Повторно выслать код подтверждения email.

    Не палит enumeration: всегда отвечаем 200 «Код повторно отправлен» вне
    зависимости от того, существует ли юзер и подтверждён ли уже email.
    Если есть и не подтверждён — высылаем; иначе ничего не делаем.
    """
    user = None
    if req.email:
        try:
            email = validate_email(req.email)
            user = db.query(User).filter_by(email=email).first()
        except Exception:
            user = None
    elif req.user_id:
        user = db.query(User).filter_by(id=req.user_id).first()
    if user and not user.is_verified:
        try:
            code = _make_verify_token(db, user.id, "verify_email", generate_code,
                                       VERIFY_TTL_MINUTES)
            send_verification(user.email, code)
        except Exception as e:
            log.error(f"Resend error: {e}")
    return {"message": "Если email зарегистрирован и не подтверждён — код выслан повторно."}


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
        # Не возвращаем user.id — это утечка enumeration: атакер с верным
        # паролем (например, переиспользованным с другого сервиса) увидит
        # внутренний ID юзера. /resend-verify теперь принимает email
        # (см. ResendVerifyRequest), а не user_id.
        return {"status": "pending_verification",
                "message": "Подтвердите email. Выслать код повторно?"}

    # 2FA для админов: если у юзера включён TOTP, требуем код.
    # Возвращаем НЕ-ошибку а специальный статус — фронт покажет поле кода
    # без перелогинивания (более UX-friendly чем 401 с "wrong code").
    from server.security import ADMIN_EMAILS
    is_admin_user = (user.email or "").lower() in ADMIN_EMAILS
    if is_admin_user and user.totp_enabled and user.totp_secret:
        provided = (req.totp_code or "").strip().replace(" ", "")
        if not provided:
            return {"status": "totp_required",
                    "message": "Введите 6-значный код из приложения 2FA"}
        if not provided.isdigit() or len(provided) != 6:
            raise HTTPException(401, "Код 2FA должен быть 6 цифр")
        try:
            import pyotp
            totp = pyotp.TOTP(user.totp_secret)
            if not totp.verify(provided, valid_window=1):
                from server.audit_log import log_action
                log_action("auth.2fa_failed", user_id=user.id, level="warn",
                           target_type="user", target_id=str(user.id))
                raise HTTPException(401, "Неверный код 2FA")
        except HTTPException:
            raise
        except Exception as e:
            log.error(f"2FA verify error: {type(e).__name__}: {e}")
            raise HTTPException(500, "Ошибка проверки 2FA")

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
    rt_jti = _new_jti()
    refresh = create_refresh_token(user.id, user.email, jti=rt_jti)
    register_refresh_jti(db, user, rt_jti)
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
    # Reset password — security-инцидент: revoke ВСЕ существующие refresh
    # сессии. Иначе атакер с украденным refresh продолжит работать после
    # того как юзер сменил пароль.
    revoke_all_refresh_jtis(db, user)
    db.commit()
    access = create_token(user.id, user.email)
    rt_jti = _new_jti()
    refresh = create_refresh_token(user.id, user.email, jti=rt_jti)
    register_refresh_jti(db, user, rt_jti)
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
    # Юзер часто меняет пароль из-за подозрения на компрометацию. Если у
    # атакера остался валидный refresh-токен — он сохранит доступ ещё 30
    # дней. Ревокаем все сессии — на других устройствах придётся залогиниться
    # заново. /reset-password уже это делает; теперь и /change-password тоже.
    revoke_all_refresh_jtis(db, user)
    db.commit()
    try:
        from server.audit_log import log_action
        log_action("auth.password_changed", user_id=user.id,
                   target_type="user", target_id=user.id, level="warn",
                   details={"sessions_revoked": True})
    except Exception:
        pass
    return {"message": "Пароль успешно изменён. Все остальные сессии разлогинены."}


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
    # Email — часто канал восстановления пароля. После смены ревокаем все
    # refresh-сессии: если старый email скомпрометирован, атакер не сможет
    # дальше использовать стянутый refresh-токен.
    revoke_all_refresh_jtis(db, db_user)
    db.commit()
    try:
        from server.audit_log import log_action
        log_action("auth.email_changed", user_id=user.id,
                   target_type="user", target_id=user.id, level="warn",
                   details={"new_email": new_email, "sessions_revoked": True})
    except Exception:
        pass
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
    # Single-use: проверяем что jti токена ещё активен. Если jti уже был
    # использован (украденный токен после rotation, или повторный submit) —
    # security-инцидент: revoke ВСЕ jti юзера, чтобы и легитимная сессия,
    # и атакер потеряли доступ. Юзер увидит логаут на всех устройствах.
    old_jti = payload.get("jti")
    if not is_refresh_jti_active(user, old_jti):
        log.warning(f"[auth] refresh jti reuse detected for user {user.id} — revoking all sessions")
        revoke_all_refresh_jtis(db, user)
        try:
            from server.audit_log import log_action
            log_action("auth.refresh_reuse", user_id=user.id, target_type="user",
                        target_id=user.id, level="critical", success=False,
                        details={"jti_prefix": (old_jti or "")[:8]})
        except Exception:
            pass
        raise HTTPException(401, "Refresh-токен уже был использован. Войдите заново.")
    # Rotation: убираем старый jti (если был зарегистрирован — для legacy
    # токенов без registered jti revoke вернёт False, но это OK — grace period).
    if old_jti:
        revoke_refresh_jti(db, user, old_jti)
    # Выдаём новые токены и регистрируем новый jti
    new_access = create_token(user.id, user.email)
    new_jti = _new_jti()
    new_refresh = create_refresh_token(user.id, user.email, jti=new_jti)
    register_refresh_jti(db, user, new_jti)
    csrf = set_auth_cookies(response, new_access, new_refresh)
    return {
        "access_token": new_access,
        "refresh_token": new_refresh,
        "csrf_token": csrf,
        "token_type": "bearer",
    }


@router.post("/logout")
def logout(response: Response,
           db: Session = Depends(get_db),
           refresh_cookie: str | None = Cookie(None, alias="refresh_token")):
    """Стирает auth cookies + revoke текущий refresh jti (server-side).
    Access-токен в Authorization-header будет работать до своего exp
    (1 день), но refresh — нет: повторный refresh с этого устройства
    выдаст 401 «Refresh-токен уже был использован»."""
    if refresh_cookie:
        try:
            payload = decode_token(refresh_cookie, require_type="refresh")
            if payload:
                user = db.query(User).filter_by(id=int(payload.get("sub", 0))).first()
                if user and payload.get("jti"):
                    revoke_refresh_jti(db, user, payload["jti"])
        except Exception:
            pass
    clear_auth_cookies(response)
    return {"status": "logged_out"}
