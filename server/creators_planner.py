"""Генерация контент-плана для бренда через Sonnet.

См. docs/modules/21-creators-roadmap.md → Шаг A (один Sonnet-вызов на ~30 постов).

Эта стадия БЕСПЛАТНА (входит в freemium). Сам план — это только разметка
дат+типов+brief'ов. Подготовка реального текста/картинок — Шаг B (платный,
3 поста/мес бесплатно).
"""
import json
import logging
import random
import re
from datetime import datetime, timedelta
from typing import Optional

from server.ai import generate_response
from server.models import CreatorBrand

log = logging.getLogger(__name__)

# Лимиты Шаг A
PLAN_MAX_DAYS = 35
PLAN_DEFAULT_DAYS = 30
PLAN_MIN_DAYS = 7

# Кол-во постов на платформу в неделю (intensity = normal)
WEEKLY_FREQ = {
    "tg": 4,
    "vk": 3,
    "yt": 1,   # видео + ~2 short — но в MVP считаем как 1 «слот»
    "ig": 4,
}

# Распределение типов на платформу (вероятности; нормализуются)
PLATFORM_TYPE_WEIGHTS = {
    "tg": {"text": 5, "image": 3, "news": 2, "poll": 1},
    "vk": {"text": 4, "image": 4, "news": 1, "poll": 1},
    "yt": {"youtube": 4, "reels": 2},
    "ig": {"image": 5, "reels": 3, "text": 1},
}

# Время постинга по платформам (МСК)
PLATFORM_HOURS_MSK = {
    "tg": [10, 13, 18, 20],
    "vk": [11, 14, 19],
    "yt": [17, 19],
    "ig": [9, 13, 18, 21],
}

VALID_PLATFORMS = {"tg", "vk", "yt", "ig"}


def _weighted_pick(weights: dict) -> str:
    """Выбрать ключ с весом."""
    items = list(weights.items())
    total = sum(w for _, w in items)
    r = random.uniform(0, total)
    acc = 0
    for key, w in items:
        acc += w
        if r <= acc:
            return key
    return items[-1][0]


