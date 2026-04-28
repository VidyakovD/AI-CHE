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
"""
import logging
import time
from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from server.routes.deps import get_db, current_user
from server.models import User
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


@router.post("/ask")
def assistant_ask(req: AssistantAskReq,
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
    # Фронт может рендерить их кнопками или просто как ссылки.
    import re
    links = []
    for m in re.finditer(r"\[([^\]]{1,80})\]\(([^)]{1,200})\)", text):
        href = m.group(2).strip()
        # Только относительные ссылки внутри сайта — внешние режем (анти-фишинг
        # на случай prompt injection: AI могла подставить evil-link).
        if href.startswith(("/", "#")) and len(links) < 8:
            links.append({"label": m.group(1).strip(), "href": href})

    result = {"answer": text, "links": links, "section": section}
    _cache_put(user.id, section, message, result)

    try:
        from server.audit_log import log_action
        log_action("assistant.ask", user_id=user.id, target_type="assistant",
                   target_id=section,
                   details={"len_q": len(message), "len_a": len(text), "links": len(links)})
    except Exception:
        pass

    return result
