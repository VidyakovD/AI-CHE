"""Shared dependencies and helpers used across all routers."""
from datetime import datetime, timedelta
from fastapi import Header, Depends, HTTPException
from sqlalchemy.orm import Session

from server.db import SessionLocal
from server.models import User, Message, Subscription, Transaction, VerifyToken
from server.auth import decode_token


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def current_user(authorization: str = Header(None), db: Session = Depends(get_db)) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Not authenticated")
    payload = decode_token(authorization[7:])
    if not payload:
        raise HTTPException(401, "Invalid or expired token")
    user = db.query(User).filter_by(id=int(payload["sub"])).first()
    if not user:
        raise HTTPException(401, "User not found")
    if getattr(user, 'is_banned', False):
        raise HTTPException(403, "Аккаунт заблокирован. Обратитесь в поддержку.")
    return user


def optional_user(authorization: str = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        return None
    payload = decode_token(authorization[7:])
    if not payload:
        return None
    user = db.query(User).filter_by(id=int(payload["sub"])).first()
    if user and getattr(user, 'is_banned', False):
        return None
    return user


def _user_dict(u):
    return {"id": u.id, "email": u.email, "name": u.name,
            "avatar_url": u.avatar_url, "tokens_balance": u.tokens_balance,
            "is_verified": u.is_verified, "is_banned": getattr(u, 'is_banned', False),
            "referral_code": u.referral_code,
            "created_at": u.created_at.isoformat() if u.created_at else None}


def _sub_dict(s):
    return {"id": s.id, "plan": s.plan, "tokens_total": s.tokens_total,
            "tokens_used": s.tokens_used, "tokens_left": s.tokens_total - s.tokens_used,
            "price_rub": s.price_rub, "status": s.status,
            "started_at": s.started_at.isoformat() if s.started_at else None,
            "expires_at": s.expires_at.isoformat() if s.expires_at else None}


def _tx_dict(t):
    return {"id": t.id, "type": t.type, "amount_rub": t.amount_rub,
            "tokens_delta": t.tokens_delta, "description": t.description,
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
    vt = db.query(VerifyToken).filter_by(
        user_id=user_id, token=code, purpose=purpose, used=False).first()
    if not vt or vt.expires_at < datetime.utcnow():
        return False
    vt.used = True
    db.commit()
    return True


def _deduct(db, user, cost, description, model=None):
    """Списать токены и записать транзакцию."""
    db_user = db.query(User).filter_by(id=user.id).first()
    if db_user.tokens_balance < cost:
        raise HTTPException(402, "Недостаточно токенов. Пополните баланс в личном кабинете.")
    db_user.tokens_balance -= cost
    db.add(Transaction(user_id=user.id, type="usage", tokens_delta=-cost,
                       description=description, model=model))
