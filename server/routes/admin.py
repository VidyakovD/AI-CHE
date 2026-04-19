"""Admin endpoints — extracted from main.py."""
import os, json, logging
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func

from server.routes.deps import get_db, current_user, _user_dict, _sub_dict, _tx_dict
from server.models import (
    User, Message, Transaction, ApiKey,
    Solution, SolutionCategory, SolutionStep,
    Subscription, SupportRequest, PricingSetting,
    ModelPricing, TokenPackage, FaqItem, FeatureFlag,
    UsageLog,
)
from server.security import require_admin
from server.db import SessionLocal
from server.ai import invalidate_api_key_cache

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

# ── helpers ────────────────────────────────────────────────────────────────────

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


# ── Pydantic models ───────────────────────────────────────────────────────────

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


class ApiKeyBody(BaseModel):
    provider: str
    key_value: str
    label: str | None = None


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


class PromoBody(BaseModel):
    code: str
    discount_pct: int = 0
    bonus_tokens: int = 0
    max_uses: int = 100
    is_active: bool = True


# ── Constants ─────────────────────────────────────────────────────────────────

PROVIDERS_LIST = [
    "openai", "anthropic", "gemini", "perplexity", "kling",
    "google", "veo_project_id", "grok", "yookassa", "youtube",
]

# ── Admin: Users ──────────────────────────────────────────────────────────────

@router.get("/users")
def admin_users(user: User = Depends(current_user), db: Session = Depends(get_db)):
    require_admin(user)
    users = db.query(User).order_by(User.created_at.desc()).limit(200).all()
    return [_user_dict(u) for u in users]


@router.get("/stats")
def admin_stats(user: User = Depends(current_user), db: Session = Depends(get_db)):
    require_admin(user)
    return {
        "total_users":    db.query(User).count(),
        "verified_users": db.query(User).filter_by(is_verified=True).count(),
        "total_messages": db.query(Message).count(),
        "total_revenue":  db.query(Transaction).filter_by(type="payment")
                            .with_entities(func.sum(Transaction.amount_rub)).scalar() or 0,
    }


@router.get("/usage")
def admin_usage_stats(days: int = 30, user: User = Depends(current_user),
                      db: Session = Depends(get_db)):
    """Статистика использования моделей — токены и CH по каждой модели."""
    require_admin(user)
    from datetime import datetime, timedelta
    since = datetime.utcnow() - timedelta(days=days)
    rows = db.query(
        UsageLog.model,
        func.count(UsageLog.id).label("requests"),
        func.sum(UsageLog.input_tokens).label("input_tokens"),
        func.sum(UsageLog.output_tokens).label("output_tokens"),
        func.sum(UsageLog.cached_tokens).label("cached_tokens"),
        func.sum(UsageLog.ch_charged).label("ch_charged"),
    ).filter(UsageLog.created_at >= since).group_by(UsageLog.model).all()

    total_ch = sum(r.ch_charged or 0 for r in rows)
    total_requests = sum(r.requests or 0 for r in rows)

    return {
        "days": days,
        "total_requests": total_requests,
        "total_ch_charged": total_ch,
        "per_model": [
            {
                "model": r.model,
                "requests": r.requests or 0,
                "input_tokens": r.input_tokens or 0,
                "output_tokens": r.output_tokens or 0,
                "cached_tokens": r.cached_tokens or 0,
                "ch_charged": r.ch_charged or 0,
                "avg_ch_per_req": round((r.ch_charged or 0) / (r.requests or 1), 2),
            } for r in rows
        ],
    }


# ── Admin: Solutions CRUD ─────────────────────────────────────────────────────

@router.post("/categories")
def admin_create_category(body: CategoryBody, user: User = Depends(current_user),
                          db: Session = Depends(get_db)):
    require_admin(user)
    cat = SolutionCategory(**body.model_dump())
    db.add(cat); db.commit(); db.refresh(cat)
    return {"id": cat.id, "slug": cat.slug, "title": cat.title}


