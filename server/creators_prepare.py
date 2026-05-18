"""Подготовка отдельного поста (Шаг B) — текст + опц. картинка.

Pipeline:
  is_news=True   → Perplexity research (recency=day) → Sonnet адаптирует под платформу
  is_news=False  → Sonnet сразу пишет финальный текст
  type in {image, reels} → DALL-E генерирует картинку

См. docs/modules/21-creators-roadmap.md → Шаг B (платный, 3 поста/мес бесплатно).

Тариф (default, можно перекрыть через pricing_config в будущем):
  evergreen text                    → 15 ₽ (1500 коп)
  evergreen с картинкой / image     → 30 ₽ (3000 коп)
  reels (сценарий+картинка)         → 30 ₽
  news (Perplexity + Sonnet)        → 25 ₽
  + image отдельно если запрошено  → +15 ₽ к базовой

Freemium: первые 3 поста в календарном месяце бесплатно (CreatorBrand.free_posts_used_this_month).
"""
import json
import logging
import os
from datetime import datetime
from typing import Optional

from sqlalchemy import update as sa_update, case, or_ as sa_or_
from sqlalchemy.orm import Session

from server.ai import generate_response
from server.models import CreatorBrand, ContentItem

log = logging.getLogger(__name__)

# Цены в копейках
PRICE_TEXT_EVERGREEN_KOP = 1500
PRICE_TEXT_NEWS_KOP      = 2500
PRICE_WITH_IMAGE_KOP     = 3000   # text + картинка
PRICE_REELS_KOP          = 3000   # сценарий + картинка превью

FREE_POSTS_PER_MONTH = 3


def compute_cost_kop(item: ContentItem) -> int:
    """Сколько будет стоить подготовка этого поста (если не freemium)."""
    if item.is_news:
        return PRICE_TEXT_NEWS_KOP
    if item.type in ("image", "reels"):
        return PRICE_WITH_IMAGE_KOP
    # text/poll/youtube — без картинки по умолчанию
    return PRICE_TEXT_EVERGREEN_KOP


PLATFORM_LIMITS = {
    "tg": {"max_chars": 1024, "style": "Telegram-канал: короткий читабельный текст, абзацы по 1-2 фразы, можно с эмодзи в меру, без хештегов в теле"},
    "vk": {"max_chars": 1500, "style": "ВКонтакте: дружелюбный текст, 1-3 абзаца, можно с эмодзи, можно с 2-3 хештегами в конце"},
    "yt": {"max_chars": 5000, "style": "YouTube — описание под видео: цепляющая первая строка, далее тайминги по разделам, в конце 5-10 хештегов"},
    "ig": {"max_chars": 2200, "style": "Instagram-пост: эмоциональный, визуальный язык, 1-2 абзаца, в конце 8-15 хештегов"},
}

TYPE_FORMAT = {
    "text":    "обычный текстовый пост",
    "image":   "пост с картинкой — текст + краткое описание желаемой картинки",
    "reels":   "сценарий короткого видео (15-30 сек) — hook, 3-5 сцен, CTA",
    "youtube": "описание YouTube-видео — тайминги, ключевые мысли, CTA",
    "poll":    "пост-опрос — формулировка вопроса + 3-4 варианта",
}


