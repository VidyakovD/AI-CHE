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
from models import (User, Message, Subscription, Transaction, VerifyToken,
                    Solution, SolutionCategory, SolutionStep, SolutionRun)
from auth import (hash_password, verify_password, create_token, decode_token,
                  generate_code, VERIFY_TTL_MINUTES)
from ai import generate_response, get_token_cost, resolve_model
from payments import create_payment, check_payment, get_plan, PLANS
from email_service import send_verification, send_password_reset, send_welcome
from security import (rate_limit_middleware, validate_email, validate_password,
                      validate_upload_filename, require_admin)

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="Obsidian AI")
UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ── CORS — задай свой домен в .env: ALLOWED_ORIGINS=https://yourdomain.com ──
_origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.middleware("http")(rate_limit_middleware)

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

def _user_dict(u):
    return {"id": u.id, "email": u.email, "name": u.name,
            "avatar_url": u.avatar_url, "tokens_balance": u.tokens_balance,
            "is_verified": u.is_verified, "referral_code": u.referral_code,
            "created_at": u.created_at.isoformat() if u.created_at else None}

def _sub_dict(s):
    return {"id": s.id, "plan": s.plan, "tokens_total": s.tokens_total,
            "tokens_used": s.tokens_used, "tokens_left": s.tokens_total - s.tokens_used,
            "price_rub": s.price_rub, "status": s.status,
            "started_at": s.started_at.isoformat() if s.started_at else None,
            "expires_at": s.expires_at.isoformat() if s.expires_at else None}

def _tx_dict(t):
    return {"id": t.id, "type": t.type, "amount_rub": t.amount_rub,
            "tokens_delta": t.tokens_delta, "description": t.description,
            "model": t.model,
            "created_at": t.created_at.isoformat() if t.created_at else None}

def _make_verify_token(db, user_id, purpose):
    db.query(VerifyToken).filter_by(user_id=user_id, purpose=purpose, used=False).update({"used": True})
    code = generate_code(6)
    db.add(VerifyToken(user_id=user_id, token=code, purpose=purpose,
                       expires_at=datetime.utcnow() + timedelta(minutes=VERIFY_TTL_MINUTES)))
    db.commit()
    return code

def _use_verify_token(db, user_id, code, purpose):
    vt = db.query(VerifyToken).filter_by(
        user_id=user_id, token=code, purpose=purpose, used=False).first()
    if not vt or vt.expires_at < datetime.utcnow():
        return False
    vt.used = True
    db.commit()
    return True

def _deduct(db, user, cost, description, model=None):
    """Списать токены и записать транзакцию."""
    db_user = db.query(User).filter_by(id=user.id).first()
    if db_user.tokens_balance < cost:
        raise HTTPException(402, "Недостаточно токенов. Пополните баланс в личном кабинете.")
    db_user.tokens_balance -= cost
    db.add(Transaction(user_id=user.id, type="usage", tokens_delta=-cost,
                       description=description, model=model))

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

    code = _make_verify_token(db, user.id, "verify_email")
    try: send_verification(user.email, code)
    except Exception as e: log.error(f"Email error: {e}")

    return {"status": "pending_verification", "user_id": user.id,
            "message": "На ваш email отправлен 6-значный код подтверждения"}

@app.post("/auth/verify-email")
def verify_email(req: VerifyEmailRequest, db: Session = Depends(get_db)):
    # Лимит попыток: не более 10 за последний час через rate limiter
    user = db.query(User).filter_by(id=req.user_id).first()
    if not user: raise HTTPException(404, "Пользователь не найден")
    if user.is_verified: raise HTTPException(400, "Email уже подтверждён")
    if not _use_verify_token(db, user.id, req.code, "verify_email"):
        raise HTTPException(400, "Неверный или истёкший код")
    user.is_verified = True
    user.tokens_balance = 5_000
    db.add(Transaction(user_id=user.id, type="bonus", tokens_delta=5_000,
                       description="Приветственный бонус"))
    db.commit()
    try: send_welcome(user.email, user.name or "")
    except Exception as e: log.error(f"Welcome email error: {e}")
    return {"token": create_token(user.id, user.email), "user": _user_dict(user)}

