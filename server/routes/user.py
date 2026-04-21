import csv, io, logging
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from sqlalchemy import func
from datetime import datetime, timedelta

from server.routes.deps import get_db, current_user, _user_dict, _sub_dict, _tx_dict
from server.models import User, Subscription, Transaction, Message, SupportRequest, UsageLog, ImapCredential

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
    # Детальная статистика по токенам (из UsageLog) за 30 дней
    since = datetime.utcnow() - timedelta(days=30)
    usage_rows = db.query(
        UsageLog.model,
        func.count(UsageLog.id).label("requests"),
        func.sum(UsageLog.input_tokens).label("in_tok"),
        func.sum(UsageLog.output_tokens).label("out_tok"),
        func.sum(UsageLog.ch_charged).label("ch"),
    ).filter(UsageLog.user_id == user.id, UsageLog.created_at >= since)\
     .group_by(UsageLog.model).all()
    token_usage = [
        {
            "model": r.model,
            "requests": r.requests or 0,
            "input_tokens": r.in_tok or 0,
            "output_tokens": r.out_tok or 0,
            "ch_charged": r.ch or 0,
            "avg_ch": round((r.ch or 0) / (r.requests or 1), 1),
        } for r in usage_rows
    ]

    # Разбивка расходов по модулям (из транзакций usage за 30 дней)
    spend = _spend_by_module(db, user.id, since)

    # Топ-5 самых дорогих транзакций usage за 30 дней
    top_spend = db.query(Transaction).filter(
        Transaction.user_id == user.id,
        Transaction.type == "usage",
        Transaction.created_at >= since,
    ).order_by(Transaction.tokens_delta.asc()).limit(5).all()

    return {"user": u,
            "subscription": _sub_dict(sub) if sub else None,
            "transactions": [_tx_dict(t) for t in txs],
            "model_usage": model_usage,
            "token_usage": token_usage,
            "spend_by_module": spend,
            "top_expensive": [_tx_dict(t) for t in top_spend]}


MODULE_LABELS = {
    "chat": "💬 Чат",
    "chatbots": "🤖 Чат-боты",
    "sites": "🌐 Сайты",
    "presentations": "📄 Презентации/КП",
    "agents": "🧠 AI-агенты",
    "solutions": "✨ Готовые решения",
    "media": "🎨 Картинки/видео",
}


def _classify_tx(desc: str, model: str | None) -> str:
    """Относит транзакцию к модулю по тексту описания или модели."""
    d = (desc or "").lower()
    if "бот «" in d or d.startswith("бот "):
        return "chatbots"
    if "сайт" in d or "код сайт" in d:
        return "sites"
    if "презентац" in d or "кп" in d.split():
        return "presentations"
    if "агент" in d or "ии агент" in d:
        return "agents"
    if "решение:" in d or "готовое решение" in d or "промпт" in d:
        return "solutions"
    if model in ("nano", "kling", "kling-pro", "veo"):
        return "media"
    return "chat"


def _spend_by_module(db, user_id: int, since):
    """Возвращает {module_key: {label, ch, requests, share_pct}} за период."""
    rows = db.query(Transaction).filter(
        Transaction.user_id == user_id,
        Transaction.type == "usage",
        Transaction.created_at >= since,
    ).all()
    buckets: dict[str, dict] = {}
    total = 0
    for t in rows:
        ch = -int(t.tokens_delta or 0)  # usage хранит отрицательные числа
        if ch <= 0:
            continue
        mod = _classify_tx(t.description, t.model)
        b = buckets.setdefault(mod, {"module": mod, "label": MODULE_LABELS.get(mod, mod), "ch": 0, "requests": 0})
        b["ch"] += ch
        b["requests"] += 1
        total += ch
    # sort desc by ch, посчитать доли
    out = sorted(buckets.values(), key=lambda b: b["ch"], reverse=True)
    for b in out:
        b["share_pct"] = round(100 * b["ch"] / total, 1) if total else 0
    return {"total_ch": total, "period_days": 30, "items": out}


