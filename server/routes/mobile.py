"""
Mobile-режим API: лента событий + голосовые команды.

Endpoints:
  GET  /mobile/feed        — последние 20 событий: новые заявки, ответы на КП,
                             платежи, ошибки. Для дашборда на телефоне.
  POST /mobile/voice/parse — текст команды → структурированное действие.
                             AI парсит «открой КП Иванов» → {action: "open_proposal",
                             query: "иванов"}, фронт делает navigation.
  POST /mobile/voice/transcribe — аудио → текст через Whisper. Используется
                                  как fallback на iOS Safari, где Web Speech
                                  API ограничен.
"""
import os
import json
import logging
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import or_

from server.routes.deps import get_db, current_user, kop_to_rub
from server.models import (
    User, BotRecord, ProposalProject, Transaction, ChatBot,
)
from server.ai import generate_response

log = logging.getLogger(__name__)
router = APIRouter(prefix="/mobile", tags=["mobile"])


@router.get("/feed")
def mobile_feed(db: Session = Depends(get_db),
                user: User = Depends(current_user),
                limit: int = 20):
    """
    Лента последних событий для главной мобильного дашборда.

    Источники (отсортированы по времени, последние 20):
      - bot_records (заявки от ботов): record_type=lead/booking/order/...
      - proposal_projects: новые опен/реплай (crm_stage)
      - transactions: payment, bonus, usage > 100 ₽
    """
    # Соберём события из нескольких источников и склеим по времени.
    events: list[dict] = []
    cutoff = datetime.utcnow() - timedelta(days=14)  # 2 недели назад макс

    # 1. Заявки от ботов
    user_bot_ids = [b.id for b in db.query(ChatBot.id)
                                    .filter_by(user_id=user.id).all()]
    if user_bot_ids:
        records = (db.query(BotRecord)
                     .filter(BotRecord.bot_id.in_(user_bot_ids),
                             BotRecord.created_at >= cutoff)
                     .order_by(BotRecord.created_at.desc())
                     .limit(50).all())
        for r in records:
            label = {
                "lead": "Лид", "booking": "Запись", "order": "Заказ",
                "quiz": "Квиз", "ticket": "Тикет", "subscriber": "Подписка",
                "proposal_sent": "КП отправлено",
            }.get(r.record_type or "", r.record_type or "Запись")
            events.append({
                "type": "bot_record",
                "kind": r.record_type or "record",
                "label": label,
                "title": r.customer_name or r.customer_phone or r.customer_email or "—",
                "subtitle": (label + " · бот " + str(r.bot_id)),
                "ts": r.created_at.isoformat() if r.created_at else None,
                "url": f"/chatbots.html#bot-{r.bot_id}",
                "icon": "📥",
            })

    # 2. Активность по КП — открытия / ответы / выигрыши
    proposals = (db.query(ProposalProject)
                   .filter_by(user_id=user.id)
                   .filter(or_(
                       ProposalProject.opened_at >= cutoff,
                       ProposalProject.replied_at >= cutoff,
                       ProposalProject.won_at >= cutoff,
                       ProposalProject.lost_at >= cutoff,
                   ))
                   .order_by(ProposalProject.id.desc())
                   .limit(50).all())
    for p in proposals:
        # Берём самое позднее событие по этому КП
        candidates = []
        if p.replied_at: candidates.append(("Клиент ответил", p.replied_at, "💬"))
        if p.opened_at:  candidates.append(("Открыто клиентом", p.opened_at, "👁"))
        if p.won_at:     candidates.append(("Сделка выиграна", p.won_at, "🏆"))
        if p.lost_at:    candidates.append(("Отказ клиента", p.lost_at, "❌"))
        if not candidates:
            continue
        candidates.sort(key=lambda x: x[1], reverse=True)
        label, when, icon = candidates[0]
        events.append({
            "type": "proposal",
            "kind": "proposal_event",
            "label": label,
            "title": p.name or f"КП #{p.id}",
            "subtitle": (p.client_name or p.client_email or "—"),
            "ts": when.isoformat(),
            "url": f"/proposals.html#proposal-{p.id}",
            "icon": icon,
        })

    # 3. Транзакции (только большие — 100+ ₽ и платежи)
    txs = (db.query(Transaction)
             .filter_by(user_id=user.id)
             .filter(Transaction.created_at >= cutoff)
             .filter(or_(
                 Transaction.type == "payment",
                 Transaction.type == "bonus",
                 (Transaction.tokens_delta != None) & (Transaction.tokens_delta <= -10_000),
             ))
             .order_by(Transaction.created_at.desc())
             .limit(20).all())
    for t in txs:
        delta = int(t.tokens_delta or 0)
        if t.type == "payment":
            label, icon = "Пополнение", "💰"
        elif t.type == "bonus":
            label, icon = "Бонус", "🎁"
        else:
            label, icon = "Списание", "💸"
        events.append({
            "type": "tx",
            "kind": t.type,
            "label": label,
            "title": (t.description or label)[:80],
            "subtitle": f"{abs(delta)/100:.0f} ₽" + ("" if delta > 0 else " списано"),
            "ts": t.created_at.isoformat() if t.created_at else None,
            "url": "/?tab=history",
            "icon": icon,
        })

    # Сортировка по времени и обрезка
    events = [e for e in events if e.get("ts")]
    events.sort(key=lambda e: e["ts"], reverse=True)
    events = events[:limit]

    # Сводка
    summary = {
        "balance_kop": int(user.tokens_balance or 0),
        "balance_rub": kop_to_rub(user.tokens_balance),
        "low_balance": int(user.tokens_balance or 0) < int(getattr(user, "low_balance_threshold", 0) or 0),
        "user": {"id": user.id, "email": user.email, "name": user.name,
                 "avatar_url": user.avatar_url},
    }

    return {"events": events, "summary": summary}