@router.post("/solutions")
def admin_create_solution(body: SolutionBody, user: User = Depends(current_user),
                          db: Session = Depends(get_db)):
    require_admin(user)
    sol = Solution(**body.model_dump())
    db.add(sol); db.commit(); db.refresh(sol)
    return _sol_dict(sol)


@router.put("/solutions/{solution_id}")
def admin_update_solution(solution_id: int, body: SolutionBody,
                          user: User = Depends(current_user), db: Session = Depends(get_db)):
    require_admin(user)
    sol = db.query(Solution).filter_by(id=solution_id).first()
    if not sol:
        raise HTTPException(404)
    for k, v in body.model_dump().items():
        setattr(sol, k, v)
    db.commit()
    return _sol_dict(sol)


@router.delete("/solutions/{solution_id}")
def admin_delete_solution(solution_id: int, user: User = Depends(current_user),
                          db: Session = Depends(get_db)):
    require_admin(user)
    sol = db.query(Solution).filter_by(id=solution_id).first()
    if not sol:
        raise HTTPException(404)
    db.delete(sol); db.commit()
    return {"status": "deleted"}


@router.post("/solutions/{solution_id}/steps")
def admin_add_step(solution_id: int, body: StepBody, user: User = Depends(current_user),
                   db: Session = Depends(get_db)):
    require_admin(user)
    sol = db.query(Solution).filter_by(id=solution_id).first()
    if not sol:
        raise HTTPException(404)
    d = body.model_dump()
    if d.get("extra_params"):
        d["extra_params"] = json.dumps(d["extra_params"])
    step = SolutionStep(solution_id=solution_id, **d)
    db.add(step); db.commit(); db.refresh(step)
    return _step_dict(step)


@router.put("/steps/{step_id}")
def admin_update_step(step_id: int, body: StepBody, user: User = Depends(current_user),
                      db: Session = Depends(get_db)):
    require_admin(user)
    step = db.query(SolutionStep).filter_by(id=step_id).first()
    if not step:
        raise HTTPException(404)
    d = body.model_dump()
    if d.get("extra_params"):
        d["extra_params"] = json.dumps(d["extra_params"])
    for k, v in d.items():
        setattr(step, k, v)
    db.commit()
    return _step_dict(step)


@router.delete("/steps/{step_id}")
def admin_delete_step(step_id: int, user: User = Depends(current_user),
                      db: Session = Depends(get_db)):
    require_admin(user)
    step = db.query(SolutionStep).filter_by(id=step_id).first()
    if not step:
        raise HTTPException(404)
    db.delete(step); db.commit()
    return {"status": "deleted"}


# ── Admin: API Keys Management ────────────────────────────────────────────────

@router.get("/apikeys")
def admin_get_keys(user: User = Depends(current_user), db: Session = Depends(get_db)):
    require_admin(user)
    keys = db.query(ApiKey).order_by(ApiKey.provider, ApiKey.id).all()
    return [{
        "id": k.id, "provider": k.provider, "label": k.label,
        "key_preview": k.key_value[:8] + "..." + k.key_value[-4:] if len(k.key_value) > 12 else "***",
        "status": k.status, "last_error": k.last_error,
        "last_check": k.last_check.isoformat() if k.last_check else None,
    } for k in keys]


@router.post("/apikeys")
def admin_add_key(body: ApiKeyBody, user: User = Depends(current_user),
                  db: Session = Depends(get_db)):
    require_admin(user)
    if body.provider not in PROVIDERS_LIST:
        raise HTTPException(400, f"Неизвестный провайдер: {body.provider}")
    key = ApiKey(provider=body.provider, key_value=body.key_value.strip(),
                 label=body.label, status="unknown")
    db.add(key); db.commit(); db.refresh(key)
    _rebuild_env_keys(body.provider, db)
    invalidate_api_key_cache(body.provider)
    return {"id": key.id, "status": "added"}


