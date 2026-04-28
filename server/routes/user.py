import os, csv, io, logging
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from sqlalchemy import func
from datetime import datetime, timedelta

from server.routes.deps import get_db, current_user, _user_dict, _tx_dict, kop_to_rub
from server.models import User, Transaction, Message, SupportRequest, UsageLog, ImapCredential

log = logging.getLogger(__name__)

router = APIRouter(prefix="/user", tags=["user"])


@router.get("/cabinet/stats")
def cabinet_stats(user=Depends(current_user), db: Session = Depends(get_db)):
    db_user = db.query(User).filter_by(id=user.id).first()
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
            "spent_kopecks": int(r.ch or 0),
            "spent_rub": kop_to_rub(r.ch or 0),
            "avg_kop": round((r.ch or 0) / (r.requests or 1), 1),
            "avg_rub": kop_to_rub(round((r.ch or 0) / (r.requests or 1), 1)),
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
        kop = -int(t.tokens_delta or 0)  # usage хранит отрицательные числа (теперь копейки)
        if kop <= 0:
            continue
        mod = _classify_tx(t.description, t.model)
        b = buckets.setdefault(mod, {"module": mod, "label": MODULE_LABELS.get(mod, mod), "kopecks": 0, "requests": 0})
        b["kopecks"] += kop
        b["requests"] += 1
        total += kop
    # sort desc, посчитать доли
    out = sorted(buckets.values(), key=lambda b: b["kopecks"], reverse=True)
    for b in out:
        b["share_pct"] = round(100 * b["kopecks"] / total, 1) if total else 0
        b["rub"] = kop_to_rub(b["kopecks"])
    return {"total_kopecks": total, "total_rub": kop_to_rub(total), "period_days": 30, "items": out}


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
        "total_earned_kopecks": total_earned,
        "total_earned_rub": kop_to_rub(total_earned),
        "recent_bonuses": [{
            "kopecks": t.tokens_delta,
            "rub": kop_to_rub(t.tokens_delta),
            "description": t.description,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        } for t in bonus_txs[:10]],
    }


class LowBalanceThresholdBody(BaseModel):
    threshold_rub: float  # порог в рублях, 0 — отключено


@router.post("/low-balance-threshold")
def set_low_balance_threshold(body: LowBalanceThresholdBody,
                               user=Depends(current_user), db: Session = Depends(get_db)):
    """Юзер задаёт порог уведомления о низком балансе (₽). 0 — отключает."""
    threshold_kop = int(round(body.threshold_rub * 100))
    if threshold_kop < 0 or threshold_kop > 10_000_000:  # макс 100 000 ₽
        raise HTTPException(400, "Порог от 0 до 100 000 ₽")
    u = db.query(User).filter_by(id=user.id).first()
    u.low_balance_threshold = threshold_kop
    u.low_balance_alerted_at = None
    db.commit()
    return {"threshold_rub": kop_to_rub(u.low_balance_threshold),
            "threshold_kopecks": int(u.low_balance_threshold or 0)}


def _csv_safe(v):
    """Защита от CSV-injection: если поле начинается с =+-@, префиксим апострофом.
    Excel/LibreOffice иначе воспримут как формулу (может выполнить команду через DDE)."""
    s = str(v) if v is not None else ""
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


@router.get("/transactions.csv")
def export_transactions_csv(user=Depends(current_user), db: Session = Depends(get_db)):
    """Экспорт всех транзакций юзера в CSV (для бухгалтерии)."""
    rows = db.query(Transaction).filter_by(user_id=user.id).order_by(Transaction.created_at.desc()).all()
    buf = io.StringIO()
    buf.write("\ufeff")  # BOM для корректной кириллицы в Excel
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Дата", "Тип", "Дельта (₽)", "Сумма платежа (₽)", "Модель", "Описание", "YooKassa ID"])
    type_ru = {"payment":"Платёж", "usage":"Списание", "bonus":"Бонус", "refund":"Возврат"}
    for t in rows:
        delta_rub = (t.tokens_delta or 0) / 100  # копейки → рубли
        w.writerow([
            t.created_at.strftime("%Y-%m-%d %H:%M:%S") if t.created_at else "",
            type_ru.get(t.type, t.type or ""),
            f"{delta_rub:.2f}",
            f"{t.amount_rub:.2f}" if t.amount_rub else "",
            _csv_safe(t.model),
            _csv_safe(t.description),
            _csv_safe(t.yookassa_payment_id),
        ])
    buf.seek(0)
    filename = f"aiche-transactions-{user.id}-{datetime.utcnow().strftime('%Y%m%d')}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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


