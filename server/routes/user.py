import os, csv, io, logging
from fastapi import APIRouter, Depends, HTTPException, Request
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

    # Активность по модулям с UI (Креаторы / Финансы / Календарь / Заметки).
    # Считаем кол-во записей юзера в каждом — позволяет ему / админу видеть
    # какие модули реально используются (новый юзер увидит 0 и поймёт куда
    # ткнуть, опытный — где у него «много» и есть ли смысл чистить).
    modules_activity: dict[str, dict] = {}
    try:
        from server.models import (
            CreatorBrand, ContentItem, FinanceTransaction,
            LocalCalendarEvent, KnowledgeFile, AgentTask,
        )
        brands_cnt = db.query(CreatorBrand).filter_by(user_id=user.id).count()
        # ContentItem напрямую — кол-во контент-планов любого бренда юзера
        posts_cnt = (db.query(ContentItem)
                       .join(CreatorBrand, ContentItem.calendar_id == CreatorBrand.id, isouter=True)
                       .filter(CreatorBrand.user_id == user.id).count()) if brands_cnt else 0
        fin_cnt = db.query(FinanceTransaction).filter_by(user_id=user.id).count()
        cal_cnt = db.query(LocalCalendarEvent).filter_by(user_id=user.id).count()
        notes_cnt = (db.query(KnowledgeFile)
                       .filter_by(user_id=user.id, owner_type="user", mime="text/x-note")
                       .count())
        kb_files_cnt = (db.query(KnowledgeFile)
                          .filter(KnowledgeFile.user_id == user.id,
                                  KnowledgeFile.mime != "text/x-note")
                          .count())
        tasks_30d = db.query(AgentTask).filter(
            AgentTask.user_id == user.id,
            AgentTask.created_at >= since,
        ).count() if hasattr(AgentTask, "created_at") else 0
        modules_activity = {
            "creators":     {"brands": brands_cnt, "posts": posts_cnt, "label": "📅 Креаторы", "url": "/creators.html"},
            "finance":      {"transactions": fin_cnt, "label": "💰 Финансы", "url": "/finance.html"},
            "calendar":     {"events": cal_cnt, "label": "📅 Календарь", "url": "/calendar.html"},
            "notes":        {"notes": notes_cnt, "kb_files": kb_files_cnt, "label": "📝 Заметки", "url": "/notes.html"},
            "agents_tasks_30d": {"count": tasks_30d, "label": "🧠 Задачи агентов (30д)"},
        }
    except Exception as _e:
        log.warning(f"[cabinet/stats] modules_activity skipped: {_e}")

    return {"user": u,
            "transactions": [_tx_dict(t) for t in txs],
            "model_usage": model_usage,
            "token_usage": token_usage,
            "spend_by_module": spend,
            "top_expensive": [_tx_dict(t) for t in top_spend],
            "modules_activity": modules_activity}


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
    try:
        from server.audit_log import log_action
        log_action("user.tg_link_code_generated", user_id=user.id,
                   target_type="user", target_id=user.id)
    except Exception:
        pass
    return {"code": code, "deep_link": deep_link, "expires_in_minutes": 10}


@router.post("/tg-link/aiche-bot/code")
def aiche_bot_link_code(user: User = Depends(current_user)):
    """Сгенерировать код для привязки tg_user_id к @aiche_bot (общему боту платформы).

    Возвращает {code, deep_link, expires_in_minutes}.
    Юзер открывает deep_link → TG авто-запускает бот с pre-filled /start LINK_<code>.
    Бот вызывает link_codes.redeem_code → линкует tg_user_id к существующему User.

    Отличие от /tg-link/code: тот для legacy mgmt-бота (push-нотификации),
    этот для нового @aiche_bot (интерактивный + AI).
    """
    from server.link_codes import issue_code, DEFAULT_TTL_SEC
    bot_username = (os.getenv("AICHE_TG_BOT_USERNAME") or "").strip().lstrip("@")
    if not bot_username:
        raise HTTPException(503, "@aiche_bot не настроен (AICHE_TG_BOT_USERNAME пустой)")
    code = issue_code(user.id, "tg_user_id")
    deep_link = f"https://t.me/{bot_username}?start=LINK_{code}"
    try:
        from server.audit_log import log_action
        log_action("user.aiche_bot_link_code_generated", user_id=user.id,
                   target_type="user", target_id=user.id)
    except Exception:
        pass
    return {
        "code": code,
        "deep_link": deep_link,
        "expires_in_minutes": DEFAULT_TTL_SEC // 60,
        "bot_username": bot_username,
    }


@router.post("/tg-link/unlink")
def tg_link_unlink(user: User = Depends(current_user), db: Session = Depends(get_db)):
    from server.tg_management import unlink
    # Запомним до unlink — чтобы в audit-логе остался tg_user_id
    u = db.query(User).filter_by(id=user.id).first()
    prev_tg_uid = (u.tg_user_id if u else None) or ""
    unlink(db, user.id)
    try:
        from server.audit_log import log_action
        # Уровень warn — unlink важное событие, после него юзер ПЕРЕСТАНЕТ
        # получать push-alerts о подозрительной активности. Если атакер
        # компрометировал сессию — это первое что он сделает.
        log_action("user.tg_unlink", user_id=user.id,
                   target_type="user", target_id=user.id, level="warn",
                   details={"prev_tg_user_id": prev_tg_uid})
    except Exception:
        pass
    return {"status": "unlinked"}


