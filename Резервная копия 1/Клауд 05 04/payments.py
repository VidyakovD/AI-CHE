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


def create_payment(plan: str, user_id: int, return_url: str) -> dict:
    plan_cfg = PLANS.get(plan)
    if not plan_cfg:
        raise ValueError(f"Unknown plan: {plan}")
    p = Payment.create({
        "amount":       {"value": str(float(plan_cfg["price_rub"])), "currency": "RUB"},
        "confirmation": {"type": "redirect", "return_url": return_url},
        "capture":      True,
        "description":  f"AI Студия Че — {plan_cfg['name']} (user {user_id})",
        "metadata":     {"user_id": user_id, "plan": plan},
    }, str(uuid.uuid4()))
    return {
        "payment_id":       p.id,
        "confirmation_url": p.confirmation.confirmation_url,
        "status":           p.status,
    }


def check_payment(payment_id: str) -> str:
    p = Payment.find_one(payment_id)
    return p.status
