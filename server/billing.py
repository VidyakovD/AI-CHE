"""
Атомарные операции с балансом CH.

Все списания/начисления должны идти через эти функции, иначе при параллельных
запросах возможен lost update (двойное списание, отрицательный баланс).

Реализация — `UPDATE ... WHERE` без read-then-write на уровне Python.
"""
from sqlalchemy import update as sa_update, select
from sqlalchemy.orm import Session

from server.models import User


def deduct_atomic(db: Session, user_id: int, cost: int) -> int:
    """
    Атомарно списывает min(balance, cost). Возвращает фактически списанное.
    Не уходит в минус. Caller должен сам сделать db.commit().
    """
    if cost <= 0 or not user_id:
        return 0

    res = db.execute(
        sa_update(User)
        .where(User.id == user_id, User.tokens_balance >= cost)
        .values(tokens_balance=User.tokens_balance - cost)
    )
    if (res.rowcount or 0) > 0:
        return cost

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
            return cur
    return 0


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
    return (res.rowcount or 0) > 0


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