@router.delete("/apikeys/{key_id}")
def admin_delete_key(key_id: int, user: User = Depends(current_user),
                     db: Session = Depends(get_db)):
    require_admin(user)
    key = db.query(ApiKey).filter_by(id=key_id).first()
    if not key:
        raise HTTPException(404)
    provider = key.provider
    db.delete(key); db.commit()
    _rebuild_env_keys(provider, db)
    invalidate_api_key_cache(provider)
    return {"status": "deleted"}


@router.post("/apikeys/{key_id}/check")
def admin_check_key(key_id: int, user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    require_admin(user)
    key = db.query(ApiKey).filter_by(id=key_id).first()
    if not key:
        raise HTTPException(404)
    status, error = _test_key(key.provider, key.key_value)
    key.status = status
    key.last_error = error
    key.last_check = datetime.utcnow()
    db.commit()
    return {"status": status, "error": error}


@router.post("/apikeys/check-all")
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


# ── API Key test / rebuild helpers ────────────────────────────────────────────

def _test_key(provider: str, key_value: str) -> tuple[str, str | None]:
    """Проверяет ключ отправкой минимального запроса."""
    try:
        if provider == "openai":
            from openai import OpenAI
            c = OpenAI(api_key=key_value)
            c.chat.completions.create(model="gpt-4o-mini",
                messages=[{"role": "user", "content": "hi"}], max_tokens=1)
            return "ok", None
        elif provider == "anthropic":
            import anthropic as _ant
            base_url = os.getenv("ANTHROPIC_BASE_URL")
            kwargs = {"api_key": key_value}
            if base_url:
                kwargs["base_url"] = base_url
            c = _ant.Anthropic(**kwargs)
            c.messages.create(model="claude-sonnet-4-20250514",
                max_tokens=1, messages=[{"role": "user", "content": "hi"}])
            return "ok", None
        elif provider in ("gemini", "google", "nano", "veo"):
            import httpx
            r = httpx.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={key_value}",
                json={"contents": [{"parts": [{"text": "hi"}]}]}, timeout=10)
            return ("ok", None) if r.status_code < 400 else ("error", f"HTTP {r.status_code}: {r.text[:100]}")
        elif provider == "perplexity":
            from openai import OpenAI
            c = OpenAI(api_key=key_value, base_url="https://api.perplexity.ai")
            c.chat.completions.create(model="sonar-small-chat",
                messages=[{"role": "user", "content": "hi"}], max_tokens=1)
            return "ok", None
        elif provider == "kling":
            import httpx, time, jwt as _jwt
            if "," in key_value:
                ak, sk = key_value.split(",", 1)
                token = _jwt.encode(
                    {"iss": ak.strip(), "exp": int(time.time()) + 1800, "nbf": int(time.time()) - 5},
                    sk.strip(),
                    headers={"alg": "HS256", "typ": "JWT"}
                )
                r = httpx.get("https://api.klingai.com/v1/videos/text2video",
                    headers={"Authorization": f"Bearer {token}"}, timeout=8)
                if r.status_code == 401:
                    return "error", f"Неверный ключ: {r.text[:100]}"
                return ("ok", None) if r.status_code != 401 else ("error", f"HTTP {r.status_code}")
            return "error", "Формат Kling: ak_XXX,sk_YYY"
        elif provider == "veo_project_id":
            project_id = key_value.strip()
            if not project_id or len(project_id) < 3:
                return "error", "Project ID слишком короткий"
            return "ok", None
        elif provider == "grok":
            from openai import OpenAI
            c = OpenAI(api_key=key_value, base_url="https://api.x.ai/v1")
            c.chat.completions.create(model="grok-3-mini",
                messages=[{"role": "user", "content": "hi"}], max_tokens=1)
            return "ok", None
        elif provider == "yookassa":
            if ":" not in key_value:
                return "error", "Формат: shop_id:secret_key"
            shop_id, secret = key_value.split(":", 1)
            from yookassa import Configuration as YKConf
            YKConf.account_id = shop_id.strip()
            YKConf.secret_key = secret.strip()
            return "ok", None
        elif provider == "youtube":
            import httpx
            r = httpx.get(
                f"https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true&key={key_value}",
                timeout=10)
            return ("ok", None) if r.status_code == 200 else ("error", f"HTTP {r.status_code}: {r.text[:100]}")
        else:
            return "unknown", "Проверка не реализована"
    except Exception as e:
        return "error", str(e)[:200]