def _build_evergreen_prompt(brand: CreatorBrand, item: ContentItem,
                            style_examples: list[dict] | None = None) -> tuple[str, str]:
    try:
        topics = json.loads(brand.topics_json) if brand.topics_json else []
    except Exception:
        topics = []
    plat = PLATFORM_LIMITS.get(item.platform, PLATFORM_LIMITS["tg"])
    fmt = TYPE_FORMAT.get(item.type, "обычный текстовый пост")

    system = (
        "Ты — SMM-копирайтер высокого уровня. Ты пишешь ОДИН пост по brief'у. "
        "Никаких комментариев, никаких пояснений вокруг — только готовый пост. "
        "Без кликбейта, без воды, по делу."
    )

    # Если у юзера подключён модуль `copywriter` ИИ-Агента — подмешиваем
    # его «выученный стиль» (последние опубликованные посты). LLM подражает.
    # См. server/creators_copywriter_bridge.py.
    if style_examples:
        from server.creators_copywriter_bridge import build_style_block
        block = build_style_block(style_examples)
        if block:
            system = system + "\n\n" + block

    brand_lines = [f"Бренд: {brand.name}"]
    if brand.niche:    brand_lines.append(f"Ниша: {brand.niche}")
    if brand.product:  brand_lines.append(f"Продукт: {brand.product}")
    if brand.audience: brand_lines.append(f"ЦА: {brand.audience}")
    if brand.tone:     brand_lines.append(f"Тон: {brand.tone}")
    if topics:         brand_lines.append(f"Темы: {', '.join(topics)}")
    if brand.stopwords:brand_lines.append(f"НЕ писать: {brand.stopwords}")

    user = f"""{chr(10).join(brand_lines)}

Платформа: {item.platform} ({plat['style']})
Формат: {fmt}
Лимит символов: {plat['max_chars']}

Brief: {item.brief or '— brief пустой, ориентируйся на профиль бренда и придумай что-то релевантное —'}

Напиши готовый пост.""" + (
"""
В конце ДОБАВЬ блок:

---
IMAGE_PROMPT: <одно-два предложения по-английски — что должно быть на картинке>""" if item.type in ("image", "reels") else ""
    )

    return system, user


def _build_news_prompts(brand: CreatorBrand, item: ContentItem,
                        style_examples: list[dict] | None = None
                        ) -> tuple[tuple[str, str], tuple[str, str]]:
    """Возвращает (research_prompts, writer_prompts).

    Research через Perplexity получает свежие факты по brief'у. Writer
    через Sonnet адаптирует под платформу и tone бренда.
    """
    try:
        topics = json.loads(brand.topics_json) if brand.topics_json else []
    except Exception:
        topics = []

    research_user = f"""Найди СВЕЖИЕ (последние 24-72 часа) факты, события или
тренды по теме:

«{item.brief or 'актуальное в нише ' + (brand.niche or '')}»

Контекст бренда:
— Ниша: {brand.niche or '—'}
— Продукт: {brand.product or '—'}
— Темы интереса: {', '.join(topics) or '—'}

Верни 3-5 свежих фактов с источниками. Без воды, только конкретика."""

    plat = PLATFORM_LIMITS.get(item.platform, PLATFORM_LIMITS["tg"])
    writer_system = (
        "Ты — SMM-копирайтер. Тебе даны свежие факты от исследователя. Ты "
        "пишешь ОДИН пост-новость на их основе. Только финальный текст, "
        "никаких пояснений."
    )
    # Мост к модулю copywriter (см. _build_evergreen_prompt).
    if style_examples:
        from server.creators_copywriter_bridge import build_style_block
        block = build_style_block(style_examples)
        if block:
            writer_system = writer_system + "\n\n" + block
    brand_lines = [f"Бренд: {brand.name}", f"Тон: {brand.tone or 'нейтральный'}"]
    if brand.audience: brand_lines.append(f"ЦА: {brand.audience}")
    if brand.stopwords:brand_lines.append(f"НЕ писать: {brand.stopwords}")

    writer_user = (
        f"{chr(10).join(brand_lines)}\n\n"
        f"Платформа: {item.platform} ({plat['style']})\n"
        f"Лимит символов: {plat['max_chars']}\n\n"
        "Свежие факты:\n<RESEARCH_PLACEHOLDER>\n\n"
        "Напиши пост-новость для нашей аудитории на основе этих фактов. "
        "Не пересказывай — добавь авторский угол под бренд."
    )

    return ((None, research_user), (writer_system, writer_user))


