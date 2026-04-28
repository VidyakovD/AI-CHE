"""
Контекстный AI-помощник по разделам сайта.

Каждый view (proposals.html, presentations.html, ...) на фронте подгружает
плавающий чат-bubble. Bubble стучит сюда: POST /assistant/ask с указанием
секции (`proposals.projects`, `presentations`, ...). Backend подтягивает
соответствующий system-prompt из server/assistant_prompts.py + общий
nav-footer и зовёт дешёвую модель.

Бесплатно для верифицированных пользователей. Лимит 60 вопросов / 12 часов /
юзер (через таблицу rate-limit). Сверху — мягкий отказ с предложением
закинуть вопрос в обычный чат.

Каждый вопрос параллельно классифицируется (question/confusion/complaint/
idea/praise) и записывается в AssistantFeedback. Владелец платформы видит
агрегированную картину болей через /admin/assistant/issues и понимает
куда вкладываться в развитие.
"""
import json
import logging
import time
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Header
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from server.routes.deps import get_db, current_user
from server.models import User, AssistantFeedback
from server.ai import generate_response
from server.assistant_prompts import build_system_prompt, is_known_section
from server.security import _check as _rl_check

log = logging.getLogger(__name__)

router = APIRouter(prefix="/assistant", tags=["assistant"])


# Дешёвая модель по умолчанию: GPT-4o-mini (быстрый + копейки за запрос).
# Юзер с фронта ничего не выбирает — модель определяет backend.
_ASSISTANT_MODEL = "gpt"  # MODEL_REGISTRY["gpt"] → openai/gpt-4o-mini


class AssistantAskReq(BaseModel):
    section: str = Field(..., max_length=64)
    message: str = Field(..., min_length=1, max_length=600)


# Мини-кэш на 5 мин: одинаковый вопрос в одной секции от одного юзера
# не дёргает модель повторно.
_ASK_CACHE_TTL = 300
_ask_cache: dict[tuple[int, str, str], tuple[float, dict]] = {}


def _cache_get(uid: int, section: str, msg: str):
    key = (uid, section, msg)
    item = _ask_cache.get(key)
    if not item:
        return None
    if time.monotonic() - item[0] > _ASK_CACHE_TTL:
        _ask_cache.pop(key, None)
        return None
    return item[1]


def _cache_put(uid: int, section: str, msg: str, value: dict) -> None:
    _ask_cache[(uid, section, msg)] = (time.monotonic(), value)
    # Лёгкая чистка при росте: ограничиваем общий размер 1024 записями
    if len(_ask_cache) > 1024:
        now = time.monotonic()
        for k, (ts, _) in list(_ask_cache.items()):
            if now - ts > _ASK_CACHE_TTL:
                _ask_cache.pop(k, None)


_CLASSIFY_PROMPT = """Ты классифицируешь вопрос пользователя B2B AI-платформы.
Пользователь общается с помощником на странице сервиса, ты определяешь
эмоциональный/прагматический тип сообщения.

Возможные классы:
- question — обычный вопрос «как сделать X», «где найти Y»
- confusion — пользователь не понимает интерфейс/термин/назначение функции
- complaint — жалуется: что-то не работает, медленно, дорого, ошибка
- idea — предлагает добавить функцию или улучшение
- praise — благодарит, хвалит, делится позитивом

Верни ровно один JSON: {"class":"question|confusion|complaint|idea|praise", "confidence": 0-100}
Без объяснений, без markdown, одной строкой.
"""


