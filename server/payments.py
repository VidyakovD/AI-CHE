"""YooKassa payment integration."""
import os, uuid
from yookassa import Configuration, Payment

Configuration.account_id = os.getenv("YOOKASSA_SHOP_ID", "")
Configuration.secret_key  = os.getenv("YOOKASSA_SECRET_KEY", "")

# ── Правильные цены ───────────────────────────────────────────────────────────
PLANS = {
    "starter": {"id":"starter", "name":"Старт",  "price_rub": 590,  "tokens": 1_000},
    "pro":     {"id":"pro",     "name":"Про",     "price_rub": 1590, "tokens": 3_000},
    "ultra":   {"id":"ultra",   "name":"Ультра",  "price_rub": 4590, "tokens": 9_000},
}

TOKEN_PACKAGES = {
    1: {"name":"1 000 CH",  "tokens":1_000, "price_rub":600},
    2: {"name":"2 000 CH",  "tokens":2_000, "price_rub":1150},
    3: {"name":"5 000 CH",  "tokens":5_000, "price_rub":2700},
}

def get_plan(plan_id: str) -> dict:
    return PLANS.get(plan_id, PLANS["starter"])


def create_payment(plan: str, user_id: int, return_url: str,
                   user_email: str = None, promo_code: str = None,
                   discount_pct: int = 0) -> dict:
    plan_cfg = PLANS.get(plan)
    if not plan_cfg:
        raise ValueError(f"Unknown plan: {plan}")

    price = plan_cfg["price_rub"]
    if promo_code and discount_pct > 0:
        price = round(price * (100 - discount_pct) / 100)

    payment_data = {
        "amount":       {"value": str(float(price)), "currency": "RUB"},
        "confirmation": {"type": "redirect", "return_url": return_url},
        "capture":      True,
        "description":  f"AI Студия Че — {plan_cfg['name']} (user {user_id})",
        "metadata":     {"user_id": user_id, "plan": plan,
                         "promo": promo_code, "original_price": plan_cfg["price_rub"]},
    }
    # Электронный чек (54-ФЗ)
    if user_email:
        payment_data["receipt"] = {
            "customer_email": user_email,
            "items": [{
                "description": f"Подписка {plan_cfg['name']}" + (" (–10% промокод)" if promo_code else ""),
                "quantity": "1",
                "amount": {"value": str(float(price)), "currency": "RUB"},
                "vat_code": "1",  # Без НДС
            }],
        }
    p = Payment.create(payment_data, str(uuid.uuid4()))
    return {
        "payment_id":       p.id,
        "confirmation_url": p.confirmation.confirmation_url,
        "status":           p.status,
        "receipt_sent":     bool(user_email),
    }


def credit_referral_bonus(db, db_user, tokens, description):
    """Начисляет рефереру 10% от токенов тарифа при каждой оплате."""
    referred_by = getattr(db_user, 'referred_by', None)
    if not referred_by:
        return
    from server.models import User, Transaction
    referrer = db.query(User).filter_by(referral_code=referred_by).first()
    if not referrer:
        return
    bonus = max(1, round(tokens * 0.10))
    referrer.tokens_balance += bonus
    db.add(Transaction(user_id=referrer.id, type="bonus", tokens_delta=bonus,
                       description=f"Реферальный бонус за оплату {description}"))


def check_payment(payment_id: str) -> str:
    p = Payment.find_one(payment_id)
    return p.status
