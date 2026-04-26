"""Shared dependencies and helpers used across all routers."""
from datetime import datetime, timedelta
from fastapi import Header, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from server.db import SessionLocal
from server.models import User, Message, Transaction, VerifyToken
from server.auth import decode_token, extract_token


def kop_to_rub(kop) -> float:
    """Кастует копейки в рубли (для API). Принимает int/float/None."""
    if kop is None:
        return 0.0
    return round(int(kop) / 100, 2)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """
    Авторизация: токен из cookie `access_token` (новый путь, после миграции
    на httpOnly cookies) или из заголовка `Authorization: Bearer ...`
    (legacy / mobile-clients).
    """
    token = extract_token(request)
    if not token:
        raise HTTPException(401, "Not authenticated")
    payload = decode_token(token)
    if not payload:
        raise HTTPException(401, "Invalid or expired token")
    user = db.query(User).filter_by(id=int(payload["sub"])).first()
    if not user:
        raise HTTPException(401, "User not found")
    if getattr(user, 'is_banned', False):
        raise HTTPException(403, "Аккаунт заблокирован. Обратитесь в поддержку.")
    return user


def optional_user(request: Request, db: Session = Depends(get_db)):
    token = extract_token(request)
    if not token:
        return None
    payload = decode_token(token)
    if not payload:
        return None
    user = db.query(User).filter_by(id=int(payload["sub"])).first()
    if user and getattr(user, 'is_banned', False):
        return None
    return user


def _user_dict(u):
    return {"id": u.id, "email": u.email, "name": u.name,
            "avatar_url": u.avatar_url,
            "balance_kopecks": int(u.tokens_balance or 0),
            "balance_rub": kop_to_rub(u.tokens_balance),
            "is_verified": u.is_verified, "is_banned": getattr(u, 'is_banned', False),
            "referral_code": u.referral_code,
            "low_balance_threshold_kop": int(getattr(u, "low_balance_threshold", 0) or 0),
            "low_balance_threshold_rub": kop_to_rub(getattr(u, "low_balance_threshold", 0)),
            "created_at": u.created_at.isoformat() if u.created_at else None}


def _tx_dict(t):
    delta = int(t.tokens_delta or 0)
    return {"id": t.id, "type": t.type, "amount_rub": t.amount_rub,
            "delta_kopecks": delta,
            "delta_rub": kop_to_rub(delta),
            "description": t.description,
            "model": t.model,
            "created_at": t.created_at.isoformat() if t.created_at else None}


def _make_verify_token(db, user_id, purpose, generate_code, VERIFY_TTL_MINUTES):
    db.query(VerifyToken).filter_by(user_id=user_id, purpose=purpose, used=False).update({"used": True})
    code = generate_code(6)
    db.add(VerifyToken(user_id=user_id, token=code, purpose=purpose,
                       expires_at=datetime.utcnow() + timedelta(minutes=VERIFY_TTL_MINUTES)))
    db.commit()
    return code


def _use_verify_token(db, user_id, code, purpose):
    """
    Атомарно помечает токен использованным. Гарантирует ровно одного
    «победителя» при гонке: используем UPDATE ... WHERE used=False
    и проверяем rowcount — выиграл ровно тот вызов где БД отдала 1 строку.

    Без этого два параллельных POST /verify-email с одним кодом могли пройти
    SELECT-then-UPDATE одновременно, оба увидели used=False — и оба
    выполнили действие (например welcome-bonus тоже мог дублироваться,
    хотя сам бонус защищён отдельным atomic gate).
    """
    now = datetime.utcnow()
    rowcount = db.query(VerifyToken).filter_by(
        user_id=user_id, token=code, purpose=purpose, used=False,
    ).filter(VerifyToken.expires_at > now).update(
        {"used": True}, synchronize_session=False,
    )
    db.commit()
    return rowcount == 1


def _deduct(db, user, cost_kop, description, model=None):
    """Списать копейки с баланса и записать транзакцию (атомарно — защита от lost update)."""
    from server.billing import deduct_strict
    if not deduct_strict(db, user.id, cost_kop):
        raise HTTPException(402, "Недостаточно средств. Пополните баланс в личном кабинете.")
    db.add(Transaction(user_id=user.id, type="usage", tokens_delta=-cost_kop,
                       description=description, model=model))
    db.commit()
