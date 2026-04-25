import os, json, uuid, logging
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from server.routes.deps import get_db, current_user, _user_dict, _sub_dict
from server.models import User, Subscription, Transaction, PromoUse, PromoCode, TokenPackage
from server.payments import create_payment, check_payment, get_plan, PLANS, credit_referral_bonus
from server.billing import credit_atomic

log = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["payments"])


_DEFAULT_RETURN_URL = os.getenv("APP_URL", "http://localhost:8000").rstrip("/") + "/?payment=success"


class BuyPlanRequest(BaseModel):
    plan: str
    return_url: str = _DEFAULT_RETURN_URL
    promo_code: str | None = None


class BuyTokenRequest(BaseModel):
    package_id: int
    return_url: str = _DEFAULT_RETURN_URL


class BuyPackageRequest(BaseModel):
    package_id: int
    return_url: str = _DEFAULT_RETURN_URL


@router.get("/plans", tags=["payments"])
def list_plans():
    return [{"id": k, "name": v["name"], "price_rub": v["price_rub"],
             "tokens": v["tokens"], "tokens_fmt": f"{v['tokens']//1000}к"}
            for k, v in PLANS.items()]


@router.post("/payment/create")
def payment_create(req: BuyPlanRequest, user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    if not user.is_verified:
        raise HTTPException(403, "Подтвердите email для оплаты")
    if req.plan not in PLANS:
        raise HTTPException(400, f"Неизвестный план: {req.plan}")
    promo = req.promo_code
    discount_pct = 0
    if not promo:
        used = db.query(PromoUse).filter_by(user_id=user.id).first()
        if not used:
            promo_code_rec = db.query(PromoCode).filter_by(is_active=True).first()
            if promo_code_rec and promo_code_rec.used_count < promo_code_rec.max_uses:
                promo = promo_code_rec.code
                discount_pct = promo_code_rec.discount_pct
    else:
        promo_code_rec = db.query(PromoCode).filter_by(code=promo.upper(), is_active=True).first()
        if promo_code_rec and promo_code_rec.used_count < promo_code_rec.max_uses:
            discount_pct = promo_code_rec.discount_pct
        else:
            promo = None
    try:
        return create_payment(req.plan, user.id, req.return_url, user.email,
                              promo, discount_pct)
    except Exception as e:
        raise HTTPException(500, f"Ошибка платежа: {e}")


@router.get("/payment/confirm/{payment_id}")
def payment_confirm(payment_id: str, user: User = Depends(current_user),
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
    # Защита от кражи: payment_id должен принадлежать текущему пользователю
    meta_user_id = (p.metadata or {}).get("user_id")
    if str(meta_user_id) != str(user.id):
        log.warning(f"User {user.id} tried to confirm payment {payment_id} of user {meta_user_id}")
        raise HTTPException(403, "Этот платёж принадлежит другому пользователю")
    plan = (p.metadata or {}).get("plan", "starter")
    plan_cfg = get_plan(plan)
    db_user = db.query(User).filter_by(id=user.id).first()
    credit_atomic(db, user.id, plan_cfg["tokens"])
    credit_referral_bonus(db, db_user, plan_cfg["tokens"], plan_cfg["name"])
    sub = Subscription(
        user_id=user.id, plan=plan, tokens_total=plan_cfg["tokens"],
        price_rub=p.amount.value if p.amount else plan_cfg["price_rub"],
        status="active",
        yookassa_payment_id=payment_id,
        expires_at=datetime.utcnow() + timedelta(days=30),
    )
    db.add(sub)
    db.add(Transaction(
        user_id=user.id, type="payment",
        amount_rub=p.amount.value if p.amount else plan_cfg["price_rub"],
        tokens_delta=plan_cfg["tokens"],
        description=f"Подписка «{plan_cfg['name']}»",
        yookassa_payment_id=payment_id,
    ))
    db.commit()
    db.refresh(sub)
    return {"status": "activated", "subscription": _sub_dict(sub)}


@router.post("/payment/webhook")
async def payment_webhook(request: Request, db: Session = Depends(get_db)):
    """ЮKassa webhook — автоматическое зачисление/списание токенов."""
    import hashlib, hmac

    # ВАЖНО: читаем raw body ДО json — иначе stream съедается и HMAC будет от пустых байт.
    raw_body = await request.body()
    try:
        body = json.loads(raw_body)
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    hmac_header = request.headers.get("X-Content-Signature")
    secret = os.getenv("YOOKASSA_SECRET_KEY", "")
    if secret:
        # Если secret настроен — подпись обязательна, no exceptions.
        if not hmac_header:
            log.warning("Webhook: missing X-Content-Signature while secret is configured")
            raise HTTPException(401, "Signature required")
        import re
        match = re.match(r"^sha256=([0-9a-f]{64})$", hmac_header)
        if not match:
            log.warning("Webhook: malformed X-Content-Signature")
            raise HTTPException(401, "Malformed signature")
        computed = hmac.new(
            secret.encode(),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(computed, match.group(1)):
            log.warning("Webhook: invalid HMAC signature")
            raise HTTPException(401, "Invalid signature")

    obj = body.get("object", body)
    payment_id = obj.get("id")
    if not payment_id:
        raise HTTPException(400, "No payment id in webhook")

    # Источник истины — ЮKassa API. Если не подтвердили статус через API, НЕ зачисляем
    # (тело webhook без подписи можно подделать).
    from yookassa import Payment as YKP
    try:
        p = YKP.find_one(payment_id)
    except Exception as e:
        log.error(f"Webhook: cannot verify payment {payment_id} via YooKassa API: {e}")
        # Возвращаем 200, чтобы YooKassa не ретраила бесконечно, но НЕ зачисляем
        return {"status": "verification_failed"}
    if p.status != "succeeded":
        return {"status": "not_yet_paid"}
    yk_meta = p.metadata or {}
    amount = float(p.amount.value) if p.amount else 0

    existing = db.query(Subscription).filter_by(yookassa_payment_id=payment_id).first()
    if existing:
        return {"status": "already_activated"}

    user_id = yk_meta.get("user_id")
    if not user_id:
        return {"status": "no_user_id"}

    db_user = db.query(User).filter_by(id=int(user_id)).first()
    if not db_user:
        return {"status": "user_not_found"}

    pay_type = yk_meta.get("type", "subscription")
    plan = yk_meta.get("plan", "starter")

    if pay_type == "tokens":
        existing_tx = db.query(Transaction).filter_by(yookassa_payment_id=payment_id).first()
        if existing_tx:
            return {"status": "already_credited"}

        pkg_id = int(yk_meta.get("package_id", 0))
        pkg = db.query(TokenPackage).filter_by(id=pkg_id).first()
        if not pkg:
            _pkgs = {1: ("1 000", 1000), 2: ("2 000", 2000), 3: ("5 000", 5000)}
            name_fmt, tokens = _pkgs.get(pkg_id, (f"{int(amount*2)} CH", int(amount*2)))
            pkg_name = name_fmt
        else:
            tokens = pkg.tokens
            pkg_name = pkg.name

        credit_atomic(db, db_user.id, tokens)
        credit_referral_bonus(db, db_user, tokens, pkg_name)
        db.add(Transaction(
            user_id=db_user.id, type="payment", amount_rub=amount,
            tokens_delta=tokens,
            description=f"Докупка токенов: {pkg_name} (webhook)",
            yookassa_payment_id=payment_id,
        ))
        db.commit()
        log.info(f"Webhook: credited {tokens} tokens for user {user_id}")
        return {"status": "ok"}

    plan_cfg = get_plan(plan)
    credit_atomic(db, db_user.id, plan_cfg["tokens"])
    credit_referral_bonus(db, db_user, plan_cfg["tokens"], plan_cfg["name"])
    sub = Subscription(
        user_id=db_user.id, plan=plan, tokens_total=plan_cfg["tokens"],
        price_rub=amount, status="active",
        yookassa_payment_id=payment_id,
        expires_at=datetime.utcnow() + timedelta(days=30),
    )
    db.add(sub)
    db.add(Transaction(
        user_id=db_user.id, type="payment", amount_rub=amount,
        tokens_delta=plan_cfg["tokens"],
        description=f"Подписка «{plan_cfg['name']}» (webhook)",
        yookassa_payment_id=payment_id,
    ))
    db.commit()
    log.info(f"Webhook: activated {plan} for user {user_id}")
    return {"status": "ok"}


@router.post("/payment/buy-tokens")
def buy_tokens(req: BuyTokenRequest, user: User = Depends(current_user),
               db: Session = Depends(get_db)):
    if not user.is_verified:
        raise HTTPException(403, "Подтвердите email для оплаты")
    pkg = db.query(TokenPackage).filter_by(id=req.package_id, is_active=True).first()
    if not pkg:
        raise HTTPException(404, "Пакет не найден")
    try:
        from yookassa import Payment as YKP
        payment_data = {
            "amount": {"value": str(float(pkg.price_rub)), "currency": "RUB"},
            "confirmation": {"type": "redirect", "return_url": req.return_url},
            "capture": True,
            "description": f"AI Студия Че — {pkg.name} (user {user.id})",
            "metadata": {
                "user_id": str(user.id),
                "type": "tokens",
                "package_id": str(pkg.id),
            },
        }
        if user.email:
            payment_data["receipt"] = {
                "customer": {"email": user.email},
                "items": [{
                    "description": f"Пакет токенов: {pkg.name}",
                    "quantity": "1",
                    "amount": {"value": str(float(pkg.price_rub)), "currency": "RUB"},
                    "vat_code": "1",
                }],
            }
        p = YKP.create(payment_data, str(uuid.uuid4()))
        return {
            "payment_id": p.id,
            "confirmation_url": p.confirmation.confirmation_url,
            "status": p.status,
        }
    except Exception as e:
        raise HTTPException(500, f"Ошибка платежа: {e}")


@router.get("/payment/confirm-tokens/{payment_id}")
def confirm_tokens(payment_id: str, user: User = Depends(current_user),
                   db: Session = Depends(get_db)):
    try:
        status = check_payment(payment_id)
    except Exception as e:
        raise HTTPException(500, str(e))
    if status != "succeeded":
        return {"status": status}

    existing = db.query(Transaction).filter_by(yookassa_payment_id=payment_id).first()
    if existing:
        return {"status": "already_credited"}

    from yookassa import Payment as YKP
    p = YKP.find_one(payment_id)
    # Защита от кражи: payment_id должен принадлежать текущему пользователю
    meta_user_id = (p.metadata or {}).get("user_id")
    if str(meta_user_id) != str(user.id):
        log.warning(f"User {user.id} tried to confirm token-payment {payment_id} of user {meta_user_id}")
        raise HTTPException(403, "Этот платёж принадлежит другому пользователю")
    pkg_id = int(p.metadata.get("package_id", 0))
    pkg = db.query(TokenPackage).filter_by(id=pkg_id).first()
    if not pkg:
        raise HTTPException(404, "Пакет не найден")

    db_user = db.query(User).filter_by(id=user.id).first()
    credit_atomic(db, user.id, pkg.tokens)
    credit_referral_bonus(db, db_user, pkg.tokens, pkg.name)
    db.add(Transaction(
        user_id=user.id, type="payment",
        amount_rub=pkg.price_rub, tokens_delta=pkg.tokens,
        description=f"Докупка токенов: {pkg.name}",
        yookassa_payment_id=payment_id,
    ))
    db.commit()
    return {"status": "credited", "tokens_added": pkg.tokens}