def _rebuild_env_keys(provider: str, db: Session):
    """Пересобирает env-переменную из БД ключей."""
    ENV_MAP = {
        "openai":         "OPENAI_API_KEYS",
        "anthropic":      "ANTHROPIC_API_KEYS",
        "google":         "GOOGLE_API_KEYS",
        "gemini":         "GOOGLE_API_KEYS",
        "nano":           "GOOGLE_API_KEYS",
        "veo":            "GOOGLE_API_KEYS",
        "grok":           "GROK_API_KEYS",
        "veo_project_id": "VEO_PROJECT_ID",
        "youtube":        "YOUTUBE_API_KEYS",
        "kling":          "KLING_API_KEYS",
    }
    env_var = ENV_MAP.get(provider)
    if env_var:
        if env_var == "GOOGLE_API_KEYS":
            all_keys = db.query(ApiKey).filter(ApiKey.provider.in_(["gemini", "google", "nano", "veo"])).all()
        else:
            all_keys = db.query(ApiKey).filter_by(provider=provider).all()
        if provider == "kling":
            value = ";;".join(k.key_value for k in all_keys)
        else:
            value = ",".join(k.key_value for k in all_keys)
        # Не затираем env если в БД нет ключей — возможно они есть в .env
        if value:
            os.environ[env_var] = value

    if provider == "yookassa":
        key = db.query(ApiKey).filter_by(provider="yookassa").first()
        if key and ":" in key.key_value:
            shop_id, secret = key.key_value.split(":", 1)
            from yookassa import Configuration as YKConf
            YKConf.account_id = shop_id.strip()
            YKConf.secret_key = secret.strip()


def _load_all_apikeys_from_db():
    """При старте загружаем ВСЕ API ключи из БД в env."""
    db = SessionLocal()
    try:
        for provider in PROVIDERS_LIST:
            _rebuild_env_keys(provider, db)
        # TG bot settings for error notifications
        for setting in db.query(PricingSetting).filter(
            PricingSetting.key.in_(["tg_bot_token", "tg_admin_chat_id",
                                    "anthropic_base_url", "error_webhook_url"])
        ).all():
            os.environ[setting.key.upper()] = setting.value
    finally:
        db.close()


# ── Admin: Users (full with balance) ──────────────────────────────────────────

@router.get("/users/full")
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


@router.post("/users/{user_id}/adjust-balance")
def admin_adjust_balance(user_id: int, body: dict,
                         user: User = Depends(current_user),
                         db: Session = Depends(get_db)):
    require_admin(user)
    delta = int(body.get("delta", 0))
    reason = body.get("reason", "Ручная корректировка")
    target = db.query(User).filter_by(id=user_id).first()
    if not target:
        raise HTTPException(404)
    from server.billing import credit_atomic, deduct_atomic
    if delta > 0:
        credit_atomic(db, user_id, delta)
    elif delta < 0:
        deduct_atomic(db, user_id, -delta)
    db.add(Transaction(user_id=user_id, type="bonus" if delta > 0 else "usage",
                       tokens_delta=delta, description=reason))
    db.commit()
    db.refresh(target)
    return {"tokens_balance": target.tokens_balance}


@router.post("/users/{user_id}/toggle-ban")
def admin_toggle_ban(user_id: int, body: dict,
                     user: User = Depends(current_user),
                     db: Session = Depends(get_db)):
    """Бан / разбан пользователя (п. 10.1 оферты)."""
    require_admin(user)
    target = db.query(User).filter_by(id=user_id).first()
    if not target:
        raise HTTPException(404)
    target.is_banned = not target.is_banned
    db.commit()
    return {"user_id": target.id, "is_banned": target.is_banned}


# ── Admin: Support Requests ──────────────────────────────────────────────────