# ── MAX-link (симметрично TG) ──────────────────────────────────────────────


@router.get("/max-link/status")
def max_link_status(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Статус привязки MAX к юзеру + конфигурация бота на сервере."""
    from server.max_management import is_configured
    u = db.query(User).filter_by(id=user.id).first()
    return {
        "bot_configured": is_configured(),
        "bot_username": os.getenv("MAX_MGMT_BOT_USERNAME", "").strip().lstrip("@") or None,
        "linked": bool(u and u.max_user_id),
        "max_username": (u.max_username if u and u.max_username else None),
    }


@router.post("/max-link/code")
def max_link_code(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Сгенерировать одноразовый код для привязки MAX. Юзер вводит его в боте
    командой /link XXXXXX — после этого аккаунт связан."""
    from server.max_management import generate_link_code, is_configured
    if not is_configured():
        raise HTTPException(503, "MAX-бот управления не настроен")
    code = generate_link_code(db, user.id)
    bot_username = os.getenv("MAX_MGMT_BOT_USERNAME", "").strip().lstrip("@")
    # MAX-эквивалент t.me deep-link — используем max.ru/<username>?start=LINK_<code>
    deep_link = (f"https://max.ru/{bot_username}?start=LINK_{code}"
                  if bot_username else None)
    try:
        from server.audit_log import log_action
        log_action("user.max_link_code_generated", user_id=user.id,
                   target_type="user", target_id=user.id)
    except Exception:
        pass
    return {"code": code, "deep_link": deep_link, "expires_in_minutes": 10}


@router.post("/max-link/unlink")
def max_link_unlink(user: User = Depends(current_user), db: Session = Depends(get_db)):
    from server.max_management import unlink
    u = db.query(User).filter_by(id=user.id).first()
    prev_max_uid = (u.max_user_id if u else None) or ""
    unlink(db, user.id)
    try:
        from server.audit_log import log_action
        log_action("user.max_unlink", user_id=user.id,
                   target_type="user", target_id=user.id, level="warn",
                   details={"prev_max_user_id": prev_max_uid})
    except Exception:
        pass
    return {"status": "unlinked"}


# ── Personal-боты юзеров (модель «каждый юзер свой бот», 2026-05-22) ──────


class PersonalBotConnectBody(BaseModel):
    token: str  # токен от @BotFather (TG) или MAX-эквивалента


@router.get("/personal-bot/tg/status")
def personal_tg_status(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Статус подключения personal TG-бота юзера."""
    u = db.query(User).filter_by(id=user.id).first()
    return {
        "connected": bool(u and u.personal_tg_bot_token),
        "bot_username": (u.personal_tg_bot_username if u else None) or None,
        "webhook_set": bool(u and u.personal_tg_webhook_set),
        "chat_id_known": bool(u and u.personal_tg_chat_id),
    }


@router.post("/personal-bot/tg/connect")
async def personal_tg_connect(payload: PersonalBotConnectBody,
                              user: User = Depends(current_user),
                              db: Session = Depends(get_db)):
    """Подключить свой TG-бот: валидируем через getMe, сохраняем токен, ставим webhook."""
    from server.personal_bot_relay import (
        tg_validate_token, tg_set_webhook, compute_token_hash,
    )
    token = (payload.token or "").strip()
    if not token:
        raise HTTPException(400, "Токен не передан")
    # Валидация через Telegram
    info = await tg_validate_token(token)
    if not info.get("ok"):
        raise HTTPException(400, f"Telegram: {info.get('error', 'unknown')}")
    if not info.get("is_bot"):
        raise HTTPException(400, "Это не bot-токен (getMe вернул is_bot=false)")
    # Проверим что этот hash ещё ни к кому не привязан (anti-collision / anti-steal)
    token_hash = compute_token_hash(token)
    existing = (db.query(User)
                  .filter(User.personal_tg_bot_token_hash == token_hash,
                          User.id != user.id)
                  .first())
    if existing:
        raise HTTPException(409,
            "Этот бот уже подключен к другому аккаунту. Отвяжи там сначала.")
    # Save (token хранится через EncryptedString)
    u = db.query(User).filter_by(id=user.id).first()
    u.personal_tg_bot_token = token
    u.personal_tg_bot_username = (info.get("bot_username") or "").lstrip("@")[:80]
    u.personal_tg_bot_token_hash = token_hash
    u.personal_tg_webhook_set = False
    u.personal_tg_chat_id = None  # сбрасываем — будет заполнен при /start
    db.commit()
    # Set webhook
    wh = await tg_set_webhook(token, token_hash)
    if not wh.get("ok"):
        # Сохранили токен, но webhook не встал — это нормально. Cron
        # failed_webhooks_retry_loop каждые 10 мин повторит попытку.
        # Юзеру говорим что всё под контролем — не нужно ничего делать.
        log.warning(f"[personal-tg] setWebhook failed user={user.id}: {wh.get('error')} (cron retry в течение 10 мин)")
        return {"status": "connected_partial",
                "bot_username": u.personal_tg_bot_username,
                "error": (f"Бот подключен ✓. Telegram временно недоступен "
                          f"({wh.get('error')}) — система автоматически "
                          f"установит webhook в течение 10 минут. Можешь не "
                          f"переподключать. Когда заработает — напиши /start "
                          f"в бот.")}
    u.personal_tg_webhook_set = True
    db.commit()

    try:
        from server.audit_log import log_action
        log_action("user.personal_tg_connect", user_id=user.id,
                   target_type="user", target_id=user.id,
                   details={"bot_username": u.personal_tg_bot_username})
    except Exception:
        pass

    return {"status": "connected",
            "bot_username": u.personal_tg_bot_username,
            "webhook_url": wh.get("webhook_url"),
            "next_step": f"Открой @{u.personal_tg_bot_username} в Telegram и напиши /start"}


@router.post("/personal-bot/tg/disconnect")
async def personal_tg_disconnect(user: User = Depends(current_user),
                                  db: Session = Depends(get_db)):
    """Отключить свой TG-бот: снимаем webhook, очищаем токен."""
    from server.personal_bot_relay import tg_delete_webhook
    u = db.query(User).filter_by(id=user.id).first()
    if not u or not u.personal_tg_bot_token:
        return {"status": "not_connected"}
    token = u.personal_tg_bot_token
    prev_bot = u.personal_tg_bot_username
    try:
        await tg_delete_webhook(token)
    except Exception:
        pass
    u.personal_tg_bot_token = None
    u.personal_tg_bot_username = None
    u.personal_tg_bot_token_hash = None
    u.personal_tg_chat_id = None
    u.personal_tg_webhook_set = False
    db.commit()
    try:
        from server.audit_log import log_action
        log_action("user.personal_tg_disconnect", user_id=user.id,
                   target_type="user", target_id=user.id, level="warn",
                   details={"prev_bot": prev_bot or ""})
    except Exception:
        pass
    return {"status": "disconnected"}


# MAX-аналоги (симметрично)


@router.get("/personal-bot/max/status")
def personal_max_status(user: User = Depends(current_user), db: Session = Depends(get_db)):
    u = db.query(User).filter_by(id=user.id).first()
    return {
        "connected": bool(u and u.personal_max_bot_token),
        "bot_username": (u.personal_max_bot_username if u else None) or None,
        "webhook_set": bool(u and u.personal_max_webhook_set),
        "user_id_known": bool(u and u.personal_max_user_id),
    }


@router.post("/personal-bot/max/connect")
async def personal_max_connect(payload: PersonalBotConnectBody,
                                user: User = Depends(current_user),
                                db: Session = Depends(get_db)):
    from server.personal_bot_relay import (
        max_validate_token, max_set_webhook, compute_token_hash,
    )
    token = (payload.token or "").strip()
    if not token:
        raise HTTPException(400, "Токен не передан")
    info = await max_validate_token(token)
    if not info.get("ok"):
        raise HTTPException(400, f"MAX: {info.get('error', 'unknown')}")
    token_hash = compute_token_hash(token)
    existing = (db.query(User)
                  .filter(User.personal_max_bot_token_hash == token_hash,
                          User.id != user.id)
                  .first())
    if existing:
        raise HTTPException(409, "Этот бот уже подключен к другому аккаунту.")
    u = db.query(User).filter_by(id=user.id).first()
    u.personal_max_bot_token = token
    u.personal_max_bot_username = (info.get("bot_username") or "")[:80]
    u.personal_max_bot_token_hash = token_hash
    u.personal_max_user_id = None
    u.personal_max_webhook_set = False
    db.commit()
    wh = await max_set_webhook(token, token_hash)
    if not wh.get("ok"):
        log.warning(f"[personal-max] setWebhook failed user={user.id}: {wh.get('error')}")
        return {"status": "connected_partial",
                "bot_username": u.personal_max_bot_username,
                "error": wh.get("error")}
    u.personal_max_webhook_set = True
    db.commit()
    try:
        from server.audit_log import log_action
        log_action("user.personal_max_connect", user_id=user.id,
                   target_type="user", target_id=user.id,
                   details={"bot_username": u.personal_max_bot_username})
    except Exception:
        pass
    return {"status": "connected", "bot_username": u.personal_max_bot_username}


@router.post("/personal-bot/max/disconnect")
async def personal_max_disconnect(user: User = Depends(current_user),
                                   db: Session = Depends(get_db)):
    from server.personal_bot_relay import max_delete_webhook
    u = db.query(User).filter_by(id=user.id).first()
    if not u or not u.personal_max_bot_token:
        return {"status": "not_connected"}
    token = u.personal_max_bot_token
    prev_bot = u.personal_max_bot_username
    try:
        await max_delete_webhook(token)
    except Exception:
        pass
    u.personal_max_bot_token = None
    u.personal_max_bot_username = None
    u.personal_max_bot_token_hash = None
    u.personal_max_user_id = None
    u.personal_max_webhook_set = False
    db.commit()
    try:
        from server.audit_log import log_action
        log_action("user.personal_max_disconnect", user_id=user.id,
                   target_type="user", target_id=user.id, level="warn",
                   details={"prev_bot": prev_bot or ""})
    except Exception:
        pass
    return {"status": "disconnected"}


# ── VK community-bot connection (2026-05-28) ───────────────────────────────


class PersonalVKConnectBody(BaseModel):
    token: str       # group access_token (права messages + manage)
    group_id: str    # положительное число — id сообщества


@router.get("/personal-bot/vk/status")
def personal_vk_status(user: User = Depends(current_user),
                       db: Session = Depends(get_db)):
    """Статус подключения VK community-бота юзера."""
    u = db.query(User).filter_by(id=user.id).first()
    return {
        "connected": bool(u and u.personal_vk_bot_token),
        "group_id": (u.personal_vk_group_id if u else None) or None,
        "group_name": (u.personal_vk_group_name if u else None) or None,
        "webhook_set": bool(u and u.personal_vk_webhook_set),
    }


@router.post("/personal-bot/vk/connect")
async def personal_vk_connect(payload: PersonalVKConnectBody,
                              user: User = Depends(current_user),
                              db: Session = Depends(get_db)):
    """Подключить VK-сообщество к личному агенту: валидация через VK API,
    добавление callback-сервера, включение message_new event.

    Юзер должен сначала:
    1. Зайти в VK → Управление → Работа с API → Ключи доступа → Создать ключ
       с правами «Сообщения сообщества» и «Управление сообществом»
    2. Скопировать access_token и group_id из URL сообщества
    """
    from server.personal_bot_relay import (
        vk_validate_token, vk_set_callback, compute_token_hash,
    )
    token = (payload.token or "").strip()
    group_id = (payload.group_id or "").strip().lstrip("-")
    if not token or not group_id:
        raise HTTPException(400, "Токен и group_id обязательны")

    # Валидация через VK API
    info = await vk_validate_token(token, group_id)
    if not info.get("ok"):
        raise HTTPException(400, f"VK: {info.get('error', 'unknown')}")

    # Anti-collision: один токен → один юзер
    token_hash = compute_token_hash(token)
    existing = (db.query(User)
                  .filter(User.personal_vk_bot_token_hash == token_hash,
                          User.id != user.id)
                  .first())
    if existing:
        raise HTTPException(409,
            "Этот VK-бот уже подключен к другому аккаунту. Отвяжи там сначала.")

    # Сохраняем
    u = db.query(User).filter_by(id=user.id).first()
    u.personal_vk_bot_token = token
    u.personal_vk_group_id = group_id
    u.personal_vk_group_name = info.get("group_name", "")
    u.personal_vk_bot_token_hash = token_hash
    u.personal_vk_confirmation = info.get("confirmation_code", "")
    u.personal_vk_webhook_set = False
    db.commit()

    # Прописываем Callback API
    cb = await vk_set_callback(token, group_id, token_hash)
    if not cb.get("ok"):
        log.warning(f"[personal-vk] setCallback failed user={user.id}: {cb.get('error')}")
        return {"status": "connected_partial",
                "group_name": u.personal_vk_group_name,
                "error": f"Бот подключен, но callback не установился: {cb.get('error')}"}
    u.personal_vk_webhook_set = True
    db.commit()

    try:
        from server.audit_log import log_action
        log_action("user.personal_vk_connect", user_id=user.id,
                   target_type="user", target_id=user.id,
                   details={"group_name": u.personal_vk_group_name,
                            "group_id": group_id})
    except Exception:
        pass

    return {"status": "connected",
            "group_name": u.personal_vk_group_name,
            "group_id": group_id,
            "webhook_url": cb.get("webhook_url"),
            "next_step": (f"Открой ВК-сообщество «{u.personal_vk_group_name}», "
                          "включи «Сообщения сообщества» в настройках, "
                          "и напиши боту первое сообщение.")}


@router.post("/personal-bot/vk/disconnect")
async def personal_vk_disconnect(user: User = Depends(current_user),
                                  db: Session = Depends(get_db)):
    """Отключить VK-бота: снять callback + очистить токен."""
    from server.personal_bot_relay import vk_delete_callback
    u = db.query(User).filter_by(id=user.id).first()
    if not u or not u.personal_vk_bot_token:
        return {"status": "not_connected"}
    token = u.personal_vk_bot_token
    group_id = u.personal_vk_group_id
    token_hash = u.personal_vk_bot_token_hash
    prev_group = u.personal_vk_group_name
    if token and group_id and token_hash:
        try:
            await vk_delete_callback(token, group_id, token_hash)
        except Exception as e:
            log.warning(f"[personal-vk] deleteCallback failed: {e}")
    u.personal_vk_bot_token = None
    u.personal_vk_group_id = None
    u.personal_vk_group_name = None
    u.personal_vk_bot_token_hash = None
    u.personal_vk_confirmation = None
    u.personal_vk_webhook_set = False
    db.commit()
    try:
        from server.audit_log import log_action
        log_action("user.personal_vk_disconnect", user_id=user.id,
                   target_type="user", target_id=user.id, level="warn",
                   details={"prev_group": prev_group or ""})
    except Exception:
        pass
    return {"status": "disconnected"}


# ── Calendar connections (Loom Phase 2: модуль 📅 Календарь) ────────────────


@router.get("/calendar/connections")
def list_calendar_connections(user: User = Depends(current_user),
                               db: Session = Depends(get_db)):
    """Список подключённых календарей юзера."""
    from server.models import UserCalendarConnection
    conns = (db.query(UserCalendarConnection)
               .filter_by(user_id=user.id)
               .order_by(UserCalendarConnection.id.asc())
               .all())
    return {
        "connections": [{
            "id": c.id,
            "provider": c.provider,
            "account_email": c.account_email,
            "is_active": bool(c.is_active),
            "last_synced_at": c.last_synced_at.isoformat() if c.last_synced_at else None,
            "last_error": c.last_error,
        } for c in conns]
    }


@router.delete("/calendar/connections/{conn_id}")
def disconnect_calendar(conn_id: int,
                         user: User = Depends(current_user),
                         db: Session = Depends(get_db)):
    """Отключить календарь юзера. Refresh-token/app-password стираются."""
    from server.models import UserCalendarConnection
    c = (db.query(UserCalendarConnection)
           .filter_by(id=conn_id, user_id=user.id)
           .first())
    if not c:
        raise HTTPException(404, "Подключение не найдено")
    prev_provider = c.provider
    prev_email = c.account_email
    db.delete(c)
    db.commit()
    try:
        from server.audit_log import log_action
        log_action("user.calendar_disconnect", user_id=user.id,
                   target_type="user_calendar_connection", target_id=conn_id,
                   details={"provider": prev_provider, "email": prev_email or ""})
    except Exception:
        pass
    return {"status": "disconnected"}


# Google OAuth flow для Calendar (отдельно от login-OAuth)


def _google_calendar_redirect_uri() -> str:
    base = os.getenv("APP_URL", "https://aiche.ru").rstrip("/")
    return f"{base}/user/calendar/google/callback"


@router.get("/calendar/google/connect")
async def google_calendar_oauth_start(request: Request,
                                       user: User = Depends(current_user)):
    """Сгенерировать redirect URL на Google OAuth consent screen для Calendar.

    Scope: calendar.events.readonly (только чтение, без права создания —
    более узкий scope = проще пройти Google verification).

    Возвращает JSON {redirect_url} — фронт сам делает window.location =
    (а не 302 редирект на бэке, чтобы fetch не сломался).
    """
    from urllib.parse import urlencode
    client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    if not client_id:
        raise HTTPException(503, "Google OAuth не настроен (GOOGLE_CLIENT_ID пустой)")
    # State = user.id для линковки в callback (CSRF protection)
    import secrets as _secrets
    state = f"{user.id}.{_secrets.token_urlsafe(16)}"
    # Сохраняем state в БД (или временно в RAM). Для простоты — в RAM cache.
    _save_oauth_state(user.id, state)

    params = {
        "client_id": client_id,
        "redirect_uri": _google_calendar_redirect_uri(),
        "response_type": "code",
        # calendar.events — read + write (нужен для модуля calendar чтобы
        # реально создавать встречи через [ACTION:create_google_event]).
        # Старые connection с .readonly scope получат 403 при попытке
        # создать событие — система предложит переподключиться.
        "scope": "https://www.googleapis.com/auth/calendar.events",
        "access_type": "offline",      # → refresh_token
        "prompt": "consent",            # форсим refresh_token даже если уже давали разрешение
        "state": state,
    }
    return {"redirect_url": "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)}


# RAM-cache для OAuth state (TTL 15 мин). Простое решение для MVP.
_OAUTH_STATES: dict[str, tuple[int, float]] = {}
_OAUTH_STATE_TTL = 900  # 15 мин


def _save_oauth_state(user_id: int, state: str) -> None:
    import time as _time
    now = _time.monotonic()
    # Cleanup expired
    expired = [k for k, (_, ts) in _OAUTH_STATES.items()
                if now - ts > _OAUTH_STATE_TTL]
    for k in expired:
        _OAUTH_STATES.pop(k, None)
    _OAUTH_STATES[state] = (user_id, now)


def _consume_oauth_state(state: str) -> int | None:
    import time as _time
    item = _OAUTH_STATES.pop(state, None)
    if not item:
        return None
    user_id, ts = item
    if _time.monotonic() - ts > _OAUTH_STATE_TTL:
        return None
    return user_id


@router.get("/calendar/google/callback")
async def google_calendar_oauth_callback(code: str = "", state: str = "",
                                          error: str = "",
                                          db: Session = Depends(get_db)):
    """OAuth callback от Google. Обменивает code на refresh_token, сохраняет.

    Этот endpoint открывается в браузере (юзер пришёл после consent), поэтому
    возвращаем HTML с сообщением (success/error) и редиректом на /agents-modular.html.
    """
    from fastapi.responses import HTMLResponse
    from server.models import UserCalendarConnection
    app_url = os.getenv("APP_URL", "https://aiche.ru").rstrip("/")

    def _html(msg: str, success: bool = False) -> HTMLResponse:
        icon = "✅" if success else "❌"
        color = "#7bd968" if success else "#ff6b6b"
        html = f"""<!DOCTYPE html><html><head>
<meta charset="utf-8"><title>Google Calendar — AI Студия Че</title>
<style>body{{background:#1c1c1c;color:#f0e6d8;font:14px/1.5 system-ui;
text-align:center;padding:60px 20px}}
.card{{max-width:480px;margin:0 auto;background:#272018;border:1px solid #4a3f2f;
border-radius:16px;padding:32px}}
.ic{{font-size:48px;margin-bottom:12px}}
.msg{{color:{color};font-size:18px;font-weight:600;margin-bottom:12px}}
.btn{{display:inline-block;margin-top:18px;padding:10px 24px;
background:linear-gradient(135deg,#ff8c42,#ffb347);color:#141210;
border-radius:10px;font-weight:700;text-decoration:none}}</style>
</head><body><div class="card"><div class="ic">{icon}</div>
<div class="msg">{msg}</div>
<a class="btn" href="{app_url}/agents-modular.html">Вернуться в Че</a>
</div>
<script>setTimeout(()=>{{location.href='{app_url}/agents-modular.html'}}, 3000)</script>
</body></html>"""
        return HTMLResponse(html, status_code=200 if success else 400)

    if error:
        return _html(f"Google вернул ошибку: {error}")
    if not code or not state:
        return _html("Не получен code или state от Google")

    user_id = _consume_oauth_state(state)
    if not user_id:
        return _html("Сессия привязки истекла. Попробуй подключить ещё раз.")

    client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
    if not (client_id and client_secret):
        return _html("Google OAuth не настроен на сервере")

    # Обмен code → tokens
    import httpx
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post("https://oauth2.googleapis.com/token", data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": _google_calendar_redirect_uri(),
                "grant_type": "authorization_code",
            })
    except Exception as e:
        log.warning(f"[google-cal-oauth] exchange net err: {type(e).__name__}")
        return _html(f"Сеть упала при обмене кода: {type(e).__name__}")
    if r.status_code != 200:
        log.warning(f"[google-cal-oauth] exchange {r.status_code}: {r.text[:200]}")
        return _html(f"Google отклонил exchange ({r.status_code})")
    try:
        data = r.json()
    except Exception:
        return _html("Google вернул не-JSON")

    refresh_token = data.get("refresh_token")
    access_token = data.get("access_token")
    expires_in = int(data.get("expires_in") or 3600)
    if not refresh_token:
        # Без refresh_token — мы можем сделать первый запрос на access, но
        # не сможем обновить. Это значит юзер уже даёт разрешение раньше — нам
        # вернули только access. Не сохраняем — лучше попросить consent заново.
        return _html("Google не вернул refresh_token (нужно prompt=consent — это баг сервера)")

    # Получим userinfo чтобы узнать email
    account_email = ""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r2 = await client.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {access_token}"}
            )
            if r2.status_code == 200:
                account_email = (r2.json() or {}).get("email", "")
    except Exception:
        pass

    # Сохраняем (или обновляем существующее подключение этого аккаунта)
    existing = (db.query(UserCalendarConnection)
                  .filter_by(user_id=user_id, provider="google",
                              account_email=account_email)
                  .first())
    if existing:
        existing.access_token = access_token
        existing.refresh_token = refresh_token
        existing.token_expires_at = datetime.utcnow() + timedelta(seconds=expires_in - 60)
        existing.is_active = True
        existing.last_error = None
        existing.fail_count = 0
    else:
        existing = UserCalendarConnection(
            user_id=user_id,
            provider="google",
            account_email=account_email,
            calendar_id="primary",
            access_token=access_token,
            refresh_token=refresh_token,
            token_expires_at=datetime.utcnow() + timedelta(seconds=expires_in - 60),
            is_active=True,
        )
        db.add(existing)
    db.commit()

    try:
        from server.audit_log import log_action
        log_action("user.calendar_google_connect", user_id=user_id,
                   target_type="user_calendar_connection", target_id=existing.id,
                   details={"email": account_email})
    except Exception:
        pass

    return _html(f"Google Calendar подключён: {account_email}", success=True)


# Yandex CalDAV — без OAuth, через app-password


class YandexCalDavBody(BaseModel):
    email: str
    app_password: str


@router.post("/calendar/yandex/connect")
async def yandex_caldav_connect(payload: YandexCalDavBody,
                                 user: User = Depends(current_user),
                                 db: Session = Depends(get_db)):
    """Подключить Yandex Calendar через CalDAV (email + app-password)."""
    from server.calendar_sync import yandex_caldav_check_creds
    from server.models import UserCalendarConnection

    email = (payload.email or "").strip().lower()
    app_pwd = (payload.app_password or "").strip()
    if not email or "@" not in email:
        raise HTTPException(400, "Невалидный email")
    if not app_pwd or len(app_pwd) < 8:
        raise HTTPException(400, "Слишком короткий app-password (нужен из id.yandex.ru)")

    # Валидация: проверим что Yandex принимает creds (PROPFIND)
    check = await yandex_caldav_check_creds(email, app_pwd)
    if not check.get("ok"):
        raise HTTPException(400, check.get("error", "Yandex отклонил"))

    existing = (db.query(UserCalendarConnection)
                  .filter_by(user_id=user.id, provider="yandex",
                              account_email=email)
                  .first())
    if existing:
        existing.access_token = app_pwd  # переиспользуем поле для app-password
        existing.is_active = True
        existing.last_error = None
        existing.fail_count = 0
    else:
        existing = UserCalendarConnection(
            user_id=user.id,
            provider="yandex",
            account_email=email,
            calendar_id="events-default",
            access_token=app_pwd,
            is_active=True,
        )
        db.add(existing)
    db.commit()

    try:
        from server.audit_log import log_action
        log_action("user.calendar_yandex_connect", user_id=user.id,
                   target_type="user_calendar_connection", target_id=existing.id,
                   details={"email": email})
    except Exception:
        pass

    return {"status": "connected", "id": existing.id, "account_email": email}


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


# ── Согласие на маркетинговую рассылку (152-ФЗ) ────────────────────────────


class MarketingConsentBody(BaseModel):
    consent: bool


@router.put("/marketing-consent")
def marketing_consent_set(body: MarketingConsentBody,
                           user: User = Depends(current_user),
                           db: Session = Depends(get_db)):
    """Юзер ставит/снимает согласие на маркетинговую рассылку.
    152-ФЗ: согласие на ПДн для маркетинга должно быть отдельным от
    согласия на оферту, и его можно отозвать в любой момент."""
    u = db.query(User).filter_by(id=user.id).first()
    if not u:
        raise HTTPException(404, "Пользователь не найден")
    new_val = bool(body.consent)
    if new_val and not u.marketing_consent:
        u.marketing_consent_at = datetime.utcnow()
    u.marketing_consent = new_val
    db.commit()
    try:
        from server.audit_log import log_action
        log_action("user.marketing_consent",
                   user_id=u.id, target_type="user", target_id=u.id,
                   details={"consent": new_val})
    except Exception:
        pass
    return {"consent": u.marketing_consent,
            "consent_at": u.marketing_consent_at.isoformat() if u.marketing_consent_at else None}


@router.get("/marketing-consent")
def marketing_consent_get(user: User = Depends(current_user),
                           db: Session = Depends(get_db)):
    u = db.query(User).filter_by(id=user.id).first()
    if not u:
        raise HTTPException(404, "Пользователь не найден")
    return {"consent": bool(u.marketing_consent),
            "consent_at": u.marketing_consent_at.isoformat() if u.marketing_consent_at else None}


# ── Web Push (VAPID) ───────────────────────────────────────────────────────


class PushSubscribeBody(BaseModel):
    """PushSubscription JSON из браузера."""
    endpoint: str
    keys: dict   # {p256dh: ..., auth: ...}


@router.get("/push/vapid-public")
def push_vapid_public():
    """Публичный VAPID-ключ для регистрации подписки в браузере."""
    from server.push import VAPID_PUBLIC_KEY, is_configured
    if not is_configured():
        raise HTTPException(503, "Push не настроен на сервере")
    return {"public_key": VAPID_PUBLIC_KEY}


@router.post("/push/subscribe")
def push_subscribe(body: PushSubscribeBody, request: Request,
                    user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    """Регистрация подписки браузера. Идемпотентно: если endpoint уже есть —
    обновляем p256dh/auth (могли смениться)."""
    from server.models import PushSubscription
    from server.push import is_configured
    if not is_configured():
        raise HTTPException(503, "Push не настроен на сервере")
    endpoint = (body.endpoint or "").strip()
    p256dh = (body.keys.get("p256dh") or "").strip() if body.keys else ""
    auth = (body.keys.get("auth") or "").strip() if body.keys else ""
    if not endpoint or not p256dh or not auth:
        raise HTTPException(400, "Неполные данные подписки")
    if not endpoint.startswith("https://"):
        raise HTTPException(400, "endpoint должен быть https://")
    ua = (request.headers.get("user-agent") or "")[:255]
    existing = db.query(PushSubscription).filter_by(endpoint=endpoint).first()
    if existing:
        if existing.user_id != user.id:
            raise HTTPException(409, "Endpoint занят другим юзером")
        existing.p256dh = p256dh
        existing.auth = auth
        existing.user_agent = ua
        db.commit()
        return {"status": "updated", "id": existing.id}
    sub = PushSubscription(user_id=user.id, endpoint=endpoint,
                            p256dh=p256dh, auth=auth, user_agent=ua)
    db.add(sub); db.commit(); db.refresh(sub)
    return {"status": "subscribed", "id": sub.id}


@router.post("/push/unsubscribe")
def push_unsubscribe(body: dict, user: User = Depends(current_user),
                      db: Session = Depends(get_db)):
    """Отписка от push (по endpoint или сразу со всех устройств)."""
    from server.models import PushSubscription
    endpoint = (body.get("endpoint") or "").strip() if isinstance(body, dict) else ""
    q = db.query(PushSubscription).filter_by(user_id=user.id)
    if endpoint:
        q = q.filter_by(endpoint=endpoint)
    n = q.delete(synchronize_session=False)
    db.commit()
    return {"status": "unsubscribed", "deleted": n}


@router.get("/push/status")
def push_status(user: User = Depends(current_user),
                 db: Session = Depends(get_db)):
    """Сколько активных подписок у юзера."""
    from server.models import PushSubscription
    from server.push import is_configured
    cnt = db.query(PushSubscription).filter_by(user_id=user.id).count()
    return {"configured": is_configured(), "subscriptions": cnt}


@router.post("/push/test")
def push_test(user: User = Depends(current_user)):
    """Тестовый push — для проверки что подписка работает."""
    from server.push import push_to_user
    n = push_to_user(user.id, "AI Студия Че", "Тестовое уведомление 🎉",
                      url="/")
    return {"delivered": n}


# ── In-app уведомления (колокольчик) ────────────────────────────────────────
# Источник данных — ActionLog. Колокольчик показывает события релевантные
# юзеру за последние 14 дней + бейдж = сколько с момента last_seen.

# Какие действия попадают в feed колокольчика. Технические action.* / auth.*
# / payment.* фильтруем — юзеру они неинтересны.
_NOTIFY_USER_ACTIONS = {
    "record.created",         # новая заявка из бота
    "proposal.sent",          # КП отправлено
    "proposal.opened",        # клиент открыл КП по public-link
    "site.generate_done",     # сайт готов
    "site.generate_failed",
    "solution.orchestra_started",
    "solution.orchestra_done",
    "solution.orchestra_compare",
    "presentation.done",
    "ai.refund",              # авто-возврат при ошибке
}

# Маппинг action → emoji + русская фраза для UI
_NOTIFY_LABELS = {
    "record.created":            ("📨", "Новая заявка от бота"),
    "proposal.sent":             ("📤", "Коммерческое предложение отправлено"),
    "proposal.opened":           ("👀", "Клиент открыл ваше КП"),
    "site.generate_done":        ("🌐", "Сайт сгенерирован"),
    "site.generate_failed":      ("⚠️", "Генерация сайта не удалась"),
    "solution.orchestra_started":("🚀", "Запущено бизнес-решение"),
    "solution.orchestra_done":   ("✅", "Готов отчёт по решению"),
    "solution.orchestra_compare":("🔬", "Сравнение моделей запущено"),
    "presentation.done":         ("🎬", "Презентация готова"),
    "ai.refund":                 ("↩️", "Возврат за неудачную генерацию"),
}


@router.get("/notifications/recent")
def notifications_recent(db: Session = Depends(get_db),
                          user: User = Depends(current_user)):
    """Список последних 30 событий пользователя за 14 дней + счётчик
    непрочитанных (новее `notifications_last_seen_at`)."""
    from server.models import ActionLog
    cutoff = datetime.utcnow() - timedelta(days=14)
    rows = (db.query(ActionLog)
              .filter(ActionLog.user_id == user.id,
                      ActionLog.action.in_(list(_NOTIFY_USER_ACTIONS)),
                      ActionLog.ts >= cutoff)
              .order_by(ActionLog.ts.desc())
              .limit(30).all())
    last_seen = user.notifications_last_seen_at
    items: list[dict] = []
    unread = 0
    for r in rows:
        emoji, label = _NOTIFY_LABELS.get(r.action, ("📌", r.action))
        is_unread = (last_seen is None) or (r.ts and r.ts > last_seen)
        if is_unread:
            unread += 1
        items.append({
            "id": r.id,
            "action": r.action,
            "emoji": emoji,
            "label": label,
            "target_type": r.target_type,
            "target_id": r.target_id,
            "ts": r.ts.isoformat() if r.ts else None,
            "unread": is_unread,
        })
    return {"items": items, "unread": unread}


@router.post("/notifications/seen")
def notifications_mark_seen(db: Session = Depends(get_db),
                             user: User = Depends(current_user)):
    """Отметить все уведомления прочитанными — обнуляет бейдж колокольчика."""
    user.notifications_last_seen_at = datetime.utcnow()
    db.commit()
    return {"status": "ok"}


@router.get("/recent-objects")
def recent_objects(db: Session = Depends(get_db),
                    user: User = Depends(current_user)):
    """Последние 3 бота / 3 КП / 3 сайта пользователя — для блока
    «Недавнее» на главной (welcome-экран). Возвращает только метаданные."""
    from server.models import (ChatBot as _CB, ProposalProject as _PP,
                                SiteProject as _SP)
    bots = (db.query(_CB).filter_by(user_id=user.id)
              .order_by(_CB.created_at.desc()).limit(3).all())
    proposals = (db.query(_PP).filter_by(user_id=user.id)
                   .order_by(_PP.created_at.desc()).limit(3).all())
    sites = (db.query(_SP).filter_by(user_id=user.id)
               .order_by(_SP.created_at.desc()).limit(3).all())
    return {
        "bots": [{
            "id": b.id, "name": b.name, "status": b.status,
            "created_at": b.created_at.isoformat() if b.created_at else None,
        } for b in bots],
        "proposals": [{
            "id": p.id, "name": p.name,
            "client_name": p.client_name,
            "crm_stage": p.crm_stage, "status": p.status,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        } for p in proposals],
        "sites": [{
            "id": s.id, "name": s.name,
            "gen_status": s.gen_status, "hosted_path": s.hosted_path,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        } for s in sites],
    }


# ── Onboarding flag ─────────────────────────────────────────────────────────


@router.get("/onboarding")
def onboarding_status(user: User = Depends(current_user)):
    """Видел ли юзер welcome-tour. Используется фронтом для авто-показа."""
    return {"completed": bool(user.onboarding_completed)}


@router.post("/onboarding/complete")
def onboarding_complete(db: Session = Depends(get_db),
                         user: User = Depends(current_user)):
    """Юзер прошёл/закрыл welcome-tour. Больше не показываем."""
    user.onboarding_completed = True
    db.commit()
    return {"status": "ok"}
