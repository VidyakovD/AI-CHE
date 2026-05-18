"""Двусторонний мост Креаторы ↔ модуль `copywriter` ИИ-Агента.

Идея модуля 23 (см. docs/modules/23-agents-modular-roadmap.md, Фаза 1):
copywriter — первый «прокачиваемый» модуль платформы. Он должен учиться
на реальных опубликованных постах юзера (а не на синтетике) и обратно
улучшать генерацию в Креаторах.

Связь:
  Креаторы публикуют пост → save_published_to_copywriter() добавляет
    его в module_memory.examples_by_brand[brand_id] (cap 10 per-brand).
  Креаторы готовят следующий пост → load_copywriter_examples() возвращает
    последние N постов ИМЕННО ЭТОГО БРЕНДА, мы подмешиваем их в system
    prompt → LLM подражает стилю этого бренда.

Per-brand изоляция (B-3, 2026-05-18):
  Юзер может вести несколько брендов с разным tone of voice (b2b-стройка
  деловая + личный мама-блог тёплый). Если копить общий стиль — оба
  начнут писать смешанно. Структура memory:
    {
      "examples_by_brand": {"123": [...], "456": [...]},  // активная
      "examples": [...]  // legacy (до B-3), используется как fallback
    }
  При первом save для нового бренда — стартуем с пустого списка. Старые
  legacy examples (если есть) остаются доступны через _legacy_fallback при
  load (когда per-brand bucket ещё пуст), что даёт плавный переход.

Если у юзера нет подключённого активного copywriter-модуля — функции
no-op'ят (Креаторы продолжают работать как раньше).

Лимиты:
  EXAMPLES_CAP — макс. кол-во примеров на бренд (10).
  EXAMPLE_TEXT_TRUNC — обрезка одного поста при сохранении (1500 chars).
  PROMPT_EXAMPLES_LIMIT — сколько примеров инжектим в system prompt (3).
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


def _parse_memory(mod) -> dict:
    """Безопасно прочитать module_memory_json в dict."""
    try:
        memory = json.loads(mod.module_memory_json or "{}")
        return memory if isinstance(memory, dict) else {}
    except Exception:
        return {}


def _brand_bucket(memory: dict, brand_id: int | None) -> list[dict]:
    """Достать список examples для конкретного бренда.

    Если bucket для brand_id пуст ИЛИ brand_id=None — используем
    legacy `examples` как fallback (плавная миграция из pre-B-3 данных).
    """
    by_brand = memory.get("examples_by_brand") or {}
    if not isinstance(by_brand, dict):
        by_brand = {}
    key = str(brand_id) if brand_id is not None else "_default"
    bucket = by_brand.get(key) or []
    if not isinstance(bucket, list):
        bucket = []
    # Если для этого бренда ещё ничего нет — fallback на legacy examples
    if not bucket:
        legacy = memory.get("examples") or []
        if isinstance(legacy, list):
            return legacy
    return bucket


def load_copywriter_examples(db: Session, user_id: int,
                             brand_id: int | None = None,
                             limit: int = PROMPT_EXAMPLES_LIMIT) -> list[dict]:
    """Загрузить последние N постов БРЕНДА из module_memory copywriter.

    brand_id=None → legacy режим: возвращает legacy `examples` (для случаев
    когда нет привязки к бренду, например ручной API-вызов модуля).

    Возвращает [{"text", "platform", "ts", "brand_id"}, ...] — desc по ts.
    Пустой список, если модуль не подключён.
    """
    mod = _get_active_copywriter(db, user_id)
    if not mod:
        return []
    memory = _parse_memory(mod)
    bucket = _brand_bucket(memory, brand_id)
    def _key(e: dict) -> str:
        return str(e.get("ts") or "")
    sorted_ex = sorted(bucket, key=_key, reverse=True)
    return [e for e in sorted_ex[:limit] if isinstance(e, dict) and e.get("text")]


def build_style_block(examples: list[dict]) -> str:
    """Сформировать секцию system-prompt'а с примерами стиля автора.

    Пустая строка если examples пуст — caller просто не подмешает блок.
    """
    if not examples:
        return ""
    parts = ["═══ ПРИМЕРЫ СТИЛЯ АВТОРА (последние опубликованные посты этого бренда) ═══",
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
                                 text: str, platform: str,
                                 brand_id: int | None = None) -> bool:
    """Сохранить опубликованный пост в module_memory copywriter
    для конкретного бренда.

    Возвращает True если сохранили, False если модуль не подключён или
    что-то пошло не так. Не raise'ит — Креаторы продолжают работать.

    Если brand_id=None — пишем в `_default` bucket (используется когда
    публикация идёт не через Креаторы, например через manual invoke).
    """
    if not (text or "").strip():
        return False
    mod = _get_active_copywriter(db, user_id)
    if not mod:
        return False

    memory = _parse_memory(mod)
    by_brand = memory.get("examples_by_brand")
    if not isinstance(by_brand, dict):
        by_brand = {}

    key = str(brand_id) if brand_id is not None else "_default"
    bucket = by_brand.get(key) or []
    if not isinstance(bucket, list):
        bucket = []

    bucket.append({
        "text": text[:EXAMPLE_TEXT_TRUNC].strip(),
        "platform": platform or "?",
        "ts": datetime.utcnow().isoformat(),
        "brand_id": brand_id,
    })
    # Cap per-brand: самые свежие EXAMPLES_CAP штук (по ts)
    if len(bucket) > EXAMPLES_CAP:
        bucket = sorted(bucket,
                        key=lambda e: str(e.get("ts") or ""),
                        reverse=True)[:EXAMPLES_CAP]

    by_brand[key] = bucket
    memory["examples_by_brand"] = by_brand
    mod.module_memory_json = json.dumps(memory, ensure_ascii=False)
    try:
        db.commit()
    except Exception as e:
        log.warning("[creators-bridge] save_published commit failed: %s", e)
        db.rollback()
        return False
    log.info("[creators-bridge] copywriter example saved for user=%s brand=%s "
             "platform=%s bucket_size=%s", user_id, key, platform, len(bucket))
    return True


def get_brand_summary(db: Session, user_id: int) -> dict:
    """Сводка для UI: сколько примеров запомнено по каждому бренду.

    Возвращает {"total": N, "brands": [{"brand_id": ..., "count": M}, ...],
                "has_legacy": bool}.
    Пустой dict если модуль не подключён.
    """
    mod = _get_active_copywriter(db, user_id)
    if not mod:
        return {}
    memory = _parse_memory(mod)
    by_brand = memory.get("examples_by_brand") or {}
    legacy = memory.get("examples") or []
    total = 0
    per_brand = []
    if isinstance(by_brand, dict):
        for key, bucket in by_brand.items():
            if isinstance(bucket, list):
                cnt = len(bucket)
                total += cnt
                # key может быть "_default" или строкой brand_id
                bid: int | None
                try:
                    bid = int(key) if key != "_default" else None
                except ValueError:
                    bid = None
                per_brand.append({"brand_id": bid, "count": cnt})
    return {
        "total": total,
        "brands": sorted(per_brand, key=lambda b: -b["count"]),
        "has_legacy": bool(legacy) and isinstance(legacy, list),
        "legacy_count": len(legacy) if isinstance(legacy, list) else 0,
    }