@router.get("/support-requests")
def admin_list_support_requests(user: User = Depends(current_user),
                                 db: Session = Depends(get_db)):
    require_admin(user)
    requests = db.query(SupportRequest).order_by(SupportRequest.created_at.desc()).all()
    return [{"id": r.id, "user_id": r.user_id, "type": r.type,
             "description": r.description, "status": r.status,
             "admin_response": r.admin_response,
             "created_at": r.created_at.isoformat(),
             "updated_at": r.updated_at.isoformat() if r.updated_at else None}
            for r in requests]


@router.post("/support-requests/{request_id}")
def admin_respond_support(request_id: int, body: dict,
                           user: User = Depends(current_user),
                           db: Session = Depends(get_db)):
    require_admin(user)
    req = db.query(SupportRequest).filter_by(id=request_id).first()
    if not req:
        raise HTTPException(404)
    if body.get("status"):
        req.status = body["status"]
    if body.get("admin_response"):
        req.admin_response = body["admin_response"]
    db.commit(); db.refresh(req)
    return {"id": req.id, "status": req.status, "admin_response": req.admin_response}


# ── Admin: Feature Flags ─────────────────────────────────────────────────────

@router.get("/features")
def admin_get_features(user: User = Depends(current_user), db: Session = Depends(get_db)):
    require_admin(user)
    flags = db.query(FeatureFlag).order_by(FeatureFlag.id).all()
    return [{"key": f.key, "label": f.label, "description": f.description,
             "enabled": f.enabled} for f in flags]


@router.post("/features/{key}")
def admin_toggle_feature(key: str, body: dict,
                         user: User = Depends(current_user),
                         db: Session = Depends(get_db)):
    require_admin(user)
    flag = db.query(FeatureFlag).filter_by(key=key).first()
    if not flag:
        raise HTTPException(404, "Флаг не найден")
    flag.enabled = bool(body.get("enabled", not flag.enabled))
    db.commit()
    return {"key": flag.key, "enabled": flag.enabled}


# ── Admin: Pricing ────────────────────────────────────────────────────────────

@router.put("/pricing/models/{model_id}")
def admin_update_model_price(model_id: str, body: ModelPricingBody,
                              user: User = Depends(current_user),
                              db: Session = Depends(get_db)):
    require_admin(user)
    p = db.query(ModelPricing).filter_by(model_id=model_id).first()
    if not p:
        raise HTTPException(404)
    p.cost_per_req = body.cost_per_req
    p.usd_per_req  = body.usd_per_req
    p.markup       = body.markup
    db.commit()
    return {"status": "ok"}


@router.put("/pricing/settings/{key}")
def admin_update_setting(key: str, body: SettingBody,
                          user: User = Depends(current_user),
                          db: Session = Depends(get_db)):
    require_admin(user)
    p = db.query(PricingSetting).filter_by(key=key).first()
    if not p:
        raise HTTPException(404)
    p.value = body.value
    db.commit()
    return {"status": "ok"}


@router.post("/pricing/packages")
def admin_add_package(body: PackageBody, user: User = Depends(current_user),
                       db: Session = Depends(get_db)):
    require_admin(user)
    pkg = TokenPackage(**body.model_dump())
    db.add(pkg); db.commit(); db.refresh(pkg)
    return {"id": pkg.id}


@router.put("/pricing/packages/{pkg_id}")
def admin_update_package(pkg_id: int, body: PackageBody,
                          user: User = Depends(current_user),
                          db: Session = Depends(get_db)):
    require_admin(user)
    pkg = db.query(TokenPackage).filter_by(id=pkg_id).first()
    if not pkg:
        raise HTTPException(404)
    for k, v in body.model_dump().items():
        setattr(pkg, k, v)
    db.commit()
    return {"status": "ok"}


@router.delete("/pricing/packages/{pkg_id}")
def admin_delete_package(pkg_id: int, user: User = Depends(current_user),
                          db: Session = Depends(get_db)):
    require_admin(user)
    pkg = db.query(TokenPackage).filter_by(id=pkg_id).first()
    if not pkg:
        raise HTTPException(404)
    db.delete(pkg); db.commit()
    return {"status": "deleted"}


