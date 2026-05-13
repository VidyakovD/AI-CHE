"""Креаторы — рабочая зона контент-планирования для бизнеса/SMM.

См. docs/modules/21-creators-roadmap.md.

В этой итерации (MVP-1):
- CRUD `CreatorBrand` (профиль бренда — мульти на юзера)
- Заглушки для calendar/items/channels/analysis (вернутся в следующих итерациях)
"""
import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from server.routes.deps import get_db, current_user
from server.models import (
    User, CreatorBrand, ContentCalendar, ContentItem,
    CreatorChannelConnection, CreatorAnalysisRun,
)
from server.audit_log import log_action
from server.billing import deduct_strict
from server.models import Transaction
from server.creators_planner import generate_plan, PLAN_MIN_DAYS, PLAN_MAX_DAYS, PLAN_DEFAULT_DAYS, VALID_PLATFORMS as VALID_PLATFORMS_PLANNER
from server.creators_prepare import (
    prepare_item as _prepare_item_pipeline,
    compute_cost_kop,
    freemium_status,
    consume_freemium,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/creators", tags=["creators"])


# ── Pydantic ──────────────────────────────────────────────────────────────────

VALID_NICHES = {
    "ecommerce", "services", "it", "manufacturing", "food",
    "medicine", "beauty", "education", "realestate", "other",
}
VALID_TONES = {"friendly", "expert", "premium", "provocative", "neutral"}
VALID_PLATFORMS = {"tg", "vk", "yt", "ig"}
MAX_BRANDS_PER_USER = 10
MAX_TOPICS = 8


class BrandIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    niche: Optional[str] = None
    product: Optional[str] = None
    audience: Optional[str] = None
    tone: Optional[str] = None
    topics: Optional[list[str]] = None
    stopwords: Optional[str] = None
    logo_url: Optional[str] = None


def _brand_dict(b: CreatorBrand) -> dict:
    try:
        topics = json.loads(b.topics_json) if b.topics_json else []
    except Exception:
        topics = []
    return {
        "id": b.id,
        "name": b.name,
        "niche": b.niche,
        "product": b.product,
        "audience": b.audience,
        "tone": b.tone,
        "topics": topics,
        "stopwords": b.stopwords,
        "logo_url": b.logo_url,
        "free_posts_used": int(b.free_posts_used_this_month or 0),
        "created_at": b.created_at.isoformat() if b.created_at else None,
        "updated_at": b.updated_at.isoformat() if b.updated_at else None,
    }


# ── Brand CRUD ────────────────────────────────────────────────────────────────

@router.get("/brands")
def list_brands(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Список брендов юзера."""
    brands = db.query(CreatorBrand).filter_by(user_id=user.id).order_by(CreatorBrand.id.desc()).all()
    return {"brands": [_brand_dict(b) for b in brands]}


@router.post("/brands")
def create_brand(
    payload: BrandIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Создать новый бренд."""
    count = db.query(CreatorBrand).filter_by(user_id=user.id).count()
    if count >= MAX_BRANDS_PER_USER:
        raise HTTPException(400, f"Лимит брендов: {MAX_BRANDS_PER_USER}. Удалите ненужные.")

    _validate_brand(payload)

    b = CreatorBrand(
        user_id=user.id,
        name=payload.name.strip(),
        niche=payload.niche,
        product=payload.product,
        audience=payload.audience,
        tone=payload.tone,
        topics_json=json.dumps(payload.topics, ensure_ascii=False) if payload.topics else None,
        stopwords=payload.stopwords,
        logo_url=payload.logo_url,
    )
    db.add(b)
    db.commit()
    db.refresh(b)
    log_action("creator.brand_created", user_id=user.id, brand_id=b.id, name=b.name)
    return _brand_dict(b)


@router.get("/brands/{brand_id}")
def get_brand(brand_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    b = db.query(CreatorBrand).filter_by(id=brand_id, user_id=user.id).first()
    if not b:
        raise HTTPException(404, "Бренд не найден")
    return _brand_dict(b)


@router.put("/brands/{brand_id}")
def update_brand(
    brand_id: int,
    payload: BrandIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    b = db.query(CreatorBrand).filter_by(id=brand_id, user_id=user.id).first()
    if not b:
        raise HTTPException(404, "Бренд не найден")

    _validate_brand(payload)

    b.name = payload.name.strip()
    b.niche = payload.niche
    b.product = payload.product
    b.audience = payload.audience
    b.tone = payload.tone
    b.topics_json = json.dumps(payload.topics, ensure_ascii=False) if payload.topics else None
    b.stopwords = payload.stopwords
    b.logo_url = payload.logo_url
    db.commit()
    db.refresh(b)
    log_action("creator.brand_updated", user_id=user.id, brand_id=b.id)
    return _brand_dict(b)


@router.delete("/brands/{brand_id}")
def delete_brand(brand_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    b = db.query(CreatorBrand).filter_by(id=brand_id, user_id=user.id).first()
    if not b:
        raise HTTPException(404, "Бренд не найден")
    db.delete(b)  # cascade удалит календари / items / channels / analysis_runs
    db.commit()
    log_action("creator.brand_deleted", user_id=user.id, brand_id=brand_id)
    return {"ok": True}


# ── Calendar / items / channels / analysis — заглушки (итерации 2+) ──────────

@router.get("/brands/{brand_id}/calendar")
def get_calendar(brand_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Получить активный календарь бренда (или пусто). Полная реализация — итерация 2."""
    b = db.query(CreatorBrand).filter_by(id=brand_id, user_id=user.id).first()
    if not b:
        raise HTTPException(404, "Бренд не найден")
    cal = db.query(ContentCalendar).filter_by(brand_id=brand_id, status="active").order_by(ContentCalendar.id.desc()).first()
    if not cal:
        return {"calendar": None, "items": []}
    items = db.query(ContentItem).filter_by(calendar_id=cal.id).order_by(ContentItem.schedule_at).all()
    return {
        "calendar": {
            "id": cal.id,
            "period_start": cal.period_start.isoformat(),
            "period_end": cal.period_end.isoformat(),
            "status": cal.status,
            "generated_at": cal.generated_at.isoformat() if cal.generated_at else None,
        },
        "items": [_item_dict(i) for i in items],
    }


# ── Calendar generation (Шаг A — бесплатно) ──────────────────────────────────

class CalendarGenerateIn(BaseModel):
    days: Optional[int] = Field(default=PLAN_DEFAULT_DAYS, ge=PLAN_MIN_DAYS, le=PLAN_MAX_DAYS)
    platforms: Optional[list[str]] = None  # tg/vk/yt/ig; пусто = все


CALENDAR_REGEN_COOLDOWN_MIN = 60  # анти-абуз: не чаще раза в час


@router.post("/brands/{brand_id}/calendar/generate")
def generate_calendar(
    brand_id: int,
    payload: CalendarGenerateIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Сгенерировать контент-план на N дней. Бесплатно (входит в freemium).

    Подготовка отдельных постов (Шаг B) — отдельный платный шаг, 3/мес бесплатно.
    """
    b = db.query(CreatorBrand).filter_by(id=brand_id, user_id=user.id).first()
    if not b:
        raise HTTPException(404, "Бренд не найден")

    # Анти-абуз: проверяем cooldown по последнему active календарю
    last = db.query(ContentCalendar).filter_by(brand_id=brand_id).order_by(ContentCalendar.id.desc()).first()
    if last and last.generated_at:
        age = datetime.utcnow() - last.generated_at
        if age < timedelta(minutes=CALENDAR_REGEN_COOLDOWN_MIN):
            wait = int((timedelta(minutes=CALENDAR_REGEN_COOLDOWN_MIN) - age).total_seconds() / 60) + 1
            raise HTTPException(429, f"План уже сгенерирован недавно. Попробуйте через ~{wait} мин.")

    platforms = payload.platforms
    if platforms:
        invalid = [p for p in platforms if p not in VALID_PLATFORMS_PLANNER]
        if invalid:
            raise HTTPException(400, f"Недопустимые платформы: {invalid}. Допустимо: {sorted(VALID_PLATFORMS_PLANNER)}")

    try:
        plan = generate_plan(b, days=payload.days or PLAN_DEFAULT_DAYS, platforms=platforms, user_id=user.id)
    except Exception as e:
        log.exception("[creators.generate] brand=%s failed: %s", brand_id, e)
        raise HTTPException(500, f"Не удалось сгенерировать план: {e}")

    # Архивируем все старые active календари бренда
    db.query(ContentCalendar).filter_by(brand_id=brand_id, status="active").update({"status": "archived"})

    cal = ContentCalendar(
        brand_id=brand_id,
        period_start=plan["period_start"],
        period_end=plan["period_end"],
        status="active",
        generated_at=datetime.utcnow(),
    )
    db.add(cal)
    db.flush()

    for item in plan["items"]:
        db.add(ContentItem(
            calendar_id=cal.id,
            schedule_at=item["schedule_at"],
            platform=item["platform"],
            type=item["type"],
            is_news=item["is_news"],
            brief=item["brief"] or None,
            status="planned",
        ))
    db.commit()
    db.refresh(cal)

    log_action(
        "creator.calendar_generated", user_id=user.id, brand_id=brand_id,
        calendar_id=cal.id, items_count=len(plan["items"]),
        days=payload.days, platforms=",".join(platforms or sorted(VALID_PLATFORMS_PLANNER)),
        brief_filled=plan["raw_brief_count"],
    )

    items = db.query(ContentItem).filter_by(calendar_id=cal.id).order_by(ContentItem.schedule_at).all()
    return {
        "calendar": {
            "id": cal.id,
            "period_start": cal.period_start.isoformat(),
            "period_end": cal.period_end.isoformat(),
            "status": cal.status,
            "generated_at": cal.generated_at.isoformat(),
        },
        "items": [_item_dict(i) for i in items],
        "stats": {
            "total": len(items),
            "brief_filled": plan["raw_brief_count"],
            "by_platform": _count_by(items, "platform"),
            "by_type": _count_by(items, "type"),
        },
    }


def _count_by(items: list, attr: str) -> dict:
    out: dict = {}
    for i in items:
        v = getattr(i, attr, None) or "?"
        out[v] = out.get(v, 0) + 1
    return out


# ── Item edit / skip ──────────────────────────────────────────────────────────

class ItemUpdateIn(BaseModel):
    schedule_at: Optional[datetime] = None
    brief: Optional[str] = None
    type: Optional[str] = None
    is_news: Optional[bool] = None
    status: Optional[str] = None  # planned/skipped только


VALID_TYPES = {"text", "image", "reels", "youtube", "poll", "news"}
EDITABLE_STATUSES = {"planned", "skipped"}


@router.put("/items/{item_id}")
def update_item(
    item_id: int,
    payload: ItemUpdateIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Изменить пост в плане. Доступно пока status in {planned, skipped, ready}.

    После published — менять нельзя.
    """
    item = db.query(ContentItem).join(ContentCalendar).join(CreatorBrand).filter(
        ContentItem.id == item_id, CreatorBrand.user_id == user.id,
    ).first()
    if not item:
        raise HTTPException(404, "Пост не найден")
    if item.status == "published":
        raise HTTPException(400, "Опубликованный пост менять нельзя")

    if payload.schedule_at is not None:
        item.schedule_at = payload.schedule_at
    if payload.brief is not None:
        item.brief = (payload.brief.strip() or None)
        item.manual_override = True
    if payload.type is not None:
        if payload.type not in VALID_TYPES:
            raise HTTPException(400, f"type ∉ {sorted(VALID_TYPES)}")
        item.type = payload.type
    if payload.is_news is not None:
        item.is_news = bool(payload.is_news)
    if payload.status is not None:
        if payload.status not in EDITABLE_STATUSES:
            raise HTTPException(400, "status ∈ {planned, skipped}")
        item.status = payload.status

    db.commit()
    db.refresh(item)
    log_action("creator.item_updated", user_id=user.id, item_id=item.id)
    return _item_dict(item)


# ── Item prepare (Шаг B — платно или freemium) ───────────────────────────────

class PrepareIn(BaseModel):
    with_image: Optional[bool] = None
    use_free: Optional[bool] = True  # по умолчанию пытаемся бесплатный, если есть остаток


@router.get("/brands/{brand_id}/freemium")
def get_freemium(brand_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Текущий статус freemium для бренда (used / remaining / limit)."""
    b = db.query(CreatorBrand).filter_by(id=brand_id, user_id=user.id).first()
    if not b:
        raise HTTPException(404, "Бренд не найден")
    return freemium_status(b)


@router.post("/items/{item_id}/prepare")
def prepare_item_endpoint(
    item_id: int,
    payload: PrepareIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Подготовить пост: AI генерирует текст (+ опц. картинку).

    Списание: первые 3 поста бренда в месяц бесплатно, дальше compute_cost_kop().
    """
    item = db.query(ContentItem).join(ContentCalendar).join(CreatorBrand).filter(
        ContentItem.id == item_id, CreatorBrand.user_id == user.id,
    ).first()
    if not item:
        raise HTTPException(404, "Пост не найден")
    if item.status not in ("planned", "ready"):
        # ready — повторная подготовка / regenerate
        raise HTTPException(400, f"Подготовка возможна только для planned/ready (текущий: {item.status})")

    brand = db.query(CreatorBrand).join(ContentCalendar, CreatorBrand.id == ContentCalendar.brand_id).filter(
        ContentCalendar.id == item.calendar_id,
    ).first()
    if not brand:
        raise HTTPException(500, "Бренд для поста не найден")

    cost_kop = compute_cost_kop(item)
    fs = freemium_status(brand)
    use_free = bool(payload.use_free) and fs["remaining"] > 0

    charged_kop = 0
    if not use_free:
        if not deduct_strict(db, user.id, cost_kop):
            raise HTTPException(402, f"Недостаточно средств. Нужно {cost_kop / 100:.2f} ₽")
        charged_kop = cost_kop
        db.add(Transaction(
            user_id=user.id, type="usage", tokens_delta=-cost_kop,
            description=f"Creators · подготовка поста (бренд {brand.id})",
            model="claude-sonnet-4-6" + (" + sonar-pro" if item.is_news else ""),
        ))
    else:
        # Списать 1 freemium-credit
        consume_freemium(brand)

    # Помечаем как preparing
    item.status = "preparing"
    db.commit()

    # Запускаем pipeline (синхронно — для MVP). Если упадёт — refund.
    try:
        result = _prepare_item_pipeline(item, brand, user_id=user.id, with_image=payload.with_image)
    except Exception as e:
        log.exception("[creators.prepare] item=%s failed: %s", item_id, e)
        # Refund
        if charged_kop > 0:
            from server.billing import credit_atomic
            credit_atomic(db, user.id, charged_kop)
            db.add(Transaction(
                user_id=user.id, type="refund", tokens_delta=charged_kop,
                description=f"Creators refund · подготовка #{item_id}",
            ))
        if use_free and brand.free_posts_used_this_month and brand.free_posts_used_this_month > 0:
            brand.free_posts_used_this_month -= 1
        item.status = "planned"
        item.error = str(e)[:500]
        db.commit()
        raise HTTPException(500, f"Не удалось подготовить: {e}")

    item.prepared_content_md = result["text"] or None
    item.prepared_media_url = result["media_url"]
    item.status = "ready"
    item.cost_kop = (item.cost_kop or 0) + charged_kop
    item.error = None
    db.commit()
    db.refresh(item)

    log_action(
        "creator.item_prepared", user_id=user.id, item_id=item.id,
        brand_id=brand.id, cost_kop=charged_kop, freemium=use_free,
        with_image=bool(result["media_url"]),
        models=",".join(result.get("model_chain") or []),
    )

    return {
        "item": _item_dict(item),
        "freemium": freemium_status(brand),
        "charged_kop": charged_kop,
        "was_free": use_free,
    }


@router.delete("/items/{item_id}")
def delete_item(
    item_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Полностью удалить пост из плана (отличается от status=skipped)."""
    item = db.query(ContentItem).join(ContentCalendar).join(CreatorBrand).filter(
        ContentItem.id == item_id, CreatorBrand.user_id == user.id,
    ).first()
    if not item:
        raise HTTPException(404, "Пост не найден")
    if item.status == "published":
        raise HTTPException(400, "Опубликованный пост удалить нельзя")
    db.delete(item)
    db.commit()
    log_action("creator.item_deleted", user_id=user.id, item_id=item_id)
    return {"ok": True}


def _item_dict(i: ContentItem) -> dict:
    return {
        "id": i.id,
        "schedule_at": i.schedule_at.isoformat() if i.schedule_at else None,
        "platform": i.platform,
        "type": i.type,
        "is_news": bool(i.is_news),
        "brief": i.brief,
        "prepared_content_md": i.prepared_content_md,
        "prepared_media_url": i.prepared_media_url,
        "status": i.status,
        "cost_kop": int(i.cost_kop or 0),
        "published_at": i.published_at.isoformat() if i.published_at else None,
        "manual_override": bool(i.manual_override),
    }


# ── Validation ────────────────────────────────────────────────────────────────

def _validate_brand(p: BrandIn) -> None:
    if p.niche and p.niche not in VALID_NICHES:
        raise HTTPException(400, f"Недопустимая ниша. Допустимо: {sorted(VALID_NICHES)}")
    if p.tone and p.tone not in VALID_TONES:
        raise HTTPException(400, f"Недопустимый тон. Допустимо: {sorted(VALID_TONES)}")
    if p.topics and len(p.topics) > MAX_TOPICS:
        raise HTTPException(400, f"Максимум {MAX_TOPICS} тем")
    if p.topics:
        for t in p.topics:
            if not isinstance(t, str) or len(t) > 100:
                raise HTTPException(400, "Тема: строка ≤ 100 символов")