def _generate_image(image_prompt: str, user_id: Optional[int]) -> Optional[str]:
    """Генерация картинки через DALL-E. Возвращает путь /uploads/... или None."""
    try:
        extra = {"_purpose": "creators_image"}
        if user_id is not None:
            extra["_user_id"] = user_id
        resp = generate_response(
            "dall-e-3",
            messages=[{"role": "user", "content": image_prompt}],
            extra=extra,
        )
        # generate_response возвращает {"type": "image", "content": "/uploads/...png"} либо текст
        url = (resp or {}).get("content")
        if url and isinstance(url, str) and (url.startswith("/uploads/") or url.startswith("http")):
            return url
        log.warning("[creators.prepare] image response unexpected: %s", repr(url)[:120])
        return None
    except Exception as e:
        log.warning("[creators.prepare] image gen failed: %s", e)
        return None


def _strip_image_prompt(text: str) -> tuple[str, Optional[str]]:
    """Отделить IMAGE_PROMPT: ... от тела поста."""
    if not text:
        return text, None
    marker = "IMAGE_PROMPT:"
    idx = text.rfind(marker)
    if idx < 0:
        return text.strip(), None
    body = text[:idx].rstrip().rstrip("-").rstrip()
    prompt = text[idx + len(marker):].strip().strip("`").strip()
    # Уберём trailing fence
    if prompt.endswith("```"):
        prompt = prompt[:-3].strip()
    return body, (prompt or None)


def prepare_item(item: ContentItem, brand: CreatorBrand, user_id: Optional[int] = None,
                 with_image: Optional[bool] = None,
                 db: Optional[Session] = None) -> dict:
    """Подготовить пост: вернуть {"text": ..., "media_url": ...|None, "model_chain": [...]}.

    Не списывает баланс — это делает caller (routes).

    Если у юзера подключён модуль `copywriter` ИИ-Агента — подмешиваем
    выученный стиль (последние опубликованные посты) в system prompt.
    db нужна для этого; если не передана — bridge пропускается (Креаторы
    работают как раньше).
    """
    if with_image is None:
        with_image = item.type in ("image", "reels")

    model_chain = []

    # Bridge → copywriter module. No-op если модуль не подключён.
    # Per-brand: загружаем стиль ТОЛЬКО этого бренда (B-3).
    style_examples: list[dict] = []
    if db is not None and user_id is not None:
        try:
            from server.creators_copywriter_bridge import load_copywriter_examples
            style_examples = load_copywriter_examples(db, user_id, brand_id=brand.id)
            if style_examples:
                log.info("[creators.prepare] copywriter style applied: %d examples (brand=%s)",
                         len(style_examples), brand.id)
        except Exception as e:
            log.warning("[creators.prepare] copywriter bridge failed: %s", e)

    if item.is_news:
        (_, research_user), (writer_system, writer_user) = _build_news_prompts(brand, item, style_examples)
        # 1) Perplexity research
        extra1 = {"_purpose": "creators_news_research"}
        if user_id is not None: extra1["_user_id"] = user_id
        r1 = generate_response(
            "sonar-pro",
            messages=[{"role": "user", "content": research_user}],
            extra={**extra1, "max_tokens": 1500},
        )
        research_text = (r1 or {}).get("content") or ""
        model_chain.append("sonar-pro")
        # 2) Writer Sonnet
        writer_user_filled = writer_user.replace("<RESEARCH_PLACEHOLDER>", research_text or "(нет свежих данных)")
        extra2 = {"_purpose": "creators_news_writer", "system": writer_system, "max_tokens": 2000}
        if user_id is not None: extra2["_user_id"] = user_id
        r2 = generate_response(
            "claude-sonnet-4-6",
            messages=[{"role": "user", "content": writer_user_filled}],
            extra=extra2,
        )
        full_text = (r2 or {}).get("content") or ""
        model_chain.append("claude-sonnet-4-6")
    else:
        system, user = _build_evergreen_prompt(brand, item, style_examples)
        extra = {"_purpose": "creators_evergreen", "system": system, "max_tokens": 2000}
        if user_id is not None: extra["_user_id"] = user_id
        r = generate_response(
            "claude-sonnet-4-6",
            messages=[{"role": "user", "content": user}],
            extra=extra,
        )
        full_text = (r or {}).get("content") or ""
        model_chain.append("claude-sonnet-4-6")

    body, image_prompt = _strip_image_prompt(full_text)

    media_url = None
    if with_image:
        img_prompt = image_prompt or (item.brief and item.brief[:200]) or f"{brand.name} {brand.niche or ''}"
        # Photoreal style for marketing posts
        full_img_prompt = (img_prompt + ". Photorealistic, high-quality, marketing-ready.")[:1000]
        media_url = _generate_image(full_img_prompt, user_id)
        if media_url:
            model_chain.append("dall-e-3")

    return {
        "text": body,
        "media_url": media_url,
        "model_chain": model_chain,
        "had_image_prompt": bool(image_prompt),
    }


