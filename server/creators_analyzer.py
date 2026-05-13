"""Анализ соцсетей: свой профиль или конкурент.

Pipeline:
  1. Perplexity (sonar-reasoning-pro для competitor / sonar-pro для own) —
     собирает свежие данные о канале/профиле/сообществе по URL.
  2. Sonnet — формирует структурированный отчёт под профиль бренда
     с рекомендациями.

Fix-price (списываем ДО вызова, refund если ошибка):
  own profile      → 150 ₽ (1500 коп)
  competitor       → 200 ₽ (2000 коп)
"""
import json
import logging
import re
from typing import Optional
from urllib.parse import urlparse

from server.ai import generate_response
from server.models import CreatorBrand

log = logging.getLogger(__name__)

PRICE_OWN_KOP = 15000   # 150 ₽
PRICE_COMP_KOP = 20000  # 200 ₽

VALID_TARGET_TYPES = {"own", "competitor"}
URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def is_valid_url(s: str) -> bool:
    if not isinstance(s, str) or not URL_RE.match(s):
        return False
    try:
        p = urlparse(s)
        return bool(p.netloc) and "." in p.netloc and len(p.netloc) < 256
    except Exception:
        return False


def cost_for(target_type: str) -> int:
    return PRICE_COMP_KOP if target_type == "competitor" else PRICE_OWN_KOP


def detect_platform(url: str) -> Optional[str]:
    """Угадать платформу по URL."""
    u = (url or "").lower()
    if "t.me/" in u or "telegram.me/" in u:        return "tg"
    if "vk.com/" in u or "vk.ru/" in u:            return "vk"
    if "youtube.com/" in u or "youtu.be/" in u:    return "yt"
    if "instagram.com/" in u:                       return "ig"
    return None


def _research_prompt(brand: CreatorBrand, target_url: str, target_type: str, platform: Optional[str]) -> str:
    try:
        topics = json.loads(brand.topics_json) if brand.topics_json else []
    except Exception:
        topics = []

    is_comp = target_type == "competitor"
    role = "конкурент" if is_comp else "наш собственный канал"
    return f"""Ты — старший SMM-аналитик. Тебе нужно детально проанализировать
{role} по ссылке: {target_url}

Платформа: {platform or 'определи сам'}

Контекст нашего бренда (для сравнения и релевантности):
— Название: {brand.name}
— Ниша: {brand.niche or '—'}
— Продукт: {brand.product or '—'}
— ЦА: {brand.audience or '—'}
— Темы: {', '.join(topics) or '—'}

Найди и приведи ПРАКТИЧЕСКИ ПОЛЕЗНЫЕ факты:

1. **Активность**: как часто публикует, в какое время, какие дни.
2. **Форматы**: какие типы контента доминируют (текст / фото / видео / reels /
   опросы), соотношение в процентах.
3. **Темы**: о чём пишет — конкретные категории / рубрики.
4. **Тон**: формальный / неформальный / экспертный / провокативный.
5. **Лучшие посты**: 3-5 примеров наиболее успешных постов (high reactions /
   shares) с их характеристиками — что общего, почему сработало.
6. **Слабые места / упущения**: что можно было бы делать лучше.
7. **Метрики** (если доступны): подписчики, охваты, реакции, ER.
8. **Уникальные приёмы**: что отличает от других каналов в нише.

Будь конкретен, приводи цитаты из постов, числа, факты. Без воды."""


def _writer_prompt(brand: CreatorBrand, target_url: str, target_type: str,
                   research: str) -> tuple[str, str]:
    is_comp = target_type == "competitor"
    role_intro = (
        "Это анализ КОНКУРЕНТА. Твоя задача — выделить что у него работает "
        "и что мы можем перенять (или сделать ещё лучше)."
        if is_comp else
        "Это анализ НАШЕГО собственного канала. Твоя задача — найти что "
        "улучшить, какие форматы попробовать, какие темы дополнить."
    )

    system = (
        "Ты — главный SMM-стратег. Превращаешь сырое исследование в чёткий "
        "практический отчёт. Используешь markdown с заголовками, списками, "
        "таблицами где уместно. Финал должен быть готов к использованию "
        "руководителем без дополнительной обработки."
    )

    user = f"""{role_intro}

Бренд: {brand.name} (ниша: {brand.niche or '—'})
Анализируемый аккаунт: {target_url}

Сырое исследование (от sonar-pro):
---
{research}
---

Подготовь итоговый отчёт в markdown с разделами:

# Анализ {'конкурента' if is_comp else 'своего канала'}

## 🎯 Главное в 5 строках
(самые важные выводы)

## 📊 Профиль аккаунта
- Активность, форматы, тон

## ✅ Что работает {'у конкурента' if is_comp else 'у нас'}
(сильные стороны с примерами)

## ⚠ Слабые места / возможности
{('(что мы можем сделать лучше)' if is_comp else '(что можно усилить)')}

## 🎬 5 конкретных действий
(нумерованный список — что делать в ближайшие 2-4 недели)

## 💡 Идеи для контента
(3-5 идей постов на основе анализа)

Тон: деловой, экспертный, без воды."""

    return system, user


def run_analysis(
    brand: CreatorBrand,
    target_url: str,
    target_type: str = "own",
    user_id: Optional[int] = None,
) -> dict:
    """Запустить полный анализ. Returns: {"result_md": str, "platform": str|None,
                                             "research_text": str, "models": [...]}."""
    if target_type not in VALID_TARGET_TYPES:
        raise ValueError(f"target_type must be one of {VALID_TARGET_TYPES}")
    if not is_valid_url(target_url):
        raise ValueError("target_url is not a valid http(s) URL")

    platform = detect_platform(target_url)
    models = []

    # 1. Research через Perplexity
    research_prompt = _research_prompt(brand, target_url, target_type, platform)
    px_model = "sonar-reasoning-pro" if target_type == "competitor" else "sonar-pro"
    extra1 = {"_purpose": f"creators_analysis_{target_type}", "max_tokens": 3000}
    if user_id is not None: extra1["_user_id"] = user_id
    r1 = generate_response(
        px_model,
        messages=[{"role": "user", "content": research_prompt}],
        extra=extra1,
    )
    research_text = (r1 or {}).get("content") or ""
    models.append(px_model)

    if not research_text:
        raise RuntimeError("Perplexity вернул пустой ответ — нельзя сделать анализ")

    # 2. Writer через Sonnet
    system, user = _writer_prompt(brand, target_url, target_type, research_text)
    extra2 = {"_purpose": f"creators_analysis_writer", "system": system, "max_tokens": 3500}
    if user_id is not None: extra2["_user_id"] = user_id
    r2 = generate_response(
        "claude-sonnet-4-6",
        messages=[{"role": "user", "content": user}],
        extra=extra2,
    )
    result_md = (r2 or {}).get("content") or ""
    models.append("claude-sonnet-4-6")

    return {
        "result_md": result_md.strip(),
        "platform": platform,
        "research_text": research_text,
        "models": models,
    }