def _propose_slots(period_start: datetime, days: int, platforms: list[str]) -> list[dict]:
    """Сгенерировать слоты (date+time+platform+type) равномерно по периоду.

    Возвращает список dict без content — только разметка. Sonnet потом
    заполнит brief'ы по этим слотам.
    """
    slots = []
    weeks = max(1, days // 7)
    msk_offset = timedelta(hours=3)  # UTC+3 на проде

    for platform in platforms:
        weekly = WEEKLY_FREQ.get(platform, 3)
        total_for_platform = weekly * weeks
        # Случайно разбрасываем по дням
        days_indexes = sorted(random.sample(range(days), min(total_for_platform, days)))
        for di in days_indexes:
            d = period_start + timedelta(days=di)
            hour_msk = random.choice(PLATFORM_HOURS_MSK[platform])
            # МСК → UTC (отнимаем 3 часа)
            schedule_at = d.replace(hour=hour_msk, minute=0, second=0, microsecond=0) - msk_offset
            type_w = PLATFORM_TYPE_WEIGHTS[platform]
            ctype = _weighted_pick(type_w)
            is_news = (ctype == "news")
            slots.append({
                "schedule_at": schedule_at,
                "platform": platform,
                "type": ctype if ctype != "news" else "text",  # news → text-формат, но с флагом
                "is_news": is_news,
            })

    slots.sort(key=lambda x: x["schedule_at"])
    return slots


def _build_prompt(brand: CreatorBrand, slots: list[dict], days: int) -> tuple[str, str]:
    """Системный + юзер-промпт для Sonnet."""
    try:
        topics = json.loads(brand.topics_json) if brand.topics_json else []
    except Exception:
        topics = []

    system = (
        "Ты — старший SMM-стратег с 10-летним опытом. Ты строишь контент-планы "
        "для российских брендов с учётом специфики ниши, аудитории и платформ. "
        "Твоя задача — для каждого предложенного слота (дата+платформа+тип) "
        "сформулировать КРАТКИЙ brief (одна-две фразы, по сути: о чём этот пост, "
        "какой angle, что хотим донести). Не пиши готовый текст — это будет "
        "следующий этап. Сейчас — план."
    )

    brand_block = [f"**Бренд:** {brand.name}"]
    if brand.niche:
        brand_block.append(f"**Ниша:** {brand.niche}")
    if brand.product:
        brand_block.append(f"**Продукт:** {brand.product}")
    if brand.audience:
        brand_block.append(f"**Аудитория:** {brand.audience}")
    if brand.tone:
        brand_block.append(f"**Тон:** {brand.tone}")
    if topics:
        brand_block.append(f"**Темы:** {', '.join(topics)}")
    if brand.stopwords:
        brand_block.append(f"**Стоп-слова (НЕ писать):** {brand.stopwords}")

    slots_block = []
    for i, s in enumerate(slots, 1):
        date_str = s["schedule_at"].strftime("%Y-%m-%d %H:%M")
        news_tag = " [актуальная новость]" if s["is_news"] else ""
        slots_block.append(f"{i}. {date_str} UTC · {s['platform']} · {s['type']}{news_tag}")

    user = f"""Профиль бренда:
{chr(10).join(brand_block)}

План на {days} дней — нужно проставить brief к каждому слоту.

Слоты:
{chr(10).join(slots_block)}

Верни СТРОГО JSON-массив той же длины и порядка:
```json
[
  {{"idx": 1, "brief": "..."}},
  {{"idx": 2, "brief": "..."}},
  ...
]
```

Brief: одна-две фразы по-русски, о чём пост, какой angle. Для слотов с
тегом [актуальная новость] — указать категорию новости (тренд / событие /
обновление в нише), без конкретики, так как готовиться будет в день постинга.

Никаких хештегов, никакого готового текста, никаких CTA — только концепт.
Разнообразие тем — обязательно (не повторяй одно и то же).
"""
    return system, user


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\[[\s\S]*?\])\s*```", re.IGNORECASE)


def _extract_json_array(text: str) -> Optional[list]:
    """Достать JSON-массив из ответа Sonnet (может быть в ```json блоке)."""
    if not text:
        return None
    m = _JSON_BLOCK_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # Fallback: первый '[' до последнего ']'
    a, b = text.find('['), text.rfind(']')
    if 0 <= a < b:
        try:
            return json.loads(text[a:b + 1])
        except Exception:
            pass
    return None


def generate_plan(
    brand: CreatorBrand,
    *,
    period_start: Optional[datetime] = None,
    days: int = PLAN_DEFAULT_DAYS,
    platforms: Optional[list[str]] = None,
    user_id: Optional[int] = None,
) -> dict:
    """Сгенерировать контент-план для бренда.

    Returns:
        {"items": [{"schedule_at": dt, "platform": str, "type": str,
                    "is_news": bool, "brief": str}, ...],
         "period_start": dt, "period_end": dt,
         "model": "claude-sonnet-4-6", "raw_brief_count": int}
    """
    days = max(PLAN_MIN_DAYS, min(PLAN_MAX_DAYS, int(days or PLAN_DEFAULT_DAYS)))
    period_start = period_start or datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    period_end = period_start + timedelta(days=days)

    platforms = [p for p in (platforms or list(VALID_PLATFORMS)) if p in VALID_PLATFORMS]
    if not platforms:
        platforms = list(VALID_PLATFORMS)

    slots = _propose_slots(period_start, days, platforms)
    if not slots:
        return {"items": [], "period_start": period_start, "period_end": period_end,
                "model": "claude-sonnet-4-6", "raw_brief_count": 0}

    system, user = _build_prompt(brand, slots, days)
    log.info(f"[creators.plan] brand={brand.id} slots={len(slots)} days={days} platforms={platforms}")

    extra = {"_purpose": "creators_plan"}
    if user_id is not None:
        extra["_user_id"] = user_id

    resp = generate_response(
        "claude-sonnet-4-6",
        messages=[{"role": "user", "content": user}],
        extra={**extra, "system": system, "max_tokens": 4000},
    )
    content = (resp or {}).get("content") or ""
    parsed = _extract_json_array(content)

    # Заполняем brief'ы по idx; если Sonnet вернул мусор — пустые
    brief_map: dict[int, str] = {}
    if isinstance(parsed, list):
        for entry in parsed:
            if isinstance(entry, dict):
                idx = entry.get("idx")
                brief = entry.get("brief")
                if isinstance(idx, int) and isinstance(brief, str):
                    brief_map[idx] = brief.strip()

    items = []
    for i, s in enumerate(slots, 1):
        items.append({
            "schedule_at": s["schedule_at"],
            "platform": s["platform"],
            "type": s["type"],
            "is_news": s["is_news"],
            "brief": brief_map.get(i, ""),
        })

    return {
        "items": items,
        "period_start": period_start,
        "period_end": period_end,
        "model": "claude-sonnet-4-6",
        "raw_brief_count": len(brief_map),
    }