# ── Freemium counter ─────────────────────────────────────────────────────────

def _month_key(dt: Optional[datetime] = None) -> str:
    dt = dt or datetime.utcnow()
    return dt.strftime("%Y-%m")


def freemium_status(brand: CreatorBrand) -> dict:
    """Сколько постов уже бесплатно подготовлено в этом месяце.

    Сбрасываем при смене месяца — храним `free_posts_reset_at` = первое число
    месяца (UTC). Если оно < начало текущего месяца, обнуляем счётчик.
    """
    now = datetime.utcnow()
    cur_month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    used = int(brand.free_posts_used_this_month or 0)
    reset_at = brand.free_posts_reset_at
    if not reset_at or reset_at < cur_month_start:
        used = 0  # будем сбрасывать на caller-стороне при списании
    remaining = max(0, FREE_POSTS_PER_MONTH - used)
    return {
        "used": used,
        "remaining": remaining,
        "limit": FREE_POSTS_PER_MONTH,
        "is_free_eligible": remaining > 0,
        "current_month": _month_key(now),
    }


def consume_freemium(db: Session, brand_id: int) -> bool:
    """Атомарно списать 1 freemium-credit. Возвращает True если списали,
    False если месячный лимит уже исчерпан конкурентным запросом.

    Один SQL UPDATE с CASE для rollover на новый месяц + увеличение счётчика;
    WHERE гарантирует что rowcount=0 если лимит исчерпан → защита от race
    на multi-worker (две параллельные prepare-запроса не выдадут 2 free поверх лимита).

    Caller делает db.commit().
    """
    now = datetime.utcnow()
    cur_month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    new_month = sa_or_(
        CreatorBrand.free_posts_reset_at.is_(None),
        CreatorBrand.free_posts_reset_at < cur_month_start,
    )
    res = db.execute(
        sa_update(CreatorBrand)
        .where(
            CreatorBrand.id == brand_id,
            sa_or_(new_month, CreatorBrand.free_posts_used_this_month < FREE_POSTS_PER_MONTH),
        )
        .values(
            free_posts_used_this_month=case(
                (new_month, 1),
                else_=CreatorBrand.free_posts_used_this_month + 1,
            ),
            free_posts_reset_at=case(
                (new_month, cur_month_start),
                else_=CreatorBrand.free_posts_reset_at,
            ),
        )
    )
    return (res.rowcount or 0) > 0


def refund_freemium(db: Session, brand_id: int) -> None:
    """Откатить 1 freemium-credit при ошибке pipeline. Только если used > 0.
    Атомарный UPDATE — без RMW. Caller делает db.commit()."""
    db.execute(
        sa_update(CreatorBrand)
        .where(
            CreatorBrand.id == brand_id,
            CreatorBrand.free_posts_used_this_month > 0,
        )
        .values(free_posts_used_this_month=CreatorBrand.free_posts_used_this_month - 1)
    )
