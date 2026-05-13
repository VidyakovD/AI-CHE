"""Креаторы — рабочая зона контент-планирования для бизнеса/SMM.

См. docs/modules/21-creators-roadmap.md.

В этой итерации (MVP-1):
- CRUD `CreatorBrand` (профиль бренда — мульти на юзера)
- Заглушки для calendar/items/channels/analysis (вернутся в следующих итерациях)
"""
import json
import logging
from datetime import datetime
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
