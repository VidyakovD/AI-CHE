import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from server.routes.deps import get_db, current_user, _user_dict, _sub_dict, _tx_dict
from server.models import User, Subscription, Transaction, Message, SupportRequest

log = logging.getLogger(__name__)

router = APIRouter(prefix="/user", tags=["user"])


@router.get("/cabinet/stats")
def cabinet_stats(user=Depends(current_user), db: Session = Depends(get_db)):
    db_user = db.query(User).filter_by(id=user.id).first()
    sub = db.query(Subscription).filter_by(user_id=user.id, status="active")\
            .order_by(Subscription.id.desc()).first()
    txs = db.query(Transaction).filter_by(user_id=user.id)\
            .order_by(Transaction.created_at.desc()).limit(50).all()
    usage = db.query(Message.model, Message.tokens_used).filter_by(user_id=user.id, role="user").all()
    model_usage = {}
    for m, t in usage:
        model_usage[m] = model_usage.get(m, 0) + (t or 0)
    reqs = db.query(SupportRequest).filter_by(user_id=user.id)\
             .order_by(SupportRequest.created_at.desc()).all()
    u = _user_dict(db_user)
    u["support_requests"] = [
        {"id": r.id, "type": r.type, "description": r.description,
         "status": r.status, "admin_response": r.admin_response,
         "created_at": r.created_at.isoformat(), "updated_at": r.updated_at.isoformat() if r.updated_at else None}
        for r in reqs]
    return {"user": u,
            "subscription": _sub_dict(sub) if sub else None,
            "transactions": [_tx_dict(t) for t in txs],
            "model_usage": model_usage}


@router.post("/subscription/cancel")
def cancel_subscription(user=Depends(current_user), db: Session = Depends(get_db)):
    sub = db.query(Subscription).filter_by(user_id=user.id, status="active")\
            .order_by(Subscription.id.desc()).first()
    if not sub:
        raise HTTPException(404, "Активная подписка не найдена")
    sub.status = "cancelled"
    db.add(sub)
    db.commit()
    return {"status": "cancelled", "subscription": _sub_dict(sub)}


class SupportRequestRequest(BaseModel):
    type: str
    description: str


@router.post("/support/refund")
def create_refund_request(body: SupportRequestRequest, user=Depends(current_user), db: Session = Depends(get_db)):
    req = SupportRequest(user_id=user.id, type="refund", description=body.description)
    db.add(req); db.commit(); db.refresh(req)
    return {"id": req.id, "status": "open", "message": "Заявка принята. Срок рассмотрения — 10 рабочих дней."}


@router.post("/support/delete-data")
def create_delete_data_request(body: SupportRequestRequest, user=Depends(current_user), db: Session = Depends(get_db)):
    req = SupportRequest(user_id=user.id, type="delete_data", description=body.description)
    db.add(req); db.commit(); db.refresh(req)
    return {"id": req.id, "status": "open", "message": "Запрос принят. Данные будут удалены в течение 30 дней."}


@router.get("/support/requests")
def list_support_requests(user=Depends(current_user), db: Session = Depends(get_db)):
    return [{"id": r.id, "type": r.type, "description": r.description,
             "status": r.status, "admin_response": r.admin_response,
             "created_at": r.created_at.isoformat(), "updated_at": r.updated_at.isoformat() if r.updated_at else None}
            for r in db.query(SupportRequest).filter_by(user_id=user.id).order_by(SupportRequest.created_at.desc()).all()]