@app.post("/auth/resend-verify")
def resend_verify(req: ResendVerifyRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter_by(id=req.user_id).first()
    if not user: raise HTTPException(404, "Пользователь не найден")
    if user.is_verified: raise HTTPException(400, "Email уже подтверждён")
    code = _make_verify_token(db, user.id, "verify_email")
    try: send_verification(user.email, code)
    except Exception as e: log.error(f"Resend error: {e}")
    return {"message": "Код повторно отправлен"}

@app.post("/auth/login")
def login(req: LoginRequest, db: Session = Depends(get_db)):
    email = validate_email(req.email)
    user = db.query(User).filter_by(email=email).first()
    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(401, "Неверный email или пароль")
    if not user.is_verified:
        return {"status": "pending_verification", "user_id": user.id,
                "message": "Подтвердите email. Выслать код повторно?"}
    return {"token": create_token(user.id, user.email), "user": _user_dict(user)}

@app.post("/auth/forgot-password")
def forgot_password(req: ForgotPasswordRequest, db: Session = Depends(get_db)):
    try: email = validate_email(req.email)
    except: return {"message": "Если аккаунт существует — письмо отправлено", "user_id": None}
    user = db.query(User).filter_by(email=email).first()
    if user and user.is_verified:
        code = _make_verify_token(db, user.id, "reset_password")
        try: send_password_reset(user.email, code)
        except Exception as e: log.error(f"Reset email error: {e}")
    return {"message": "Если аккаунт существует — письмо отправлено",
            "user_id": user.id if user else None}

@app.post("/auth/reset-password")
def reset_password(req: ResetPasswordRequest, db: Session = Depends(get_db)):
    validate_password(req.new_password)
    user = db.query(User).filter_by(id=req.user_id).first()
    if not user: raise HTTPException(404, "Пользователь не найден")
    if not _use_verify_token(db, user.id, req.code, "reset_password"):
        raise HTTPException(400, "Неверный или истёкший код")
    user.password_hash = hash_password(req.new_password)
    db.commit()
    return {"token": create_token(user.id, user.email), "user": _user_dict(user)}

@app.post("/auth/change-password")
def change_password(req: ChangePasswordRequest, user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    validate_password(req.new_password)
    db_user = db.query(User).filter_by(id=user.id).first()
    if not verify_password(req.old_password, db_user.password_hash):
        raise HTTPException(400, "Неверный текущий пароль")
    db_user.password_hash = hash_password(req.new_password)
    db.commit()
    return {"status": "ok"}

@app.post("/auth/change-email/request")
def change_email_request(req: ChangeEmailRequest, user: User = Depends(current_user),
                         db: Session = Depends(get_db)):
    new_email = validate_email(req.new_email)
    db_user = db.query(User).filter_by(id=user.id).first()
    if not verify_password(req.password, db_user.password_hash):
        raise HTTPException(400, "Неверный пароль")
    if db.query(User).filter_by(email=new_email).first():
        raise HTTPException(400, "Этот email уже занят")
    code = generate_code(6)
    db.add(VerifyToken(user_id=user.id, token=code,
                       purpose=f"change_email:{new_email}",
                       expires_at=datetime.utcnow() + timedelta(minutes=VERIFY_TTL_MINUTES)))
    db.commit()
    try: send_verification(new_email, code)
    except Exception as e: log.error(f"Change email error: {e}")
    return {"message": f"Код отправлен на {new_email}"}

@app.post("/auth/change-email/confirm")
def change_email_confirm(req: ConfirmChangeEmailRequest, user: User = Depends(current_user),
                         db: Session = Depends(get_db)):
    vt = db.query(VerifyToken).filter(
        VerifyToken.user_id == user.id, VerifyToken.token == req.code,
        VerifyToken.purpose.like("change_email:%"), VerifyToken.used == False,
        VerifyToken.expires_at > datetime.utcnow()).first()
    if not vt: raise HTTPException(400, "Неверный или истёкший код")
    new_email = vt.purpose.split(":", 1)[1]
    vt.used = True
    db_user = db.query(User).filter_by(id=user.id).first()
    db_user.email = new_email
    db.commit()
    return {"token": create_token(user.id, new_email), "user": _user_dict(db_user)}

@app.get("/auth/me")
def me(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return _user_dict(db.query(User).filter_by(id=user.id).first())

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
    usage = db.query(Message.model, Message.tokens_used).filter_by(user_id=user.id, role="user").all()
    model_usage = {}
    for m, t in usage:
        model_usage[m] = model_usage.get(m, 0) + (t or 0)
    return {"user": _user_dict(db_user),
            "subscription": _sub_dict(sub) if sub else None,
            "transactions": [_tx_dict(t) for t in txs],
            "model_usage": model_usage}

# ═══════════════════════════════════════════════════════════════════════════════
# PAYMENTS
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/plans")
def list_plans():
    return [{"id": k, "name": v["name"], "price_rub": v["price_rub"],
             "tokens": v["tokens"], "tokens_fmt": f"{v['tokens']//1000}к"}
            for k, v in PLANS.items()]

class BuyPlanRequest(BaseModel):
    plan: str
    return_url: str = "http://localhost:8000/?payment=success"

@app.post("/payment/create")
def payment_create(req: BuyPlanRequest, user: User = Depends(current_user)):
    if not user.is_verified: raise HTTPException(403, "Подтвердите email для оплаты")
    if req.plan not in PLANS: raise HTTPException(400, f"Неизвестный план: {req.plan}")
    try: return create_payment(req.plan, user.id, req.return_url)
    except Exception as e: raise HTTPException(500, f"Ошибка платежа: {e}")

@app.get("/payment/confirm/{payment_id}")
def payment_confirm(payment_id: str, user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    try: status = check_payment(payment_id)
    except Exception as e: raise HTTPException(500, str(e))
    if status != "succeeded": return {"status": status}
    existing = db.query(Subscription).filter_by(yookassa_payment_id=payment_id).first()
    if existing: return {"status": "already_activated", "subscription": _sub_dict(existing)}
    from yookassa import Payment as YKP
    p = YKP.find_one(payment_id)
    plan = p.metadata.get("plan", "starter")
    plan_cfg = get_plan(plan)
    db_user = db.query(User).filter_by(id=user.id).first()
    db_user.tokens_balance += plan_cfg["tokens"]
    sub = Subscription(user_id=user.id, plan=plan, tokens_total=plan_cfg["tokens"],
                       price_rub=plan_cfg["price_rub"], status="active",
                       yookassa_payment_id=payment_id,
                       expires_at=datetime.utcnow() + timedelta(days=30))
    db.add(sub)
    db.add(Transaction(user_id=user.id, type="payment", amount_rub=plan_cfg["price_rub"],
                       tokens_delta=plan_cfg["tokens"],
                       description=f"Подписка «{plan_cfg['name']}»",
                       yookassa_payment_id=payment_id))
    db.commit(); db.refresh(sub)
    return {"status": "activated", "subscription": _sub_dict(sub)}

# ═══════════════════════════════════════════════════════════════════════════════
# SOLUTIONS — публичные эндпоинты
# ═══════════════════════════════════════════════════════════════════════════════

def _sol_dict(s: Solution) -> dict:
    return {"id": s.id, "title": s.title, "description": s.description,
            "image_url": s.image_url, "price_tokens": s.price_tokens,
            "category_id": s.category_id,
            "steps_count": len(s.steps) if s.steps else 0}

def _step_dict(s: SolutionStep) -> dict:
    return {"id": s.id, "step_number": s.step_number, "title": s.title,
            "model": s.model, "system_prompt": s.system_prompt,
            "user_prompt": s.user_prompt, "wait_for_user": s.wait_for_user,
            "user_hint": s.user_hint,
            "extra_params": json.loads(s.extra_params) if s.extra_params else None}

@app.get("/solutions/categories")
def get_categories(db: Session = Depends(get_db)):
    cats = db.query(SolutionCategory).order_by(SolutionCategory.sort_order).all()
    return [{"id": c.id, "slug": c.slug, "title": c.title} for c in cats]

@app.get("/solutions")
def get_solutions(category: str | None = None, db: Session = Depends(get_db)):
    q = db.query(Solution).filter_by(is_active=True)
    if category:
        cat = db.query(SolutionCategory).filter_by(slug=category).first()
        if cat: q = q.filter_by(category_id=cat.id)
    return [_sol_dict(s) for s in q.order_by(Solution.sort_order).all()]

@app.get("/solutions/{solution_id}")
def get_solution(solution_id: int, db: Session = Depends(get_db)):
    s = db.query(Solution).filter_by(id=solution_id, is_active=True).first()
    if not s: raise HTTPException(404, "Решение не найдено")
    d = _sol_dict(s)
    d["steps"] = [_step_dict(st) for st in s.steps]
    return d

@app.post("/solutions/{solution_id}/run")
def run_solution(solution_id: int, db: Session = Depends(get_db),
                 user=Depends(optional_user)):
    s = db.query(Solution).filter_by(id=solution_id, is_active=True).first()
    if not s: raise HTTPException(404, "Решение не найдено")
    if user:
        if not user.is_verified: raise HTTPException(403, "Подтвердите email")
        db_user = db.query(User).filter_by(id=user.id).first()
        if s.price_tokens > 0 and db_user.tokens_balance < s.price_tokens:
            raise HTTPException(402, "Недостаточно токенов")
    chat_id = str(uuid.uuid4())
    run = SolutionRun(user_id=user.id if user else None,
                      solution_id=solution_id, chat_id=chat_id,
                      current_step=0, status="running", context=json.dumps({}))
    db.add(run); db.commit(); db.refresh(run)

    # Если первый шаг не ждёт ввода — сразу выполняем
    first_step = s.steps[0] if s.steps else None
    if first_step and not first_step.wait_for_user:
        return _execute_step(run, first_step, None, db, user)

    return {"run_id": run.id, "chat_id": chat_id, "status": "waiting_input",
            "step": _step_dict(first_step) if first_step else None}

@app.post("/solutions/runs/{run_id}/continue")
def continue_run(run_id: int, body: dict, db: Session = Depends(get_db),
                 user=Depends(optional_user)):
    run = db.query(SolutionRun).filter_by(id=run_id).first()
    if not run: raise HTTPException(404, "Run не найден")
    if run.status == "done": return {"status": "done"}

    solution = db.query(Solution).filter_by(id=run.solution_id).first()
    steps = solution.steps
    if run.current_step >= len(steps):
        run.status = "done"; db.commit()
        return {"status": "done", "chat_id": run.chat_id}

    step = steps[run.current_step]
    user_input = body.get("input", "")
    return _execute_step(run, step, user_input, db, user)

def _execute_step(run: SolutionRun, step: SolutionStep, user_input,
                  db: Session, user) -> dict:
    ctx = json.loads(run.context or "{}")

    # Подставляем переменные в промпт
    prompt = step.user_prompt or ""
    prompt = prompt.replace("{input}", user_input or "")
    prompt = prompt.replace("{prev_result}", ctx.get("prev_result", ""))
    for k, v in ctx.items():
        prompt = prompt.replace(f"{{{k}}}", str(v))

    messages = []
    if step.system_prompt:
        messages.append({"role": "system", "content": step.system_prompt})
    messages.append({"role": "user", "content": prompt})

    extra = json.loads(step.extra_params) if step.extra_params else None

    try:
        answer = generate_response(step.model, messages, extra)
    except Exception as e:
        run.status = "error"; db.commit()
        return {"status": "error", "error": str(e)}

    content = answer.get("content", "") if isinstance(answer, dict) else str(answer)
    resp_type = answer.get("type", "text") if isinstance(answer, dict) else "text"

    # Сохраняем в чат
    if user_input:
        db.add(Message(chat_id=run.chat_id, role="user", content=user_input,
                       model=step.model, user_id=user.id if user else None))
    db.add(Message(chat_id=run.chat_id, role="assistant", content=content,
                   model=step.model, user_id=user.id if user else None))

    # Списываем токены за шаг
    if user:
        cost = get_token_cost(resolve_model(step.model)["real_model"] if resolve_model(step.model) else step.model)
        try: _deduct(db, user, cost, f"Решение: {step.title or step.step_number}", step.model)
        except: pass

    # Обновляем контекст
    ctx["prev_result"] = content
    ctx[f"step_{step.step_number}"] = content
    run.current_step += 1

    solution = db.query(Solution).filter_by(id=run.solution_id).first()
    steps = solution.steps

    # Следующий шаг
    if run.current_step >= len(steps):
        run.status = "done"
        # Списываем фиксированную цену решения (если есть)
        if user and solution.price_tokens > 0:
            try: _deduct(db, user, solution.price_tokens, f"Готовое решение: {solution.title}")
            except: pass
        db.commit()
        return {"status": "done", "chat_id": run.chat_id,
                "result": {"type": resp_type, "content": content}}

    next_step = steps[run.current_step]
    run.context = json.dumps(ctx)
    db.commit()

    # Если следующий шаг не ждёт ввода — выполняем сразу
    if not next_step.wait_for_user:
        return _execute_step(run, next_step, None, db, user)

    return {"status": "waiting_input", "run_id": run.id, "chat_id": run.chat_id,
            "step": _step_dict(next_step),
            "current_result": {"type": resp_type, "content": content}}

# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN — Solutions CRUD
# ═══════════════════════════════════════════════════════════════════════════════

class CategoryBody(BaseModel):
    slug: str
    title: str
    sort_order: int = 0

class SolutionBody(BaseModel):
    category_id: int
    title: str
    description: str | None = None
    image_url: str | None = None
    price_tokens: int = 0
    is_active: bool = True
    sort_order: int = 0

class StepBody(BaseModel):
    step_number: int
    title: str | None = None
    model: str
    system_prompt: str | None = None
    user_prompt: str | None = None
    wait_for_user: bool = False
    user_hint: str | None = None
    extra_params: dict | None = None

@app.get("/admin/users")
def admin_users(user: User = Depends(current_user), db: Session = Depends(get_db)):
    require_admin(user)
    users = db.query(User).order_by(User.created_at.desc()).limit(200).all()
    return [_user_dict(u) for u in users]

@app.get("/admin/stats")
def admin_stats(user: User = Depends(current_user), db: Session = Depends(get_db)):
    require_admin(user)
    return {
        "total_users":    db.query(User).count(),
        "verified_users": db.query(User).filter_by(is_verified=True).count(),
        "total_messages": db.query(Message).count(),
        "total_revenue":  db.query(Transaction).filter_by(type="payment")\
                            .with_entities(__import__("sqlalchemy").func.sum(Transaction.amount_rub)).scalar() or 0,
    }

@app.post("/admin/categories")
def admin_create_category(body: CategoryBody, user: User = Depends(current_user),
                          db: Session = Depends(get_db)):
    require_admin(user)
    cat = SolutionCategory(**body.dict())
    db.add(cat); db.commit(); db.refresh(cat)
    return {"id": cat.id, "slug": cat.slug, "title": cat.title}

@app.post("/admin/solutions")
def admin_create_solution(body: SolutionBody, user: User = Depends(current_user),
                          db: Session = Depends(get_db)):
    require_admin(user)
    sol = Solution(**body.dict())
    db.add(sol); db.commit(); db.refresh(sol)
    return _sol_dict(sol)

@app.put("/admin/solutions/{solution_id}")
def admin_update_solution(solution_id: int, body: SolutionBody,
                          user: User = Depends(current_user), db: Session = Depends(get_db)):
    require_admin(user)
    sol = db.query(Solution).filter_by(id=solution_id).first()
    if not sol: raise HTTPException(404)
    for k, v in body.dict().items():
        setattr(sol, k, v)
    db.commit()
    return _sol_dict(sol)

@app.delete("/admin/solutions/{solution_id}")
def admin_delete_solution(solution_id: int, user: User = Depends(current_user),
                          db: Session = Depends(get_db)):
    require_admin(user)
    sol = db.query(Solution).filter_by(id=solution_id).first()
    if not sol: raise HTTPException(404)
    db.delete(sol); db.commit()
    return {"status": "deleted"}

@app.post("/admin/solutions/{solution_id}/steps")
def admin_add_step(solution_id: int, body: StepBody, user: User = Depends(current_user),
                   db: Session = Depends(get_db)):
    require_admin(user)
    sol = db.query(Solution).filter_by(id=solution_id).first()
    if not sol: raise HTTPException(404)
    d = body.dict()
    if d.get("extra_params"):
        d["extra_params"] = json.dumps(d["extra_params"])
    step = SolutionStep(solution_id=solution_id, **d)
    db.add(step); db.commit(); db.refresh(step)
    return _step_dict(step)

@app.put("/admin/steps/{step_id}")
def admin_update_step(step_id: int, body: StepBody, user: User = Depends(current_user),
                      db: Session = Depends(get_db)):
    require_admin(user)
    step = db.query(SolutionStep).filter_by(id=step_id).first()
    if not step: raise HTTPException(404)
    d = body.dict()
    if d.get("extra_params"):
        d["extra_params"] = json.dumps(d["extra_params"])
    for k, v in d.items():
        setattr(step, k, v)
    db.commit()
    return _step_dict(step)

@app.delete("/admin/steps/{step_id}")
def admin_delete_step(step_id: int, user: User = Depends(current_user),
                      db: Session = Depends(get_db)):
    require_admin(user)
    step = db.query(SolutionStep).filter_by(id=step_id).first()
    if not step: raise HTTPException(404)
    db.delete(step); db.commit()
    return {"status": "deleted"}

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
    if not msg: return {"error": "chat not found"}
    msg.title = req.title; db.commit()
    return {"status": "ok"}

@app.delete("/chat/{chat_id}")
def delete_chat(chat_id: str, db: Session = Depends(get_db)):
    msgs = db.query(Message).filter_by(chat_id=chat_id).all()
    if not msgs: raise HTTPException(404, "Chat not found")
    for m in msgs: db.delete(m)
    db.commit()
    return {"status": "deleted"}

@app.get("/chats/{model}")
def get_chats(model: str, db: Session = Depends(get_db), user=Depends(optional_user)):
    from sqlalchemy import or_
    gpt_models = ["gpt", "gpt-4o", "gpt-4o-mini"]
    q = db.query(Message.chat_id, Message.title)
    if model == "gpt":
        q = q.filter(Message.model.in_(gpt_models))
    else:
        q = q.filter_by(model=model)
    if user:
        q = q.filter(or_(Message.user_id == user.id, Message.user_id == None))
    else:
        q = q.filter(Message.user_id == None)
    result = {}
    for cid, title in q.all():
        if cid not in result:
            result[cid] = title or "Новый чат"
    return [{"id": k, "title": v} for k, v in result.items()]

@app.post("/message")
def send_message(req: MessageRequest, db: Session = Depends(get_db),
                 user=Depends(optional_user)):
    cfg = resolve_model(req.model)
    cost = get_token_cost(cfg["real_model"] if cfg else req.model)
    if user:
        if not user.is_verified:
            raise HTTPException(403, "Подтвердите email для отправки сообщений")
        _deduct(db, user, cost, f"Запрос к {req.model}", req.model)

    existing = db.query(Message).filter_by(chat_id=req.chat_id).first()
    title = None
    if not existing:
        title = req.message[:40] if req.message else "Файл"

    stored = json.dumps({"text": req.message, "file_url": req.file_url}) \
             if req.file_url else req.message

    db.add(Message(chat_id=req.chat_id, role="user", content=stored,
                   model=req.model, title=title,
                   user_id=user.id if user else None, tokens_used=cost))
    db.commit()

    history = db.query(Message).filter_by(chat_id=req.chat_id)\
                .order_by(Message.id).all()[-20:]

    def parse(c):
        try:
            p = json.loads(c)
            if isinstance(p, dict) and "file_url" in p: return p
        except: pass
        return c

    formatted = [{"role": "system", "content": "Ты полезный AI ассистент."}] + \
                [{"role": m.role, "content": parse(m.content)} for m in history]
    try:
        answer = generate_response(req.model, formatted, req.extra)
    except Exception as e:
        return {"error": str(e)}

    content   = answer.get("content", "") if isinstance(answer, dict) else answer
    resp_type = answer.get("type", "text") if isinstance(answer, dict) else "text"

    db.add(Message(chat_id=req.chat_id, role="assistant", content=content,
                   model=req.model, user_id=user.id if user else None))
    db.commit()
    return {"response": {"type": resp_type, "content": content}}

@app.post("/upload")
def upload_file(file: UploadFile = File(...)):
    validate_upload_filename(file.filename)
    fid  = str(uuid.uuid4())
    path = f"{UPLOAD_DIR}/{fid}_{file.filename}"
    with open(path, "wb") as buf:
        shutil.copyfileobj(file.file, buf)
    return {"url": f"/uploads/{fid}_{file.filename}"}

app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

@app.get("/kling/status/{task_id}")
def kling_status(task_id: str, user: User = Depends(current_user)):
    import httpx as hx
    keys = [k.strip() for k in os.getenv("KLING_API_KEYS","").split(",") if k.strip()]
    if not keys: raise HTTPException(503, "No Kling keys")
    r = hx.get(f"https://api.klingai.com/v1/videos/text2video/{task_id}",
               headers={"Authorization": f"Bearer {keys[0]}"}, timeout=15)
    return r.json()

# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN — API Keys Management
# ═══════════════════════════════════════════════════════════════════════════════

from models import ApiKey

PROVIDERS_LIST = ["openai","anthropic","perplexity","kling","veo","nano","yookassa"]

class ApiKeyBody(BaseModel):
    provider: str
    key_value: str
    label: str | None = None

@app.get("/admin/apikeys")
def admin_get_keys(user: User = Depends(current_user), db: Session = Depends(get_db)):
    require_admin(user)
    keys = db.query(ApiKey).order_by(ApiKey.provider, ApiKey.id).all()
    return [{
        "id": k.id, "provider": k.provider, "label": k.label,
        "key_preview": k.key_value[:8]+"..."+k.key_value[-4:] if len(k.key_value)>12 else "***",
        "status": k.status, "last_error": k.last_error,
        "last_check": k.last_check.isoformat() if k.last_check else None,
    } for k in keys]

@app.post("/admin/apikeys")
def admin_add_key(body: ApiKeyBody, user: User = Depends(current_user),
                  db: Session = Depends(get_db)):
    require_admin(user)
    if body.provider not in PROVIDERS_LIST:
        raise HTTPException(400, f"Неизвестный провайдер: {body.provider}")
    key = ApiKey(provider=body.provider, key_value=body.key_value.strip(),
                 label=body.label, status="unknown")
    db.add(key); db.commit(); db.refresh(key)
    # Сразу обновляем env переменную
    _rebuild_env_keys(body.provider, db)
    return {"id": key.id, "status": "added"}

@app.delete("/admin/apikeys/{key_id}")
def admin_delete_key(key_id: int, user: User = Depends(current_user),
                     db: Session = Depends(get_db)):
    require_admin(user)
    key = db.query(ApiKey).filter_by(id=key_id).first()
    if not key: raise HTTPException(404)
    provider = key.provider
    db.delete(key); db.commit()
    _rebuild_env_keys(provider, db)
    return {"status": "deleted"}

@app.post("/admin/apikeys/{key_id}/check")
def admin_check_key(key_id: int, user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    require_admin(user)
    key = db.query(ApiKey).filter_by(id=key_id).first()
    if not key: raise HTTPException(404)
    status, error = _test_key(key.provider, key.key_value)
    key.status = status
    key.last_error = error
    key.last_check = datetime.utcnow()
    db.commit()
    return {"status": status, "error": error}

@app.post("/admin/apikeys/check-all")
def admin_check_all_keys(user: User = Depends(current_user), db: Session = Depends(get_db)):
    require_admin(user)
    keys = db.query(ApiKey).all()
    results = []
    for key in keys:
        status, error = _test_key(key.provider, key.key_value)
        key.status = status
        key.last_error = error
        key.last_check = datetime.utcnow()
        results.append({"id": key.id, "provider": key.provider, "status": status})
    db.commit()
    return results

def _test_key(provider: str, key_value: str) -> tuple[str, str | None]:
    """Проверяет ключ отправкой минимального запроса."""
    try:
        if provider == "openai":
            from openai import OpenAI
            c = OpenAI(api_key=key_value)
            c.chat.completions.create(model="gpt-4o-mini",
                messages=[{"role":"user","content":"hi"}], max_tokens=1)
            return "ok", None
        elif provider == "anthropic":
            import anthropic as _ant
            c = _ant.Anthropic(api_key=key_value)
            c.messages.create(model="claude-3-haiku-20240307",
                max_tokens=1, messages=[{"role":"user","content":"hi"}])
            return "ok", None
        elif provider == "perplexity":
            from openai import OpenAI
            c = OpenAI(api_key=key_value, base_url="https://api.perplexity.ai")
            c.chat.completions.create(model="sonar-small-chat",
                messages=[{"role":"user","content":"hi"}], max_tokens=1)
            return "ok", None
        elif provider == "kling":
            import httpx
            r = httpx.get("https://api.klingai.com/v1/account/info",
                headers={"Authorization": f"Bearer {key_value}"}, timeout=8)
            return ("ok", None) if r.status_code < 400 else ("error", f"HTTP {r.status_code}")
        elif provider == "yookassa":
            from yookassa import Configuration, Payment
            Configuration.secret_key = key_value
            return "ok", None
        else:
            return "unknown", "Проверка не реализована"
    except Exception as e:
        return "error", str(e)[:200]

def _rebuild_env_keys(provider: str, db: Session):
    """Пересобирает env-переменную из БД ключей."""
    ENV_MAP = {
        "openai":    "OPENAI_API_KEYS",
        "anthropic": "ANTHROPIC_API_KEYS",
        "perplexity":"PERPLEXITY_API_KEYS",
        "kling":     "KLING_API_KEYS",
        "veo":       "VEO_API_KEYS",
        "nano":      "NANO_API_KEYS",
    }
    env_var = ENV_MAP.get(provider)
    if not env_var: return
    keys = db.query(ApiKey).filter_by(provider=provider).all()
    value = ",".join(k.key_value for k in keys)
    os.environ[env_var] = value

# ── Admin: users with balance ─────────────────────────────────────────────────

@app.get("/admin/users/full")
def admin_users_full(user: User = Depends(current_user), db: Session = Depends(get_db)):
    require_admin(user)
    users = db.query(User).order_by(User.created_at.desc()).all()
    result = []
    for u in users:
        sub = db.query(Subscription).filter_by(user_id=u.id, status="active").first()
        result.append({
            **_user_dict(u),
            "subscription": _sub_dict(sub) if sub else None,
            "messages_count": db.query(Message).filter_by(user_id=u.id, role="user").count(),
        })
    return result

@app.post("/admin/users/{user_id}/adjust-balance")
def admin_adjust_balance(user_id: int, body: dict,
                         user: User = Depends(current_user),
                         db: Session = Depends(get_db)):
    require_admin(user)
    delta = int(body.get("delta", 0))
    reason = body.get("reason", "Ручная корректировка")
    target = db.query(User).filter_by(id=user_id).first()
    if not target: raise HTTPException(404)
    target.tokens_balance += delta
    db.add(Transaction(user_id=user_id, type="bonus" if delta > 0 else "usage",
                       tokens_delta=delta, description=reason))
    db.commit()
    return {"tokens_balance": target.tokens_balance}

# ═══════════════════════════════════════════════════════════════════════════════
# PRICING — публичные + admin
# ═══════════════════════════════════════════════════════════════════════════════
from models import PricingSetting, ModelPricing, TokenPackage, FaqItem

DEFAULT_MODEL_PRICING = [
    {"model_id":"gpt",             "label":"GPT-4o mini",     "cost_per_req":5,   "usd_per_req":0.0001,"markup":2.0},
    {"model_id":"gpt-4o",          "label":"GPT-4o",           "cost_per_req":20,  "usd_per_req":0.005, "markup":1.8},
    {"model_id":"claude",          "label":"Claude Haiku",     "cost_per_req":8,   "usd_per_req":0.0002,"markup":1.8},
    {"model_id":"claude-sonnet",   "label":"Claude Sonnet",    "cost_per_req":25,  "usd_per_req":0.006, "markup":1.8},
    {"model_id":"perplexity",      "label":"Perplexity Small", "cost_per_req":6,   "usd_per_req":0.0002,"markup":1.8},
    {"model_id":"perplexity-large","label":"Perplexity Large", "cost_per_req":15,  "usd_per_req":0.001, "markup":1.8},
    {"model_id":"nano",            "label":"Nano Banana",      "cost_per_req":3,   "usd_per_req":0.0001,"markup":2.0},
    {"model_id":"kling",           "label":"Kling v1",         "cost_per_req":200, "usd_per_req":0.14,  "markup":1.5},
    {"model_id":"kling-pro",       "label":"Kling Pro",        "cost_per_req":400, "usd_per_req":0.28,  "markup":1.5},
    {"model_id":"veo",             "label":"Veo 3",            "cost_per_req":300, "usd_per_req":0.20,  "markup":1.5},
]

def _seed_pricing(db: Session):
    """Заполняем дефолтные цены если таблица пустая."""
    if db.query(ModelPricing).count() == 0:
        for p in DEFAULT_MODEL_PRICING:
            db.add(ModelPricing(**p))
    if db.query(PricingSetting).count() == 0:
        for k, v, d in [
            ("usd_to_rub",     "90",   "Курс доллара к рублю"),
            ("ch_to_rub",      "0.10", "Стоимость 1 CH в рублях"),
            ("support_url",    "",     "Ссылка поддержки"),
        ]:
            db.add(PricingSetting(key=k, value=v, description=d))
    if db.query(TokenPackage).count() == 0:
        for name, tokens, price in [
            ("Старт",   10_000,  49),
            ("Базовый", 50_000, 199),
            ("Большой",200_000, 699),
        ]:
            db.add(TokenPackage(name=name, tokens=tokens, price_rub=price))
    if db.query(FaqItem).count() == 0:
        faqs = [
            ("Что такое токены CH?",
             "CH (Che) — внутренняя валюта AI Студии Че. Каждый запрос к модели списывает определённое количество CH. CH входят в подписку или докупаются отдельно."),
            ("Как выбрать подходящую модель?",
             "GPT-4o mini и Claude Haiku — быстрые и экономичные для обычных задач. GPT-4o и Claude Sonnet — для сложного анализа и длинных текстов. Perplexity — для поиска актуальной информации. Kling и Veo — генерация видео."),
            ("Можно ли вернуть неиспользованные токены?",
             "Токены, входящие в подписку, не возвращаются. Докупленные токены действуют бессрочно."),
            ("Как работают готовые решения?",
             "Готовые решения — это настроенные сценарии с заготовленными промптами. Вы можете изменить промпт под свои нужды перед запуском. Стоимость списывается в CH."),
            ("Что такое реферальная программа?",
             "Поделитесь своим кодом — когда друг зарегистрируется по нему, вы оба получите бонусные CH."),
        ]
        for i, (q, a) in enumerate(faqs):
            db.add(FaqItem(question=q, answer=a, sort_order=i))
    db.commit()

# Сид при старте
@app.on_event("startup")
def startup():
    db = SessionLocal()
    try: _seed_pricing(db)
    finally: db.close()

@app.get("/pricing/models")
def get_model_pricing(db: Session = Depends(get_db)):
    items = db.query(ModelPricing).order_by(ModelPricing.model_id).all()
    return [{"model_id":p.model_id,"label":p.label,"cost_per_req":p.cost_per_req,
             "usd_per_req":p.usd_per_req,"markup":p.markup} for p in items]

@app.get("/pricing/packages")
def get_packages(db: Session = Depends(get_db)):
    pkgs = db.query(TokenPackage).filter_by(is_active=True).order_by(TokenPackage.sort_order).all()
    return [{"id":p.id,"name":p.name,"tokens":p.tokens,"price_rub":p.price_rub} for p in pkgs]

@app.get("/pricing/settings")
def get_pricing_settings(db: Session = Depends(get_db)):
    items = db.query(PricingSetting).all()
    return {p.key: p.value for p in items}

@app.get("/faq")
def get_faq(db: Session = Depends(get_db)):
    items = db.query(FaqItem).filter_by(is_active=True).order_by(FaqItem.sort_order).all()
    return [{"id":f.id,"question":f.question,"answer":f.answer} for f in items]

# ── Admin pricing CRUD ────────────────────────────────────────────────────────

class ModelPricingBody(BaseModel):
    cost_per_req: int
    usd_per_req: float
    markup: float

class PackageBody(BaseModel):
    name: str
    tokens: int
    price_rub: float
    is_active: bool = True
    sort_order: int = 0

class FaqBody(BaseModel):
    question: str
    answer: str
    sort_order: int = 0
    is_active: bool = True

class SettingBody(BaseModel):
    value: str

@app.put("/admin/pricing/models/{model_id}")
def admin_update_model_price(model_id: str, body: ModelPricingBody,
                              user: User = Depends(current_user),
                              db: Session = Depends(get_db)):
    require_admin(user)
    p = db.query(ModelPricing).filter_by(model_id=model_id).first()
    if not p: raise HTTPException(404)
    p.cost_per_req = body.cost_per_req
    p.usd_per_req  = body.usd_per_req
    p.markup       = body.markup
    db.commit()
    return {"status": "ok"}

@app.put("/admin/pricing/settings/{key}")
def admin_update_setting(key: str, body: SettingBody,
                          user: User = Depends(current_user),
                          db: Session = Depends(get_db)):
    require_admin(user)
    p = db.query(PricingSetting).filter_by(key=key).first()
    if not p: raise HTTPException(404)
    p.value = body.value
    db.commit()
    return {"status": "ok"}

@app.post("/admin/pricing/packages")
def admin_add_package(body: PackageBody, user: User = Depends(current_user),
                       db: Session = Depends(get_db)):
    require_admin(user)
    pkg = TokenPackage(**body.dict())
    db.add(pkg); db.commit(); db.refresh(pkg)
    return {"id":pkg.id}

@app.put("/admin/pricing/packages/{pkg_id}")
def admin_update_package(pkg_id: int, body: PackageBody,
                          user: User = Depends(current_user),
                          db: Session = Depends(get_db)):
    require_admin(user)
    pkg = db.query(TokenPackage).filter_by(id=pkg_id).first()
    if not pkg: raise HTTPException(404)
    for k,v in body.dict().items(): setattr(pkg, k, v)
    db.commit()
    return {"status": "ok"}

@app.delete("/admin/pricing/packages/{pkg_id}")
def admin_delete_package(pkg_id: int, user: User = Depends(current_user),
                          db: Session = Depends(get_db)):
    require_admin(user)
    pkg = db.query(TokenPackage).filter_by(id=pkg_id).first()
    if not pkg: raise HTTPException(404)
    db.delete(pkg); db.commit()
    return {"status":"deleted"}

@app.post("/admin/faq")
def admin_add_faq(body: FaqBody, user: User = Depends(current_user),
                   db: Session = Depends(get_db)):
    require_admin(user)
    f = FaqItem(**body.dict())
    db.add(f); db.commit(); db.refresh(f)
    return {"id":f.id}

@app.put("/admin/faq/{faq_id}")
def admin_update_faq(faq_id: int, body: FaqBody,
                      user: User = Depends(current_user),
                      db: Session = Depends(get_db)):
    require_admin(user)
    f = db.query(FaqItem).filter_by(id=faq_id).first()
    if not f: raise HTTPException(404)
    for k,v in body.dict().items(): setattr(f, k, v)
    db.commit()
    return {"status":"ok"}

@app.delete("/admin/faq/{faq_id}")
def admin_delete_faq(faq_id: int, user: User = Depends(current_user),
                      db: Session = Depends(get_db)):
    require_admin(user)
    f = db.query(FaqItem).filter_by(id=faq_id).first()
    if not f: raise HTTPException(404)
    db.delete(f); db.commit()
    return {"status":"deleted"}

# ── Buy token package ─────────────────────────────────────────────────────────

class BuyPackageRequest(BaseModel):
    package_id: int
    return_url: str = "http://localhost:8000/?payment=success"

@app.post("/payment/buy-tokens")
def buy_tokens(req: BuyPackageRequest, user: User = Depends(current_user),
               db: Session = Depends(get_db)):
    if not user.is_verified: raise HTTPException(403, "Подтвердите email")
    pkg = db.query(TokenPackage).filter_by(id=req.package_id, is_active=True).first()
    if not pkg: raise HTTPException(404, "Пакет не найден")
    try:
        from yookassa import Configuration, Payment as YKP
        import uuid as _uuid
        p = YKP.create({
            "amount": {"value": str(float(pkg.price_rub)), "currency": "RUB"},
            "confirmation": {"type": "redirect", "return_url": req.return_url},
            "capture": True,
            "description": f"AI Студия Че — {pkg.name} ({pkg.tokens//1000}к CH)",
            "metadata": {"user_id": user.id, "package_id": pkg.id, "type": "tokens"},
        }, str(_uuid.uuid4()))
        return {"payment_id": p.id, "confirmation_url": p.confirmation.confirmation_url}
    except Exception as e:
        raise HTTPException(500, f"Ошибка платежа: {e}")

@app.get("/payment/confirm-tokens/{payment_id}")
def confirm_tokens(payment_id: str, user: User = Depends(current_user),
                   db: Session = Depends(get_db)):
    try: status = check_payment(payment_id)
    except Exception as e: raise HTTPException(500, str(e))
    if status != "succeeded": return {"status": status}

    existing = db.query(Transaction).filter_by(yookassa_payment_id=payment_id).first()
    if existing: return {"status": "already_credited"}

    from yookassa import Payment as YKP
    p = YKP.find_one(payment_id)
    pkg_id = int(p.metadata.get("package_id", 0))
    pkg = db.query(TokenPackage).filter_by(id=pkg_id).first()
    if not pkg: raise HTTPException(404)

    db_user = db.query(User).filter_by(id=user.id).first()
    db_user.tokens_balance += pkg.tokens
    db.add(Transaction(
        user_id=user.id, type="payment",
        amount_rub=pkg.price_rub, tokens_delta=pkg.tokens,
        description=f"Докупка токенов: {pkg.name}",
        yookassa_payment_id=payment_id,
    ))
    db.commit()
    return {"status": "credited", "tokens_added": pkg.tokens}

# ═══════════════════════════════════════════════════════════════════════════════
# AGENT ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

from agent_runner import (create_task, submit_task, tasks as agent_tasks,
                           init_agent_queue, TOOL_SCHEMAS)

@app.on_event("startup")
async def startup_agent():
    await init_agent_queue()

class AgentRunRequest(BaseModel):
    goal: str
    context: dict | None = None    # vk_token, tg_token, etc.

@app.post("/agent/run")
async def agent_run(req: AgentRunRequest,
                    user=Depends(optional_user),
                    db: Session = Depends(get_db)):
    if user:
        if not user.is_verified:
            raise HTTPException(403, "Подтвердите email")
        # Cost: 50 CH per agent task
        db_user = db.query(User).filter_by(id=user.id).first()
        if db_user.tokens_balance < 50:
            raise HTTPException(402, "Недостаточно токенов (нужно минимум 50 CH)")
        db_user.tokens_balance -= 50
        db.add(Transaction(
            user_id=user.id, type="usage", tokens_delta=-50,
            description=f"ИИ Агент: {req.goal[:50]}", model="agent"
        ))
        db.commit()

    ctx = req.context or {}
    if user:
        ctx["user_id"] = user.id

    task_id = create_task(user_id=user.id if user else None, goal=req.goal, context=ctx)
    await submit_task(task_id, req.goal, ctx)
    return {"task_id": task_id, "status": "queued"}

@app.get("/agent/{task_id}/status")
def agent_status(task_id: str):
    t = agent_tasks.get(task_id)
    if not t:
        raise HTTPException(404, "Задача не найдена")
    return {
        "task_id":    task_id,
        "status":     t["status"],
        "goal":       t["goal"],
        "steps":      t["steps"],
        "outputs":    t.get("outputs",[]),
        "result":     t.get("result"),
        "created_at": t.get("created_at"),
        "updated_at": t.get("updated_at"),
    }

@app.get("/agent/{task_id}/stream")
async def agent_stream(task_id: str):
    """SSE stream for real-time step updates."""
    from fastapi.responses import StreamingResponse
    import asyncio

    async def event_gen():
        last_step = 0
        for _ in range(300):   # ~5 min timeout
            await asyncio.sleep(1)
            t = agent_tasks.get(task_id)
            if not t:
                break
            # Send new steps
            while last_step < len(t["steps"]):
                step = t["steps"][last_step]
                data = json.dumps({"type":"step","step":step}, ensure_ascii=False)
                yield f"data: {data}\n\n"
                last_step += 1
            # Done?
            if t["status"] in ("done","error"):
                final = json.dumps({"type":"done","status":t["status"],
                                    "result":t.get("result","")}, ensure_ascii=False)
                yield f"data: {final}\n\n"
                break

    return StreamingResponse(event_gen(),
                             media_type="text/event-stream",
                             headers={"Cache-Control":"no-cache",
                                      "X-Accel-Buffering":"no"})

@app.get("/agent/tools/list")
def agent_tools():
    return TOOL_SCHEMAS

# ═══════════════════════════════════════════════════════════════════════════════
# EXCHANGE RATE — обновляется каждое утро
# ═══════════════════════════════════════════════════════════════════════════════
from models import PromoCode, PromoUse, ExchangeRate
import asyncio

async def update_exchange_rate():
    """Обновляем курс USD/RUB каждые 12 часов через ЦБ РФ API."""
    while True:
        try:
            import httpx
            r = httpx.get("https://www.cbr-xml-daily.ru/daily_json.js", timeout=10)
            data = r.json()
            usd_rate = data["Valute"]["USD"]["Value"]
            db = SessionLocal()
            try:
                rec = db.query(ExchangeRate).filter_by(currency="USD").first()
                if rec:
                    rec.rate_rub = usd_rate
                    rec.updated_at = datetime.utcnow()
                else:
                    db.add(ExchangeRate(currency="USD", rate_rub=usd_rate))
                db.commit()
            finally:
                db.close()
        except Exception as e:
            pass  # Используем кэшированный курс
        await asyncio.sleep(43200)  # 12 часов

@app.on_event("startup")
async def startup_exchange():
    asyncio.create_task(update_exchange_rate())

def get_usd_rate(db: Session) -> float:
    rec = db.query(ExchangeRate).filter_by(currency="USD").first()
    return rec.rate_rub if rec else 90.0

@app.get("/pricing/exchange-rate")
def get_rate(db: Session = Depends(get_db)):
    return {"usd_to_rub": get_usd_rate(db)}

# ── Token cost calculation ────────────────────────────────────────────────────
# Формула для языковых моделей: (цена_$ × 2 × курс_₽) / 0.4 = CH за запрос
# Фиксированные цены:
MODEL_USD_COST = {
    "gpt":             0.0001,   # GPT-4o mini ~$0.0001 per message
    "gpt-4o":          0.005,    # GPT-4o
    "claude":          0.0002,   # Claude Haiku
    "claude-sonnet":   0.006,    # Claude Sonnet
    "gemini":          0.00005,  # Gemini Flash
    "perplexity":      0.0002,
    "perplexity-large":0.001,
    # Фиксированные (не зависят от курса)
    "kling":           None,     # 200 CH fixed
    "kling-pro":       None,     # 400 CH fixed
    "veo":             None,     # 120 CH fixed
    "nano":            None,     # 10 CH fixed
}
FIXED_COSTS = {"kling":200,"kling-pro":400,"veo":120,"nano":10,"dalle":40}

def calc_tokens(model: str, usd_rate: float) -> int:
    fixed = FIXED_COSTS.get(model)
    if fixed: return fixed
    usd = MODEL_USD_COST.get(model, 0.001)
    if usd is None: return 200
    return max(1, round((usd * 2 * usd_rate) / 0.4))

@app.get("/pricing/token-costs")
def get_token_costs(db: Session = Depends(get_db)):
    rate = get_usd_rate(db)
    return {m: calc_tokens(m, rate) for m in MODEL_USD_COST}

# ═══════════════════════════════════════════════════════════════════════════════
# PROMO CODES
# ═══════════════════════════════════════════════════════════════════════════════

class PromoApplyBody(BaseModel):
    code: str

@app.post("/promo/apply")
def apply_promo(body: PromoApplyBody, user: User = Depends(current_user),
                db: Session = Depends(get_db)):
    code = db.query(PromoCode).filter_by(code=body.code.upper(), is_active=True).first()
    if not code:
        raise HTTPException(404, "Промокод не найден или неактивен")
    if code.used_count >= code.max_uses:
        raise HTTPException(400, "Промокод исчерпан")
    # Check not already used by this user
    used = db.query(PromoUse).filter_by(code_id=code.id, user_id=user.id).first()
    if used:
        raise HTTPException(400, "Промокод уже использован вами")
    # Apply
    code.used_count += 1
    db.add(PromoUse(code_id=code.id, user_id=user.id))
    if code.bonus_tokens:
        db.query(User).filter_by(id=user.id).first().tokens_balance += code.bonus_tokens
        db.add(Transaction(user_id=user.id, type="bonus", tokens_delta=code.bonus_tokens,
                           description=f"Промокод: {code.code}"))
    db.commit()
    return {"discount_pct": code.discount_pct, "bonus_tokens": code.bonus_tokens,
            "message": f"Промокод применён: {'-'+str(code.discount_pct)+'%' if code.discount_pct else ''} {'+'+str(code.bonus_tokens)+' CH' if code.bonus_tokens else ''}"}

@app.get("/admin/promos")
def admin_get_promos(user: User = Depends(current_user), db: Session = Depends(get_db)):
    require_admin(user)
    return [{"id":p.id,"code":p.code,"discount_pct":p.discount_pct,"bonus_tokens":p.bonus_tokens,
             "max_uses":p.max_uses,"used_count":p.used_count,"is_active":p.is_active} for p in db.query(PromoCode).all()]

class PromoBody(BaseModel):
    code: str
    discount_pct: int = 0
    bonus_tokens: int = 0
    max_uses: int = 100
    is_active: bool = True

@app.post("/admin/promos")
def admin_create_promo(body: PromoBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require_admin(user)
    p = PromoCode(code=body.code.upper(), discount_pct=body.discount_pct,
                  bonus_tokens=body.bonus_tokens, max_uses=body.max_uses, is_active=body.is_active)
    db.add(p); db.commit(); db.refresh(p)
    return {"id": p.id}

@app.put("/admin/promos/{pid}")
def admin_update_promo(pid: int, body: PromoBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require_admin(user)
    p = db.query(PromoCode).filter_by(id=pid).first()
    if not p: raise HTTPException(404)
    for k,v in body.dict().items(): setattr(p,k,v)
    p.code = p.code.upper()
    db.commit(); return {"status":"ok"}

@app.delete("/admin/promos/{pid}")
def admin_delete_promo(pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require_admin(user)
    p = db.query(PromoCode).filter_by(id=pid).first()
    if not p: raise HTTPException(404)
    db.delete(p); db.commit(); return {"status":"deleted"}

# ── Override plan prices with correct values ──────────────────────────────────
PLANS_V2 = [
    {"id":"starter", "name":"Старт",    "tokens":1000,  "price_rub":590},
    {"id":"pro",     "name":"Про",      "tokens":3000,  "price_rub":1590},
    {"id":"ultra",   "name":"Ультра",   "tokens":9000,  "price_rub":4590},
]
TOKEN_PACKAGES_V2 = [
    {"id":1, "name":"1 000 CH",  "tokens":1000, "price_rub":600},
    {"id":2, "name":"2 000 CH",  "tokens":2000, "price_rub":1150},
    {"id":3, "name":"5 000 CH",  "tokens":5000, "price_rub":2700},
]

@app.get("/plans")
def get_plans_v2():
    return PLANS_V2

@app.get("/pricing/packages")
def get_packages_v2():
    return TOKEN_PACKAGES_V2
