import os, json, uuid, logging
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from server.routes.deps import get_db, current_user, _user_dict
from server.models import User, Transaction, TokenPackage
from server.payments import check_payment, credit_referral_bonus
from server.billing import credit_atomic

log = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["payments"])


_DEFAULT_RETURN_URL = os.getenv("APP_URL", "http://localhost:8000").rstrip("/") + "/?payment=success"


class BuyTokenRequest(BaseModel):
    package_id: int
    return_url: str = _DEFAULT_RETURN_URL


@router.post("/payment/webhook")
async def payment_webhook(request: Request, db: Session = Depends(get_db)):
    """ЮKassa webhook — атомарное зачисление баланса (в копейках)."""
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
        if not hmac_header:
            log.warning("Webhook: missing X-Content-Signature while secret is configured")
            raise HTTPException(401, "Signature required")
        import re
        match = re.match(r"^sha256=([0-9a-f]{64})$", hmac_header)
        if not match:
            raise HTTPException(401, "Malformed signature")
        computed = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(computed, match.group(1)):
            raise HTTPException(401, "Invalid signature")

    obj = body.get("object", body)
    payment_id = obj.get("id")
    if not payment_id:
        raise HTTPException(400, "No payment id in webhook")

    from yookassa import Payment as YKP
    try:
        p = YKP.find_one(payment_id)
    except Exception as e:
        log.error(f"Webhook: cannot verify payment {payment_id}: {e}")
        return {"status": "verification_failed"}
    if p.status != "succeeded":
        return {"status": "not_yet_paid"}

    yk_meta = p.metadata or {}
    amount_rub = float(p.amount.value) if p.amount else 0
    amount_kop = int(round(amount_rub * 100))

    user_id = yk_meta.get("user_id")
    if not user_id:
        return {"status": "no_user_id"}
    db_user = db.query(User).filter_by(id=int(user_id)).first()
    if not db_user:
        return {"status": "user_not_found"}

    # Защита от двойного зачисления
    existing_tx = db.query(Transaction).filter_by(yookassa_payment_id=payment_id).first()
    if existing_tx:
        return {"status": "already_credited"}

    pkg_id = yk_meta.get("package_id")
    pkg_name = yk_meta.get("pkg_name", "")
    if pkg_id:
        pkg = db.query(TokenPackage).filter_by(id=int(pkg_id)).first()
        if pkg:
            pkg_name = pkg.name
            # Зачисляем сумму в копейках по price_rub пакета (с возможным бонусом
            # в pkg.tokens — теперь это копейки, см. миграцию × 10)
            amount_kop = int(pkg.tokens) if pkg.tokens else amount_kop

    credit_atomic(db, db_user.id, amount_kop)
    credit_referral_bonus(db, db_user, amount_kop, pkg_name or f"{amount_rub:.2f} ₽")
    db.add(Transaction(
        user_id=db_user.id, type="payment", amount_rub=amount_rub,
        tokens_delta=amount_kop,
        description=f"Пополнение баланса: {pkg_name or f'{amount_rub:.2f} ₽'} (webhook)",
        yookassa_payment_id=payment_id,
    ))
    db.commit()
    log.info(f"Webhook: credited {amount_kop} kop ({amount_rub} ₽) for user {user_id}")
    return {"status": "ok"}


@router.post("/payment/buy-tokens")
def buy_tokens(req: BuyTokenRequest, user: User = Depends(current_user),
               db: Session = Depends(get_db)):
    """Создать платёж на пополнение баланса по выбранному пакету."""
    if not user.is_verified:
        raise HTTPException(403, "Подтвердите email для оплаты")
    pkg = db.query(TokenPackage).filter_by(id=req.package_id, is_active=True).first()
    if not pkg:
        raise HTTPException(404, "Пакет не найден")
    try:
        from server.payments import _init_yookassa
        _init_yookassa()
        from yookassa import Payment as YKP
        payment_data = {
            "amount": {"value": str(float(pkg.price_rub)), "currency": "RUB"},
            "confirmation": {"type": "redirect", "return_url": req.return_url},
            "capture": True,
            "description": f"AI Студия Че — пополнение «{pkg.name}» (user {user.id})",
            "metadata": {
                "user_id": str(user.id),
                "package_id": str(pkg.id),
                "pkg_name": pkg.name,
            },
        }
        if user.email:
            payment_data["receipt"] = {
                "customer": {"email": user.email},
                "items": [{
                    "description": f"Пополнение баланса: {pkg.name}",
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
    """Подтверждает платёж через ЮKassa API и зачисляет баланс."""
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
    meta_user_id = (p.metadata or {}).get("user_id")
    if str(meta_user_id) != str(user.id):
        log.warning(f"User {user.id} tried to confirm payment {payment_id} of user {meta_user_id}")
        raise HTTPException(403, "Этот платёж принадлежит другому пользователю")
    pkg_id = int((p.metadata or {}).get("package_id", 0))
    pkg = db.query(TokenPackage).filter_by(id=pkg_id).first()
    if not pkg:
        raise HTTPException(404, "Пакет не найден")

    db_user = db.query(User).filter_by(id=user.id).first()
    amount_kop = int(pkg.tokens) if pkg.tokens else int(round(float(pkg.price_rub) * 100))
    credit_atomic(db, user.id, amount_kop)
    credit_referral_bonus(db, db_user, amount_kop, pkg.name)
    db.add(Transaction(
        user_id=user.id, type="payment",
        amount_rub=pkg.price_rub, tokens_delta=amount_kop,
        description=f"Пополнение баланса: {pkg.name}",
        yookassa_payment_id=payment_id,
    ))
    db.commit()
    return {"status": "credited", "kopecks_added": amount_kop,
            "rub_added": amount_kop / 100}
