"""
Атомарные операции с балансом CH.

Все списания/начисления должны идти через эти функции, иначе при параллельных
запросах возможен lost update (двойное списание, отрицательный баланс).

Реализация — `UPDATE ... WHERE` без read-then-write на уровне Python.
"""
import logging
from datetime import datetime, timedelta
from sqlalchemy import update as sa_update, select
from sqlalchemy.orm import Session

from server.models import User

log = logging.getLogger(__name__)
_LOW_BALANCE_COOLDOWN = timedelta(hours=24)


def _maybe_send_low_balance_alert(db: Session, user_id: int):
    """Если баланс упал ниже порога и не слали 24ч — отправить email."""
    try:
        u = db.query(User).filter_by(id=user_id).first()
        if not u or not u.email:
            return
        threshold = int(getattr(u, "low_balance_threshold", 0) or 0)
        if threshold <= 0:
            return  # юзер отключил уведомления
        balance = int(u.tokens_balance or 0)
        if balance > threshold or balance <= 0:
            return  # или ещё хватает, или уже 0 (бессмысленно слать)
        # Анти-спам: максимум раз в 24 часа
        if u.low_balance_alerted_at and (datetime.utcnow() - u.low_balance_alerted_at) < _LOW_BALANCE_COOLDOWN:
            return
        from server.email_service import send_low_balance_alert
        send_low_balance_alert(u.email, u.name or "", balance, threshold)
        u.low_balance_alerted_at = datetime.utcnow()
        db.commit()
    except Exception as e:
        log.warning(f"low_balance_alert failed for user {user_id}: {e}")


def deduct_atomic(db: Session, user_id: int, cost: int) -> int:
    """
    Атомарно списывает min(balance, cost). Возвращает фактически списанное.
    Не уходит в минус. Caller должен сам сделать db.commit().
    Триггерит уведомление о низком балансе при падении ниже порога.
    """
    if cost <= 0 or not user_id:
        return 0

    charged = 0
    res = db.execute(
        sa_update(User)
        .where(User.id == user_id, User.tokens_balance >= cost)
        .values(tokens_balance=User.tokens_balance - cost)
    )
    if (res.rowcount or 0) > 0:
        charged = cost
    else:
        # Баланса не хватило — спишем остаток. Оптимистичная блокировка через WHERE balance==prev.
        for _ in range(5):
            cur = db.execute(
                select(User.tokens_balance).where(User.id == user_id)
            ).scalar() or 0
            cur = int(cur)
            if cur <= 0:
                return 0
            res = db.execute(
                sa_update(User)
                .where(User.id == user_id, User.tokens_balance == cur)
                .values(tokens_balance=0)
            )
            if (res.rowcount or 0) > 0:
                charged = cur
                break

    if charged > 0:
        _maybe_send_low_balance_alert(db, user_id)
    return charged


def deduct_strict(db: Session, user_id: int, cost: int) -> bool:
    """
    Атомарно списывает cost. True — если списал полностью, False — если не хватило баланса.
    Полезно для предоплат («всё или ничего»). Caller должен сам сделать db.commit().
    """
    if cost <= 0:
        return True
    if not user_id:
        return False
    res = db.execute(
        sa_update(User)
        .where(User.id == user_id, User.tokens_balance >= cost)
        .values(tokens_balance=User.tokens_balance - cost)
    )
    ok = (res.rowcount or 0) > 0
    if ok:
        _maybe_send_low_balance_alert(db, user_id)
    return ok


def credit_atomic(db: Session, user_id: int, amount: int) -> bool:
    """
    Атомарно начисляет amount к балансу. Caller должен сам сделать db.commit().
    """
    if amount <= 0 or not user_id:
        return False
    res = db.execute(
        sa_update(User)
        .where(User.id == user_id)
        .values(tokens_balance=User.tokens_balance + amount)
    )
    return (res.rowcount or 0) > 0


def get_balance(db: Session, user_id: int) -> int:
    """Текущий баланс. Информационно — для предпроверок."""
    val = db.execute(
        select(User.tokens_balance).where(User.id == user_id)
    ).scalar()
    return int(val or 0)


def claim_welcome_bonus(db: Session, user_id: int, amount: int) -> bool:
    """
    Атомарно ставит welcome_bonus_claimed_at и зачисляет бонус.
    Возвращает True если бонус действительно зачислен (первый раз),
    False если уже был получен ранее (race / повтор).

    Защита от двойного клейма даже при гонке двух /verify-email.
    Caller должен сделать db.commit().
    """
    if amount <= 0 or not user_id:
        return False
    res = db.execute(
        sa_update(User)
        .where(User.id == user_id, User.welcome_bonus_claimed_at.is_(None))
        .values(welcome_bonus_claimed_at=datetime.utcnow())
    )
    if (res.rowcount or 0) == 0:
        return False  # уже был зачислен
    return credit_atomic(db, user_id, amount)


def claim_referral_signup_bonus(db: Session, referred_user_id: int,
                                 referrer_id: int, amount: int) -> bool:
    """
    Атомарно отмечает что за регистрацию referred_user рефереру уже
    выплачен bounty, и начисляет amount рефереру.
    Защита от гонки двух concurrent /register с одним email.
    Caller должен сделать db.commit().
    """
    if amount <= 0 or not referred_user_id or not referrer_id:
        return False
    res = db.execute(
        sa_update(User)
        .where(User.id == referred_user_id,
               User.referral_signup_bonus_paid_at.is_(None))
        .values(referral_signup_bonus_paid_at=datetime.utcnow())
    )
    if (res.rowcount or 0) == 0:
        return False
    return credit_atomic(db, referrer_id, amount)
