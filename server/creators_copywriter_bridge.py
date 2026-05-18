"""Двусторонний мост Креаторы ↔ модуль `copywriter` ИИ-Агента.

Идея модуля 23 (см. docs/modules/23-agents-modular-roadmap.md, Фаза 1):
copywriter — первый «прокачиваемый» модуль платформы. Он должен учиться
на реальных опубликованных постах юзера (а не на синтетике) и обратно
улучшать генерацию в Креаторах.

Связь:
  Креаторы публикуют пост → save_published_to_copywriter() добавляет
    его в module_memory.examples (cap 10, top by ts).
  Креаторы готовят следующий пост → load_copywriter_examples() возвращает
    последние N постов, мы подмешиваем их в system prompt → LLM подражает
    стилю.

Если у юзера нет подключённого активного copywriter-модуля — функции
no-op'ят (Креаторы продолжают работать как раньше).

Лимиты:
  EXAMPLES_CAP — макс. кол-во хранимых примеров (10). Старые отбрасываются.
  EXAMPLE_TEXT_TRUNC — обрезка одного поста при сохранении (1500 chars).
    Этого хватит для стиля + не раздуёт module_memory_json (PostgreSQL TEXT).
  PROMPT_EXAMPLES_LIMIT — сколько примеров инжектим в system prompt (3).
    Больше → много токенов на каждом вызове; LLM и так подражает с 2-3.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


EXAMPLES_CAP = 10
EXAMPLE_TEXT_TRUNC = 1500
PROMPT_EXAMPLES_LIMIT = 3


def _get_active_copywriter(db: Session, user_id: int) -> Any:
    """Найти активный copywriter-модуль юзера или None.

    Изолировано в функцию чтобы тесты могли monkeypatch'нуть.
    """
    if not user_id:
        return None
    try:
        from server.models import Agent, AgentModule
        agent = (db.query(Agent)
                   .filter(Agent.user_id == user_id,
                           Agent.status != "archived")
                   .first())
        if not agent:
            return None
        return (db.query(AgentModule)
                  .filter(AgentModule.agent_id == agent.id,
                          AgentModule.slug == "copywriter",
                          AgentModule.is_enabled.is_(True))
                  .first())
    except Exception as e:
        log.warning("[creators-bridge] copywriter lookup failed: %s", e)
        return None


def load_copywriter_examples(db: Session, user_id: int,
                             limit: int = PROMPT_EXAMPLES_LIMIT) -> list[dict]:
    """Загрузить последние N опубликованных постов из module_memory copywriter.

    Возвращает [{"text": str, "platform": str, "ts": str}, ...] — отсортировано
    от новых к старым. Пустой список, если модуль не подключён.
    """
    mod = _get_active_copywriter(db, user_id)
    if not mod:
        return []
    try:
        memory = json.loads(mod.module_memory_json or "{}")
    except Exception:
        return []
    examples = memory.get("examples") or []
    if not isinstance(examples, list):
        return []
    # Отсортировать по ts desc — на случай если был bulk-import не по порядку
    def _key(e: dict) -> str:
        return str(e.get("ts") or "")
    sorted_ex = sorted(examples, key=_key, reverse=True)
    return [e for e in sorted_ex[:limit] if isinstance(e, dict) and e.get("text")]


def build_style_block(examples: list[dict]) -> str:
    """Сформировать секцию system-prompt'а с примерами стиля автора.

    Пустая строка если examples пуст — caller просто не подмешает блок.
    """
    if not examples:
        return ""
    parts = ["═══ ПРИМЕРЫ СТИЛЯ АВТОРА (последние опубликованные посты) ═══",
             "Автор уже выложил вот эти посты — пиши в ТАКОМ ЖЕ стиле:",
             "  • тон обращения (Ты/Вы, тёплый/деловой)",
             "  • длина абзацев и предложений",
             "  • эмодзи (есть/нет, плотность)",
             "  • типичные слова и обороты автора"]
    for i, ex in enumerate(examples, 1):
        plat = ex.get("platform", "?")
        text = (ex.get("text") or "")[:600]
        parts.append(f"\n— Пост {i} ({plat}) —\n{text}")
    parts.append("\nНе копируй дословно — подражай стилю. Тема и факты могут быть другими.")
    return "\n".join(parts)


def save_published_to_copywriter(db: Session, user_id: int,
                                 text: str, platform: str) -> bool:
    """Сохранить только что опубликованный пост в module_memory copywriter.

    Возвращает True если сохранили, False если модуль не подключён или
    что-то пошло не так. Не raise'ит — Креаторы продолжают работать.

    Caller (publish_item) уже сделал db.commit() своих изменений; здесь
    мы делаем отдельный update модуля и тоже коммитим.
    """
    if not (text or "").strip():
        return False
    mod = _get_active_copywriter(db, user_id)
    if not mod:
        return False
    try:
        memory = json.loads(mod.module_memory_json or "{}")
    except Exception:
        memory = {}

    examples = memory.get("examples") or []
    if not isinstance(examples, list):
        examples = []
    examples.append({
        "text": text[:EXAMPLE_TEXT_TRUNC].strip(),
        "platform": platform or "?",
        "ts": datetime.utcnow().isoformat(),
    })
    # Cap: храним самые свежие EXAMPLES_CAP штук (по ts)
    if len(examples) > EXAMPLES_CAP:
        examples = sorted(examples,
                          key=lambda e: str(e.get("ts") or ""),
                          reverse=True)[:EXAMPLES_CAP]
    memory["examples"] = examples
    mod.module_memory_json = json.dumps(memory, ensure_ascii=False)
    try:
        db.commit()
    except Exception as e:
        log.warning("[creators-bridge] save_published commit failed: %s", e)
        db.rollback()
        return False
    log.info("[creators-bridge] copywriter example saved for user=%s platform=%s "
             "total_examples=%s", user_id, platform, len(examples))
    return True