@router.get("/referral/stats")
def referral_stats(user=Depends(current_user), db: Session = Depends(get_db)):
    """Статистика рефералов: кого позвал + сколько заработал."""
    db_user = db.query(User).filter_by(id=user.id).first()
    # Все кто зарегался по моему коду
    invited = db.query(User).filter_by(referred_by=db_user.referral_code).all()
    # Мои bonus-транзакции (за рефералов)
    bonus_txs = db.query(Transaction).filter(
        Transaction.user_id == user.id,
        Transaction.type == "bonus",
        Transaction.description.like("%еферал%"),
    ).order_by(Transaction.created_at.desc()).all()
    total_earned = sum(t.tokens_delta or 0 for t in bonus_txs)
    paying = sum(1 for u in invited if any(
        t.type == "payment" for t in u.transactions
    ))
    return {
        "code": db_user.referral_code,
        "invited_count": len(invited),
        "invited_verified": sum(1 for u in invited if u.is_verified),
        "invited_paying": paying,
        "total_earned_ch": total_earned,
        "recent_bonuses": [{
            "tokens": t.tokens_delta,
            "description": t.description,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        } for t in bonus_txs[:10]],
    }


class LowBalanceThresholdBody(BaseModel):
    threshold: int  # 0 — отключено


@router.post("/low-balance-threshold")
def set_low_balance_threshold(body: LowBalanceThresholdBody,
                               user=Depends(current_user), db: Session = Depends(get_db)):
    """Юзер задаёт порог уведомления о низком балансе (CH). 0 — отключает."""
    if body.threshold < 0 or body.threshold > 100_000:
        raise HTTPException(400, "Порог от 0 до 100 000 CH")
    u = db.query(User).filter_by(id=user.id).first()
    u.low_balance_threshold = body.threshold
    # При изменении — сбросим alerted_at, чтобы юзер получил уведомление снова если уже ниже порога
    u.low_balance_alerted_at = None
    db.commit()
    return {"threshold": u.low_balance_threshold}


@router.get("/transactions.csv")
def export_transactions_csv(user=Depends(current_user), db: Session = Depends(get_db)):
    """Экспорт всех транзакций юзера в CSV (для бухгалтерии)."""
    rows = db.query(Transaction).filter_by(user_id=user.id).order_by(Transaction.created_at.desc()).all()
    buf = io.StringIO()
    buf.write("\ufeff")  # BOM для корректной кириллицы в Excel
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Дата", "Тип", "CH (дельта)", "Рубли", "Модель", "Описание", "YooKassa ID"])
    type_ru = {"payment":"Платёж", "usage":"Списание", "bonus":"Бонус", "refund":"Возврат"}
    for t in rows:
        w.writerow([
            t.created_at.strftime("%Y-%m-%d %H:%M:%S") if t.created_at else "",
            type_ru.get(t.type, t.type or ""),
            t.tokens_delta or 0,
            f"{t.amount_rub:.2f}" if t.amount_rub else "",
            t.model or "",
            t.description or "",
            t.yookassa_payment_id or "",
        ])
    buf.seek(0)
    filename = f"aiche-transactions-{user.id}-{datetime.utcnow().strftime('%Y%m%d')}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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


# ── IMAP credentials ──────────────────────────────────────────────────────────

class ImapCredCreate(BaseModel):
    label: str = "Main"
    host: str
    port: int = 993
    username: str
    password: str
    use_ssl: bool = True


@router.get("/imap")
def list_imap(user=Depends(current_user), db: Session = Depends(get_db)):
    from server.secrets_crypto import decrypt
    rows = db.query(ImapCredential).filter_by(user_id=user.id).all()
    out = []
    for r in rows:
        pw_plain = decrypt(r.password)
        out.append({"id": r.id, "label": r.label, "host": r.host, "port": r.port,
                    "username": r.username, "use_ssl": r.use_ssl,
                    "password_preview": "***" + pw_plain[-2:] if pw_plain else "",
                    "last_uid": r.last_uid or 0})
    return out


@router.post("/imap")
def create_imap(body: ImapCredCreate, user=Depends(current_user), db: Session = Depends(get_db)):
    from server.secrets_crypto import encrypt
    cred = ImapCredential(
        user_id=user.id, label=body.label,
        host=body.host, port=body.port,
        username=body.username, password=encrypt(body.password), use_ssl=body.use_ssl,
    )
    db.add(cred); db.commit(); db.refresh(cred)
    return {"id": cred.id, "status": "created"}


@router.delete("/imap/{cred_id}")
def delete_imap(cred_id: int, user=Depends(current_user), db: Session = Depends(get_db)):
    cred = db.query(ImapCredential).filter_by(id=cred_id, user_id=user.id).first()
    if not cred:
        raise HTTPException(404)
    db.delete(cred); db.commit()
    return {"status": "deleted"}
