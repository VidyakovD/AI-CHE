"""YooKassa payment integration."""
import os, uuid
from dotenv import load_dotenv
load_dotenv()
from yookassa import Configuration, Payment


def _init_yookassa():
    """Инициализирует YooKassa credentials из env (динамически, не при импорте)."""
    Configuration.account_id = os.getenv("YOOKASSA_SHOP_ID", "")
    Configuration.secret_key = os.getenv("YOOKASSA_SECRET_KEY", "")


_init_yookassa()

def credit_referral_bonus(db, db_user, kopecks_paid, description):
    """Начисляет рефереру 10% от суммы пополнения (в копейках, атомарно)."""
    referred_by = getattr(db_user, 'referred_by', None)
    if not referred_by:
        return
    from server.models import User, Transaction
    from server.billing import credit_atomic
    referrer = db.query(User).filter_by(referral_code=referred_by).first()
    if not referrer:
        return
    bonus = max(1, round(kopecks_paid * 0.10))
    credit_atomic(db, referrer.id, bonus)
    db.add(Transaction(user_id=referrer.id, type="bonus", tokens_delta=bonus,
                       description=f"Реферальный бонус за пополнение {description}"))


def check_payment(payment_id: str) -> str:
    p = Payment.find_one(payment_id)
    return p.status