@router.post("/ask")
def assistant_ask(req: AssistantAskReq,
                  background_tasks: BackgroundTasks,
                  db: Session = Depends(get_db),
                  user: User = Depends(current_user)):
    """Спросить помощника. Бесплатно. Лимит 60 вопросов / 12 часов / юзер."""
    if not user.is_verified:
        raise HTTPException(403, "Подтвердите email, чтобы пользоваться помощником")

    section = (req.section or "").strip().lower()
    if not is_known_section(section):
        # Не падаем — просто используем default, помощник всё равно ответит.
        section = "default"

    message = req.message.strip()
    if not message:
        raise HTTPException(400, "Пустой вопрос")

    # Кэш: дублирующий вопрос → возвращаем готовый ответ, не списывая лимит.
    cached = _cache_get(user.id, section, message)
    if cached is not None:
        return cached

    # Rate-limit: 60 запросов / 12 часов на юзера (бесплатный лимит).
    if not _rl_check(f"assistant:{user.id}", max_calls=60, window_sec=43200):
        raise HTTPException(429,
            "Дневной лимит помощника исчерпан (60 вопросов / 12 ч). "
            "Возвращайтесь завтра или задайте вопрос в обычном чате.")

    system = build_system_prompt(section)
    formatted = [
        {"role": "system", "content": system},
        {"role": "user", "content": message},
    ]
    try:
        ans = generate_response(_ASSISTANT_MODEL, formatted, extra={"max_tokens": 320})
    except Exception as e:
        log.error(f"[assistant] AI error: {type(e).__name__}: {e}")
        raise HTTPException(503, "Помощник временно недоступен. Попробуйте позже.")

    text = ans.get("content", "") if isinstance(ans, dict) else str(ans)
    text = (text or "").strip()
    if not text:
        text = "Не получилось сгенерировать ответ. Попробуйте переформулировать вопрос."

    # Парсим markdown-ссылки [label](href) → структурированный список.
    # В тексте ответа заменяем «[label](href)» на просто «label», чтобы юзер
    # видел чистый текст, а ссылки рендерились отдельным блоком кнопок-чипов
    # снизу сообщения.
    import re
    links = []
    seen_hrefs = set()
    def _link_repl(m):
        label = m.group(1).strip()
        href = m.group(2).strip()
        # Только относительные ссылки внутри сайта — внешние режем (анти-фишинг:
        # AI могла подставить evil-link через prompt injection).
        if href.startswith(("/", "#")) and href not in seen_hrefs and len(links) < 6:
            seen_hrefs.add(href)
            links.append({"label": label, "href": href})
        return label
    cleaned_text = re.sub(r"\[([^\]]{1,80})\]\(([^)]{1,200})\)", _link_repl, text)
    # Также схлопываем тройные пробелы / переносы строки если AI наделал
    cleaned_text = re.sub(r"\n{3,}", "\n\n", cleaned_text).strip()

    # Сохраняем фидбек СИНХРОННО (без embedding'а — embedding в фоне) чтобы
    # вернуть feedback_id юзеру для последующих 👍/👎/💡-меток.
    feedback_id = None
    try:
        from server.db import db_session
        with db_session() as fdb:
            fb = AssistantFeedback(
                user_id=user.id,
                section=section[:64] if section else None,
                message=message[:1500],
                ai_answer=cleaned_text[:2000],
                classification="question",  # уточнится в фоне
                confidence=0,
            )
            fdb.add(fb); fdb.commit(); fdb.refresh(fb)
            feedback_id = fb.id
    except Exception as e:
        log.warning(f"[assistant] feedback init failed: {type(e).__name__}: {e}")

    # Фоновая задача: AI классифицирует тип сообщения + считает embedding
    # для кластеризации похожих жалоб. Обновляет существующую запись.
    if feedback_id:
        background_tasks.add_task(_classify_existing, feedback_id, section, message)

    result = {"answer": cleaned_text, "links": links, "section": section,
              "feedback_id": feedback_id}
    _cache_put(user.id, section, message, result)

    try:
        from server.audit_log import log_action
        log_action("assistant.ask", user_id=user.id, target_type="assistant",
                   target_id=section,
                   details={"len_q": len(message), "len_a": len(text), "links": len(links)})
    except Exception:
        pass

    return result


def _classify_existing(feedback_id: int, section: str, message: str) -> None:
    """Догоняющая классификация для уже созданной записи."""
    try:
        cls_resp = generate_response(
            "gpt",
            [
                {"role": "system", "content": _CLASSIFY_PROMPT},
                {"role": "user", "content": f"[{section or 'unknown'}] {message}"},
            ],
            extra={"max_tokens": 30, "temperature": 0},
        )
        raw = (cls_resp.get("content", "") if isinstance(cls_resp, dict) else "").strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.lower().startswith("json"):
                raw = raw[4:].strip()
        parsed = json.loads(raw) if raw else {}
        cls = parsed.get("class", "question")
        if cls not in ("question", "confusion", "complaint", "idea", "praise"):
            cls = "question"
        confidence = max(0, min(100, int(parsed.get("confidence", 0) or 0)))
    except Exception as e:
        log.warning(f"[assistant] classify failed: {type(e).__name__}: {e}")
        cls, confidence = "question", 0

    # Embedding для семантической группировки похожих жалоб
    embedding_json = None
    try:
        from server.knowledge import _embed_one
        emb = _embed_one(message[:500])
        if emb:
            embedding_json = json.dumps(emb, separators=(",", ":"))
    except Exception:
        pass

    try:
        from server.db import db_session
        with db_session() as db:
            fb = db.query(AssistantFeedback).filter_by(id=feedback_id).first()
            if fb:
                fb.classification = cls
                fb.confidence = confidence
                if embedding_json:
                    fb.embedding_json = embedding_json
                db.commit()
    except Exception as e:
        log.warning(f"[assistant] classify update failed: {type(e).__name__}: {e}")


# ── Явная разметка юзера: 👍 / 👎 / 💡 ─────────────────────────────────────


class FeedbackMarkReq(BaseModel):
    feedback_id: int
    mark: str = Field(..., pattern="^(up|down|idea|clear)$")


@router.post("/feedback")
def assistant_feedback(req: FeedbackMarkReq,
                       db: Session = Depends(get_db),
                       user: User = Depends(current_user)):
    """Юзер ставит 👍/👎/💡 на ответ помощника. clear — сбрасывает."""
    fb = (db.query(AssistantFeedback)
            .filter_by(id=req.feedback_id, user_id=user.id).first())
    if not fb:
        raise HTTPException(404, "Запись не найдена")
    fb.user_mark = None if req.mark == "clear" else req.mark
    # Если пометка thumbs-down или idea — повышаем приоритет в issues
    # (это ставит явный сигнал юзера выше AI-классификации).
    if req.mark == "down" and fb.classification not in ("complaint", "confusion"):
        fb.classification = "complaint"
    elif req.mark == "idea" and fb.classification != "idea":
        fb.classification = "idea"
    db.commit()
    return {"ok": True, "feedback_id": fb.id, "user_mark": fb.user_mark}