# ── Admin: FAQ ────────────────────────────────────────────────────────────────

@router.post("/faq")
def admin_add_faq(body: FaqBody, user: User = Depends(current_user),
                   db: Session = Depends(get_db)):
    require_admin(user)
    f = FaqItem(**body.model_dump())
    db.add(f); db.commit(); db.refresh(f)
    return {"id": f.id}


@router.put("/faq/{faq_id}")
def admin_update_faq(faq_id: int, body: FaqBody,
                      user: User = Depends(current_user),
                      db: Session = Depends(get_db)):
    require_admin(user)
    f = db.query(FaqItem).filter_by(id=faq_id).first()
    if not f:
        raise HTTPException(404)
    for k, v in body.model_dump().items():
        setattr(f, k, v)
    db.commit()
    return {"status": "ok"}


@router.delete("/faq/{faq_id}")
def admin_delete_faq(faq_id: int, user: User = Depends(current_user),
                      db: Session = Depends(get_db)):
    require_admin(user)
    f = db.query(FaqItem).filter_by(id=faq_id).first()
    if not f:
        raise HTTPException(404)
    db.delete(f); db.commit()
    return {"status": "deleted"}


# ── Admin: Promo Codes ────────────────────────────────────────────────────────

from server.models import PromoCode


@router.get("/promos")
def admin_get_promos(user: User = Depends(current_user), db: Session = Depends(get_db)):
    require_admin(user)
    return [{"id": p.id, "code": p.code, "discount_pct": p.discount_pct,
             "bonus_tokens": p.bonus_tokens, "max_uses": p.max_uses,
             "used_count": p.used_count, "is_active": p.is_active}
            for p in db.query(PromoCode).all()]


@router.post("/promos")
def admin_create_promo(body: PromoBody, user: User = Depends(current_user),
                        db: Session = Depends(get_db)):
    require_admin(user)
    p = PromoCode(code=body.code.upper(), discount_pct=body.discount_pct,
                  bonus_tokens=body.bonus_tokens, max_uses=body.max_uses,
                  is_active=body.is_active)
    db.add(p); db.commit(); db.refresh(p)
    return {"id": p.id}


@router.put("/promos/{pid}")
def admin_update_promo(pid: int, body: PromoBody,
                        user: User = Depends(current_user),
                        db: Session = Depends(get_db)):
    require_admin(user)
    p = db.query(PromoCode).filter_by(id=pid).first()
    if not p:
        raise HTTPException(404)
    for k, v in body.model_dump().items():
        setattr(p, k, v)
    p.code = p.code.upper()
    db.commit()
    return {"status": "ok"}


@router.delete("/promos/{pid}")
def admin_delete_promo(pid: int, user: User = Depends(current_user),
                        db: Session = Depends(get_db)):
    require_admin(user)
    p = db.query(PromoCode).filter_by(id=pid).first()
    if not p:
        raise HTTPException(404)
    db.delete(p); db.commit()
    return {"status": "deleted"}


# ── Admin: Presentation Templates ─────────────────────────────────────────────

from server.models import PresentationTemplate


@router.post("/presentations/templates")
def admin_create_pres_template(body: dict, user: User = Depends(current_user),
                                db: Session = Depends(get_db)):
    require_admin(user)
    t = PresentationTemplate(
        title=body.get("title", ""),
        description=body.get("description", ""),
        header_html=body.get("header_html", ""),
        pricing_json=json.dumps(body.get("pricing", {})),
        spec_prompt=body.get("spec_prompt", ""),
        style_css=body.get("style_css", ""),
        input_fields=json.dumps(body.get("input_fields", [])),
        is_active=body.get("is_active", True),
        sort_order=body.get("sort_order", 0),
    )
    db.add(t); db.commit(); db.refresh(t)
    return {"id": t.id, "status": "created"}