# ── Голос: разбор команды через AI ────────────────────────────────────────

class VoiceParseReq(BaseModel):
    text: str = Field(..., min_length=1, max_length=300)


_VOICE_SYSTEM_PROMPT = """Ты разбираешь голосовые команды русскоговорящего пользователя B2B AI-платформы.
Возвращай JSON с одним из действий:

{"action": "open_chat"} — открыть чат с ИИ
{"action": "open_proposals"} — открыть КП
{"action": "open_proposal", "query": "иванов"} — найти и открыть КП по имени клиента/проекта
{"action": "create_proposal"} — создать новое КП
{"action": "open_presentations"} — открыть презентации
{"action": "open_sites"} — открыть конструктор сайтов
{"action": "open_chatbots"} — открыть чат-боты
{"action": "open_agents"} — открыть AI-агентов
{"action": "open_solutions"} — открыть бизнес-решения
{"action": "balance"} — показать баланс
{"action": "feed"} — показать ленту событий
{"action": "topup"} — пополнить баланс
{"action": "ask", "query": "<полный вопрос пользователя>"} — если команда не подпадает ни под что выше, юзер задаёт вопрос ИИ
{"action": "unknown"} — если совсем непонятно

Возвращай ТОЛЬКО валидный JSON одной строкой, без объяснений и markdown.
"""


@router.post("/voice/parse")
def voice_parse(req: VoiceParseReq,
                user: User = Depends(current_user)):
    """Текст голосовой команды → структурированное действие через AI."""
    text = req.text.strip()
    if not text:
        raise HTTPException(400, "Пустая команда")

    formatted = [
        {"role": "system", "content": _VOICE_SYSTEM_PROMPT},
        {"role": "user", "content": text},
    ]
    try:
        ans = generate_response("gpt", formatted, extra={"max_tokens": 120, "temperature": 0})
    except Exception as e:
        log.error(f"[voice/parse] AI error: {type(e).__name__}: {e}")
        raise HTTPException(503, "AI временно недоступен")

    raw = (ans.get("content", "") if isinstance(ans, dict) else str(ans)).strip()
    # Часто AI оборачивает в ```json ... ```. Убираем.
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    try:
        parsed = json.loads(raw)
    except Exception:
        log.warning(f"[voice/parse] не парсится JSON: {raw[:120]}")
        parsed = {"action": "ask", "query": text}

    # Валидация
    action = parsed.get("action") if isinstance(parsed, dict) else None
    allowed_actions = {
        "open_chat", "open_proposals", "open_proposal", "create_proposal",
        "open_presentations", "open_sites", "open_chatbots", "open_agents",
        "open_solutions", "balance", "feed", "topup", "ask", "unknown",
    }
    if action not in allowed_actions:
        parsed = {"action": "ask", "query": text}

    return parsed


@router.post("/voice/transcribe")
async def voice_transcribe(audio: UploadFile = File(...),
                           db: Session = Depends(get_db),
                           user: User = Depends(current_user)):
    """Принимает аудио (webm/m4a/wav/ogg, ≤15 МБ) и возвращает текст через Whisper.
    Используется как fallback на iOS Safari, где Web Speech API ограничен.
    Стоимость ~5 ₽ / вызов (фикс)."""
    contents = await audio.read(15 * 1024 * 1024 + 1)
    if len(contents) > 15 * 1024 * 1024:
        raise HTTPException(413, "Аудио больше 15 МБ")

    try:
        from openai import OpenAI
        from server.ai import _get_api_keys
        keys = _get_api_keys("openai")
        if not keys:
            raise HTTPException(503, "OpenAI ключ не настроен")
        cli = OpenAI(api_key=keys[0])
        import io
        f = io.BytesIO(contents)
        f.name = audio.filename or "voice.webm"
        resp = cli.audio.transcriptions.create(
            model="whisper-1", file=f, language="ru",
        )
        text = (getattr(resp, "text", "") or "").strip()
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"[voice/transcribe] Whisper error: {type(e).__name__}: {e}")
        raise HTTPException(503, "Не удалось распознать речь")

    if not text:
        raise HTTPException(422, "Тишина или речь не распознана")

    # Списание (фикс 5 ₽), без жёсткой проверки баланса — фича утилитарная.
    try:
        from server.billing import deduct_atomic
        cost = 500
        charged = deduct_atomic(db, user.id, cost)
        if charged > 0:
            db.add(Transaction(user_id=user.id, type="usage",
                               tokens_delta=-charged,
                               description=f"Голосовой ввод: {len(text)} симв.",
                               model="whisper-1"))
            db.commit()
    except Exception as e:
        log.warning(f"[voice] billing failed: {type(e).__name__}: {e}")

    return {"text": text}