class FeatureVoteBody(BaseModel):
    feature: str


@router.post("/feature-vote")
def feature_vote(body: FeatureVoteBody, user: User = Depends(current_user)):
    """
    Голос юзера за будущую фичу/канал. Записывается в audit_log,
    мы потом приоритизируем разработку по количеству голосов.
    Защита от спама — по audit_log с фильтром user_id+target_id+24h.
    """
    feat = (body.feature or "").strip()[:80]
    if not feat:
        raise HTTPException(400, "feature обязательно")
    from server.audit_log import log_action
    log_action(
        "user.feature_vote",
        user_id=user.id,
        target_type="feature",
        target_id=feat,
        details={"feature": feat},
    )
    return {"status": "ok", "feature": feat}


# ── Telegram management bot binding ──────────────────────────────────────


@router.get("/tg-link/status")
def tg_link_status(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Статус привязки TG к юзеру + конфигурация бота на сервере."""
    from server.tg_management import is_configured
    u = db.query(User).filter_by(id=user.id).first()
    return {
        "bot_configured": is_configured(),
        "bot_username": os.getenv("TG_MGMT_BOT_USERNAME", "").strip().lstrip("@") or None,
        "linked": bool(u and u.tg_user_id),
        "tg_username": (u.tg_username if u and u.tg_username else None),
        "notify_proposals": bool(getattr(u, "tg_notify_proposals", True)),
        "notify_records": bool(getattr(u, "tg_notify_records", True)),
        "notify_errors": bool(getattr(u, "tg_notify_errors", True)),
    }


@router.post("/tg-link/code")
def tg_link_code(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Сгенерировать одноразовый код для привязки. Юзер вводит его в боте
    командой /link XXXXXX — после этого аккаунт связан."""
    from server.tg_management import generate_link_code, is_configured
    if not is_configured():
        raise HTTPException(503, "Telegram-бот управления не настроен")
    code = generate_link_code(db, user.id)
    bot_username = os.getenv("TG_MGMT_BOT_USERNAME", "").strip().lstrip("@")
    deep_link = (f"https://t.me/{bot_username}?start=LINK_{code}"
                  if bot_username else None)
    return {"code": code, "deep_link": deep_link, "expires_in_minutes": 10}


@router.post("/tg-link/unlink")
def tg_link_unlink(user: User = Depends(current_user), db: Session = Depends(get_db)):
    from server.tg_management import unlink
    unlink(db, user.id)
    return {"status": "unlinked"}


class TgNotifyToggleBody(BaseModel):
    notify_proposals: bool | None = None
    notify_records: bool | None = None
    notify_errors: bool | None = None


@router.put("/tg-link/notifications")
def tg_link_notifications(body: TgNotifyToggleBody,
                           user: User = Depends(current_user),
                           db: Session = Depends(get_db)):
    """Управление флагами подписки на push'и."""
    u = db.query(User).filter_by(id=user.id).first()
    if not u:
        raise HTTPException(404, "Пользователь не найден")
    if body.notify_proposals is not None:
        u.tg_notify_proposals = bool(body.notify_proposals)
    if body.notify_records is not None:
        u.tg_notify_records = bool(body.notify_records)
    if body.notify_errors is not None:
        u.tg_notify_errors = bool(body.notify_errors)
    db.commit()
    return {"status": "ok"}
