import os, json, uuid, shutil, logging
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, UploadFile, File, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session
from dotenv import load_dotenv

from db import SessionLocal, engine
import models
from models import User, Message, Subscription, Transaction, VerifyToken
from auth import (hash_password, verify_password, create_token, decode_token,
                  generate_code, VERIFY_TTL_MINUTES)
from ai import generate_response, get_token_cost, resolve_model
from payments import create_payment, check_payment, get_plan, PLANS
from email_service import send_verification, send_password_reset, send_welcome

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="Obsidian AI")
UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# ─── DB / Auth deps ───────────────────────────────────────────────────────────

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

def current_user(authorization: str = Header(None), db: Session = Depends(get_db)) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Not authenticated")
    payload = decode_token(authorization[7:])
    if not payload:
        raise HTTPException(401, "Invalid or expired token")
    user = db.query(User).filter_by(id=int(payload["sub"])).first()
    if not user:
        raise HTTPException(401, "User not found")
    return user

def optional_user(authorization: str = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        return None
    payload = decode_token(authorization[7:])
    if not payload:
        return None
    return db.query(User).filter_by(id=int(payload["sub"])).first()

# ─── helpers ──────────────────────────────────────────────────────────────────

def _user_dict(u: User) -> dict:
    return {
        "id": u.id, "email": u.email, "name": u.name,
        "avatar_url": u.avatar_url, "tokens_balance": u.tokens_balance,
        "is_verified": u.is_verified, "referral_code": u.referral_code,
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }

def _sub_dict(s: Subscription) -> dict:
    return {
        "id": s.id, "plan": s.plan,
        "tokens_total": s.tokens_total, "tokens_used": s.tokens_used,
        "tokens_left": s.tokens_total - s.tokens_used,
        "price_rub": s.price_rub, "status": s.status,
        "started_at": s.started_at.isoformat() if s.started_at else None,
        "expires_at": s.expires_at.isoformat() if s.expires_at else None,
    }

def _tx_dict(t: Transaction) -> dict:
    return {
        "id": t.id, "type": t.type, "amount_rub": t.amount_rub,
        "tokens_delta": t.tokens_delta, "description": t.description,
        "model": t.model,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }

def _make_verify_token(db: Session, user_id: int, purpose: str) -> str:
    # Инвалидируем старые токены того же назначения
    db.query(VerifyToken).filter_by(user_id=user_id, purpose=purpose, used=False).update({"used": True})
    code = generate_code(6)
    vt = VerifyToken(
        user_id=user_id,
        token=code,
        purpose=purpose,
        expires_at=datetime.utcnow() + timedelta(minutes=VERIFY_TTL_MINUTES),
    )
    db.add(vt)
    db.commit()
    return code

def _use_verify_token(db: Session, user_id: int, code: str, purpose: str) -> bool:
    vt = db.query(VerifyToken).filter_by(
        user_id=user_id, token=code, purpose=purpose, used=False
    ).first()
    if not vt or vt.expires_at < datetime.utcnow():
        return False
    vt.used = True
    db.commit()
    return True

# ═══════════════════════════════════════════════════════════════════════════════
# AUTH
# ═══════════════════════════════════════════════════════════════════════════════

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


@app.post("/auth/register")
def register(req: RegisterRequest, db: Session = Depends(get_db)):
    if not req.agreed_to_terms:
        raise HTTPException(400, "Необходимо принять оферту")
    if len(req.password) < 8:
        raise HTTPException(400, "Пароль должен быть не менее 8 символов")
    if db.query(User).filter_by(email=req.email.lower()).first():
        raise HTTPException(400, "Email уже зарегистрирован")

    ref_code   = uuid.uuid4().hex[:8].upper()
    referred_by = None

    if req.referral_code:
        referrer = db.query(User).filter_by(referral_code=req.referral_code.upper()).first()
        if referrer:
            referred_by = req.referral_code.upper()
            referrer.tokens_balance += 10_000
            db.add(Transaction(
                user_id=referrer.id, type="bonus", tokens_delta=10_000,
                description=f"Реферальный бонус за приглашение {req.email}"
            ))

    user = User(
        email=req.email.lower(),
        password_hash=hash_password(req.password),
        name=req.name or req.email.split("@")[0],
        tokens_balance=0,          # токены начислим после верификации
        agreed_to_terms=True,
        is_verified=False,
        referral_code=ref_code,
        referred_by=referred_by,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    # отправляем код
    code = _make_verify_token(db, user.id, "verify_email")
    try:
        send_verification(user.email, code)
    except Exception as e:
        log.error(f"Email send error: {e}")

    return {"status": "pending_verification", "user_id": user.id,
            "message": "На ваш email отправлен 6-значный код подтверждения"}


@app.post("/auth/verify-email")
def verify_email(req: VerifyEmailRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter_by(id=req.user_id).first()
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    if user.is_verified:
        raise HTTPException(400, "Email уже подтверждён")
    if not _use_verify_token(db, user.id, req.code, "verify_email"):
        raise HTTPException(400, "Неверный или истёкший код")

    user.is_verified = True
    user.tokens_balance = 5_000   # приветственный бонус
    db.add(Transaction(
        user_id=user.id, type="bonus", tokens_delta=5_000,
        description="Приветственный бонус за регистрацию"
    ))
    db.commit()

    try:
        send_welcome(user.email, user.name or "")
    except Exception as e:
        log.error(f"Welcome email error: {e}")

    return {"token": create_token(user.id, user.email), "user": _user_dict(user)}


@app.post("/auth/resend-verify")
def resend_verify(req: ResendVerifyRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter_by(id=req.user_id).first()
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    if user.is_verified:
        raise HTTPException(400, "Email уже подтверждён")
    code = _make_verify_token(db, user.id, "verify_email")
    try:
        send_verification(user.email, code)
    except Exception as e:
        log.error(f"Resend error: {e}")
    return {"message": "Код повторно отправлен"}


@app.post("/auth/login")
def login(req: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter_by(email=req.email.lower()).first()
    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(401, "Неверный email или пароль")
    if not user.is_verified:
        return {"status": "pending_verification", "user_id": user.id,
                "message": "Подтвердите email. Выслать код повторно?"}
    return {"token": create_token(user.id, user.email), "user": _user_dict(user)}


@app.post("/auth/forgot-password")
def forgot_password(req: ForgotPasswordRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter_by(email=req.email.lower()).first()
    # Не раскрываем существование пользователя
    if user and user.is_verified:
        code = _make_verify_token(db, user.id, "reset_password")
        try:
            send_password_reset(user.email, code)
        except Exception as e:
            log.error(f"Reset email error: {e}")
    return {"message": "Если аккаунт существует — письмо отправлено", "user_id": user.id if user else None}


@app.post("/auth/reset-password")
def reset_password(req: ResetPasswordRequest, db: Session = Depends(get_db)):
    if len(req.new_password) < 8:
        raise HTTPException(400, "Пароль должен быть не менее 8 символов")
    user = db.query(User).filter_by(id=req.user_id).first()
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    if not _use_verify_token(db, user.id, req.code, "reset_password"):
        raise HTTPException(400, "Неверный или истёкший код")
    user.password_hash = hash_password(req.new_password)
    db.commit()
    return {"token": create_token(user.id, user.email), "user": _user_dict(user)}


@app.post("/auth/change-password")
def change_password(req: ChangePasswordRequest,
                    user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    if len(req.new_password) < 8:
        raise HTTPException(400, "Пароль должен быть не менее 8 символов")
    db_user = db.query(User).filter_by(id=user.id).first()
    if not verify_password(req.old_password, db_user.password_hash):
        raise HTTPException(400, "Неверный текущий пароль")
    db_user.password_hash = hash_password(req.new_password)
    db.commit()
    return {"status": "ok"}


@app.post("/auth/change-email/request")
def change_email_request(req: ChangeEmailRequest,
                         user: User = Depends(current_user),
                         db: Session = Depends(get_db)):
    db_user = db.query(User).filter_by(id=user.id).first()
    if not verify_password(req.password, db_user.password_hash):
        raise HTTPException(400, "Неверный пароль")
    if db.query(User).filter_by(email=req.new_email.lower()).first():
        raise HTTPException(400, "Этот email уже занят")
    # Сохраняем новый email во временное поле через токен description
    code = generate_code(6)
    vt = VerifyToken(
        user_id=user.id,
        token=code,
        purpose=f"change_email:{req.new_email.lower()}",
        expires_at=datetime.utcnow() + timedelta(minutes=VERIFY_TTL_MINUTES),
    )
    db.add(vt)
    db.commit()
    try:
        send_verification(req.new_email, code)
    except Exception as e:
        log.error(f"Change email send error: {e}")
    return {"message": f"Код отправлен на {req.new_email}"}


@app.post("/auth/change-email/confirm")
def change_email_confirm(req: ConfirmChangeEmailRequest,
                         user: User = Depends(current_user),
                         db: Session = Depends(get_db)):
    vt = db.query(VerifyToken).filter(
        VerifyToken.user_id == user.id,
        VerifyToken.token == req.code,
        VerifyToken.purpose.like("change_email:%"),
        VerifyToken.used == False,
        VerifyToken.expires_at > datetime.utcnow(),
    ).first()
    if not vt:
        raise HTTPException(400, "Неверный или истёкший код")
    new_email = vt.purpose.split(":", 1)[1]
    vt.used = True
    db_user = db.query(User).filter_by(id=user.id).first()
    db_user.email = new_email
    db.commit()
    return {"token": create_token(user.id, new_email), "user": _user_dict(db_user)}


@app.get("/auth/me")
def me(user: User = Depends(current_user), db: Session = Depends(get_db)):
    db_user = db.query(User).filter_by(id=user.id).first()
    return _user_dict(db_user)

# ═══════════════════════════════════════════════════════════════════════════════
# CABINET
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/cabinet/stats")
def cabinet_stats(user: User = Depends(current_user), db: Session = Depends(get_db)):
    db_user = db.query(User).filter_by(id=user.id).first()
    sub = db.query(Subscription).filter_by(user_id=user.id, status="active")\
            .order_by(Subscription.id.desc()).first()
    txs = db.query(Transaction).filter_by(user_id=user.id)\
            .order_by(Transaction.created_at.desc()).limit(50).all()
    usage = db.query(Message.model, Message.tokens_used)\
              .filter_by(user_id=user.id, role="user").all()
    model_usage = {}
    for m, t in usage:
        model_usage[m] = model_usage.get(m, 0) + (t or 0)
    return {
        "user": _user_dict(db_user),
        "subscription": _sub_dict(sub) if sub else None,
        "transactions": [_tx_dict(t) for t in txs],
        "model_usage": model_usage,
    }

# ═══════════════════════════════════════════════════════════════════════════════
# PAYMENTS
# ═══════════════════════════════════════════════════════════════════════════════

class BuyPlanRequest(BaseModel):
    plan: str
    return_url: str = "http://localhost:8000/?payment=success"

@app.get("/plans")
def list_plans():
    return [
        {"id": k, "name": v["name"], "price_rub": v["price_rub"],
         "tokens": v["tokens"], "tokens_fmt": f"{v['tokens']//1000}к"}
        for k, v in PLANS.items()
    ]

@app.post("/payment/create")
def payment_create(req: BuyPlanRequest, user: User = Depends(current_user)):
    if not user.is_verified:
        raise HTTPException(403, "Подтвердите email для оплаты")
    if req.plan not in PLANS:
        raise HTTPException(400, f"Неизвестный план: {req.plan}")
    try:
        return create_payment(req.plan, user.id, req.return_url)
    except Exception as e:
        raise HTTPException(500, f"Ошибка платежа: {e}")

@app.get("/payment/confirm/{payment_id}")
def payment_confirm(payment_id: str,
                    user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    try:
        status = check_payment(payment_id)
    except Exception as e:
        raise HTTPException(500, str(e))
    if status != "succeeded":
        return {"status": status}

    existing = db.query(Subscription).filter_by(yookassa_payment_id=payment_id).first()
    if existing:
        return {"status": "already_activated", "subscription": _sub_dict(existing)}

    from yookassa import Payment as YKP
    p = YKP.find_one(payment_id)
    plan = p.metadata.get("plan", "starter")
    plan_cfg = get_plan(plan)

    db_user = db.query(User).filter_by(id=user.id).first()
    db_user.tokens_balance += plan_cfg["tokens"]
    sub = Subscription(
        user_id=user.id, plan=plan,
        tokens_total=plan_cfg["tokens"], price_rub=plan_cfg["price_rub"],
        status="active", yookassa_payment_id=payment_id,
        expires_at=datetime.utcnow() + timedelta(days=30),
    )
    db.add(sub)
    db.add(Transaction(
        user_id=user.id, type="payment",
        amount_rub=plan_cfg["price_rub"], tokens_delta=plan_cfg["tokens"],
        description=f"Подписка «{plan_cfg['name']}»",
        yookassa_payment_id=payment_id,
    ))
    db.commit()
    db.refresh(sub)
    return {"status": "activated", "subscription": _sub_dict(sub)}

# ═══════════════════════════════════════════════════════════════════════════════
# CHAT
# ═══════════════════════════════════════════════════════════════════════════════

class CreateChatRequest(BaseModel):
    model: str

class MessageRequest(BaseModel):
    chat_id: str
    message: str
    model: str
    file_url: str | None = None
    extra: dict | None = None

class RenameRequest(BaseModel):
    chat_id: str
    title: str

@app.post("/chat/create")
def create_chat(req: CreateChatRequest):
    return {"chat_id": str(uuid.uuid4()), "model": req.model}

@app.get("/chat/{chat_id}")
def get_chat(chat_id: str, db: Session = Depends(get_db)):
    msgs = db.query(Message).filter_by(chat_id=chat_id).order_by(Message.id).all()
    return [{"role": m.role, "content": m.content} for m in msgs]

@app.post("/chat/rename")
def rename_chat(req: RenameRequest, db: Session = Depends(get_db)):
    msg = db.query(Message).filter_by(chat_id=req.chat_id).first()
    if not msg:
        return {"error": "chat not found"}
    msg.title = req.title
    db.commit()
    return {"status": "ok"}

@app.delete("/chat/{chat_id}")
def delete_chat(chat_id: str, db: Session = Depends(get_db)):
    msgs = db.query(Message).filter_by(chat_id=chat_id).all()
    if not msgs:
        raise HTTPException(404, "Chat not found")
    for m in msgs:
        db.delete(m)
    db.commit()
    return {"status": "deleted"}

@app.get("/chats/{model}")
def get_chats(model: str, db: Session = Depends(get_db), user=Depends(optional_user)):
    gpt_models = ["gpt", "gpt-4o", "gpt-4o-mini"]
    q = db.query(Message.chat_id, Message.title)
    if model == "gpt":
        q = q.filter(Message.model.in_(gpt_models))
    else:
        q = q.filter_by(model=model)
    if user:
        q = q.filter_by(user_id=user.id)
    result = {}
    for cid, title in q.all():
        if cid not in result:
            result[cid] = title or "Новый чат"
    return [{"id": k, "title": v} for k, v in result.items()]

@app.post("/message")
def send_message(req: MessageRequest,
                 db: Session = Depends(get_db),
                 user=Depends(optional_user)):
    cfg = resolve_model(req.model)
    cost = get_token_cost(cfg["real_model"] if cfg else req.model)

    if user:
        if not user.is_verified:
            raise HTTPException(403, "Подтвердите email для отправки сообщений")
        db_user = db.query(User).filter_by(id=user.id).first()
        if db_user.tokens_balance < cost:
            raise HTTPException(402, "Недостаточно токенов. Пополните баланс в личном кабинете.")

    existing = db.query(Message).filter_by(chat_id=req.chat_id).first()
    title = None
    if not existing:
        title = req.message[:40] if req.message else "Файл"

    stored = json.dumps({"text": req.message, "file_url": req.file_url}) \
             if req.file_url else req.message

    user_msg = Message(
        chat_id=req.chat_id, role="user", content=stored,
        model=req.model, title=title,
        user_id=user.id if user else None, tokens_used=cost,
    )
    db.add(user_msg)
    db.commit()

    history = db.query(Message).filter_by(chat_id=req.chat_id)\
                .order_by(Message.id).all()[-20:]

    def parse(c):
        try:
            p = json.loads(c)
            if isinstance(p, dict) and "file_url" in p:
                return p
        except:
            pass
        return c

    formatted = [{"role": "system", "content": "Ты полезный AI ассистент."}] + \
                [{"role": m.role, "content": parse(m.content)} for m in history]

    try:
        answer = generate_response(req.model, formatted, req.extra)
    except Exception as e:
        return {"error": str(e)}

    content   = answer.get("content", "") if isinstance(answer, dict) else answer
    resp_type = answer.get("type", "text") if isinstance(answer, dict) else "text"

    db.add(Message(
        chat_id=req.chat_id, role="assistant", content=content,
        model=req.model, user_id=user.id if user else None,
    ))

    if user:
        db_user = db.query(User).filter_by(id=user.id).first()
        db_user.tokens_balance -= cost
        db.add(Transaction(
            user_id=user.id, type="usage", tokens_delta=-cost,
            description=f"Запрос к {req.model}", model=req.model,
        ))

    db.commit()
    return {"response": {"type": resp_type, "content": content}}

# ─── upload ───────────────────────────────────────────────────────────────────

@app.post("/upload")
def upload_file(file: UploadFile = File(...)):
    fid  = str(uuid.uuid4())
    path = f"{UPLOAD_DIR}/{fid}_{file.filename}"
    with open(path, "wb") as buf:
        shutil.copyfileobj(file.file, buf)
    return {"url": f"/uploads/{fid}_{file.filename}"}

app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

# ─── Kling task status ────────────────────────────────────────────────────────

@app.get("/kling/status/{task_id}")
def kling_status(task_id: str, user: User = Depends(current_user)):
    import httpx as hx
    keys = [k.strip() for k in os.getenv("KLING_API_KEYS","").split(",") if k.strip()]
    if not keys:
        raise HTTPException(503, "No Kling keys")
    r = hx.get(f"https://api.klingai.com/v1/videos/text2video/{task_id}",
               headers={"Authorization": f"Bearer {keys[0]}"}, timeout=15)
    return r.json()
