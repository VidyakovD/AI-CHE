"""YooKassa payment integration."""
import os, uuid
from yookassa import Configuration, Payment

Configuration.account_id = os.getenv("YOOKASSA_SHOP_ID", "")
Configuration.secret_key  = os.getenv("YOOKASSA_SECRET_KEY", "")

PLANS = {
    "starter": {"name": "Стартер",  "price_rub": 299,  "tokens": 100_000},
    "pro":     {"name": "Про",      "price_rub": 799,  "tokens": 500_000},
    "ultra":   {"name": "Ультра",   "price_rub": 1499, "tokens": 1_500_000},
}


def create_payment(plan: str, user_id: int, return_url: str) -> dict:
    """Create YooKassa payment, return {payment_id, confirmation_url}."""
    plan_cfg = PLANS.get(plan)
    if not plan_cfg:
        raise ValueError(f"Unknown plan: {plan}")

    idempotency_key = str(uuid.uuid4())
    payment = Payment.create({
        "amount":       {"value": str(float(plan_cfg["price_rub"])), "currency": "RUB"},
        "confirmation": {"type": "redirect", "return_url": return_url},
        "capture":      True,
        "description":  f"Obsidian AI — подписка «{plan_cfg['name']}» (user {user_id})",
        "metadata":     {"user_id": user_id, "plan": plan},
    }, idempotency_key)

    return {
        "payment_id":       payment.id,
        "confirmation_url": payment.confirmation.confirmation_url,
        "status":           payment.status,
    }


def check_payment(payment_id: str) -> str:
    """Return payment status string."""
    p = Payment.find_one(payment_id)
    return p.status   # pending / waiting_for_capture / succeeded / canceled


def get_plan(plan: str) -> dict:
    return PLANS.get(plan, {})
