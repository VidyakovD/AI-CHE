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
    refund_freemium,
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
    log_action("creator.brand_created", user_id=user.id,
               target_type="brand", target_id=str(b.id),
               details={"name": b.name})
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
    log_action("creator.brand_updated", user_id=user.id,
               target_type="brand", target_id=str(b.id))
    return _brand_dict(b)


@router.delete("/brands/{brand_id}")
def delete_brand(brand_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    b = db.query(CreatorBrand).filter_by(id=brand_id, user_id=user.id).first()
    if not b:
        raise HTTPException(404, "Бренд не найден")
    db.delete(b)  # cascade удалит календари / items / channels / analysis_runs
    db.commit()
    log_action("creator.brand_deleted", user_id=user.id,
               target_type="brand", target_id=str(brand_id))
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
        "creator.calendar_generated", user_id=user.id,
        target_type="calendar", target_id=str(cal.id),
        details={
            "brand_id": brand_id,
            "items_count": len(plan["items"]),
            "days": payload.days,
            "platforms": ",".join(platforms or sorted(VALID_PLATFORMS_PLANNER)),
            "brief_filled": plan["raw_brief_count"],
        },
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
    log_action("creator.item_updated", user_id=user.id,
               target_type="content_item", target_id=str(item.id))
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
    if use_free:
        # Атомарное списание freemium — может вернуть False если конкурентный
        # запрос только что съел последний кредит. В этом случае fallback на платно.
        use_free = consume_freemium(db, brand.id)
    if not use_free:
        if not deduct_strict(db, user.id, cost_kop):
            raise HTTPException(402, f"Недостаточно средств. Нужно {cost_kop / 100:.2f} ₽")
        charged_kop = cost_kop
        db.add(Transaction(
            user_id=user.id, type="usage", tokens_delta=-cost_kop,
            description=f"Creators · подготовка поста (бренд {brand.id})",
            model="claude-sonnet-4-6" + (" + sonar-pro" if item.is_news else ""),
        ))

    # Помечаем как preparing
    item.status = "preparing"
    db.commit()

    # Запускаем pipeline (синхронно — для MVP). Если упадёт — refund.
    try:
        result = _prepare_item_pipeline(item, brand, user_id=user.id,
                                        with_image=payload.with_image, db=db)
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
        if use_free:
            refund_freemium(db, brand.id)
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
        "creator.item_prepared", user_id=user.id,
        target_type="content_item", target_id=str(item.id),
        details={
            "brand_id": brand.id,
            "cost_kop": charged_kop,
            "freemium": use_free,
            "with_image": bool(result["media_url"]),
            "models": ",".join(result.get("model_chain") or []),
        },
    )

    return {
        "item": _item_dict(item),
        "freemium": freemium_status(brand),
        "charged_kop": charged_kop,
        "was_free": use_free,
    }


class RescheduleIn(BaseModel):
    schedule_at: str  # ISO-8601 datetime в UTC


@router.patch("/items/{item_id}/reschedule")
def reschedule_item(
    item_id: int,
    payload: RescheduleIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Сдвинуть пост на другую дату (drag-n-drop в календарном UI).

    Бесплатно — это только обновление schedule_at, никакой LLM-генерации.
    Запрещаем:
      - перенос published-постов (уже опубликован, история неизменна)
      - дату в прошлом (нельзя «опубликовать вчера» — для тех кто хочет
        backfill истории есть отдельный flow ручного отмечания)
      - чужие посты (брат принадлежит юзеру через CreatorBrand.user_id)
    """
    item = (db.query(ContentItem)
              .join(ContentCalendar, ContentItem.calendar_id == ContentCalendar.id)
              .join(CreatorBrand, ContentCalendar.brand_id == CreatorBrand.id)
              .filter(ContentItem.id == item_id,
                      CreatorBrand.user_id == user.id)
              .first())
    if not item:
        raise HTTPException(404, "Пост не найден")
    if item.status == "published":
        raise HTTPException(400, "Опубликованный пост перенести нельзя")

    try:
        from datetime import timezone as _tz
        new_dt = datetime.fromisoformat(payload.schedule_at.replace("Z", "+00:00"))
        # Приводим к naive UTC (как и schedule_at в БД)
        if new_dt.tzinfo is not None:
            new_dt = new_dt.astimezone(_tz.utc).replace(tzinfo=None)
    except (ValueError, AttributeError):
        raise HTTPException(400, "Неверный формат даты — нужен ISO-8601 UTC")

    # Защита от переноса в прошлое (allow 10 мин past — UI clock skew)
    now = datetime.utcnow()
    if new_dt < now - timedelta(minutes=10):
        raise HTTPException(400, "Нельзя перенести пост в прошлое")

    old_dt = item.schedule_at
    item.schedule_at = new_dt
    db.commit()
    db.refresh(item)

    log_action(
        "creator.item_rescheduled", user_id=user.id,
        target_type="content_item", target_id=item.id,
        details={
            "old_schedule_at": old_dt.isoformat() if old_dt else None,
            "new_schedule_at": new_dt.isoformat(),
        },
    )

    return {"ok": True, "item": _item_dict(item)}


@router.post("/brands/{brand_id}/bulk-prepare")
def bulk_prepare_brand(
    brand_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Подготовить ВСЕ planned-посты бренда одной командой.

    Сценарий: юзер сгенерил контент-план (30-50 постов на месяц), теперь
    хочет нажать одну кнопку и получить ready posts со всем сгенерированным
    контентом. Ручной prepare каждого поста — занудно.

    Логика:
      - Берём ContentItem где status='planned' для этого бренда
      - Лимит 10 за раз (защита от 30-минутной блокировки воркера + от
        случайного списания тысячи рублей одним кликом). Если planned > 10
        — юзер нажмёт снова, остальные подготовим за следующий вызов.
      - Сначала пытаемся за freemium-credits, потом за деньги. Pre-check
        общего баланса ДО старта.
      - Если конкретный пост упал — refund его charge, остальные продолжают.
      - Не запускаем concurrent (синхронный цикл) — _prepare_item_pipeline
        дёргает LLM, не хотим N параллельных запросов к Anthropic.

    Returns: {total_planned, prepared, failed_count, total_charged_kop,
              total_free_used, errors: [{item_id, error}, ...]}
    """
    brand = db.query(CreatorBrand).filter_by(id=brand_id, user_id=user.id).first()
    if not brand:
        raise HTTPException(404, "Бренд не найден")

    # Все planned-посты бренда (через calendar)
    items_q = (db.query(ContentItem)
                 .join(ContentCalendar, ContentItem.calendar_id == ContentCalendar.id)
                 .filter(ContentCalendar.brand_id == brand_id,
                         ContentItem.status == "planned")
                 .order_by(ContentItem.schedule_at.asc()))
    total_planned = items_q.count()
    if total_planned == 0:
        return {"total_planned": 0, "prepared": 0, "failed_count": 0,
                "total_charged_kop": 0, "total_free_used": 0, "errors": [],
                "message": "Нет planned-постов для подготовки"}

    BULK_LIMIT = 10
    items = items_q.limit(BULK_LIMIT).all()

    # Pre-check бюджета: посчитаем максимальную стоимость (без freemium-скидки)
    # — лучше отказать сейчас, чем потратить половину и упереться в баланс.
    fs = freemium_status(brand)
    free_remaining = fs["remaining"]
    max_paid_count = max(0, len(items) - free_remaining)
    max_cost_kop = sum(compute_cost_kop(it) for it in items[free_remaining:]) if max_paid_count > 0 else 0
    if max_cost_kop > 0 and int(user.tokens_balance or 0) < max_cost_kop:
        raise HTTPException(402,
            f"Может потребоваться до {max_cost_kop / 100:.2f} ₽ за {max_paid_count} "
            f"платных постов (после {free_remaining} бесплатных). Пополни баланс "
            "или подготовь посты по одному.")

    prepared = 0
    failed_count = 0
    total_charged_kop = 0
    total_free_used = 0
    errors: list[dict] = []

    for item in items:
        cost_kop = compute_cost_kop(item)
        # Freemium first
        used_free = consume_freemium(db, brand.id) if free_remaining > 0 else False
        charged_kop = 0
        if not used_free:
            if not deduct_strict(db, user.id, cost_kop):
                # Баланс закончился (могла произойти конкурентная трата) — стоп
                errors.append({"item_id": item.id, "error": "Недостаточно средств"})
                failed_count += 1
                continue
            charged_kop = cost_kop
            db.add(Transaction(
                user_id=user.id, type="usage", tokens_delta=-cost_kop,
                description=f"Creators · bulk-prepare (бренд {brand.id})",
                model="claude-sonnet-4-6" + (" + sonar-pro" if item.is_news else ""),
            ))
        else:
            total_free_used += 1
            free_remaining -= 1

        item.status = "preparing"
        db.commit()

        try:
            # with_image=False для bulk — слишком дорого/долго на 10 постов.
            # Юзер может перегенерировать с картинкой отдельно для нужных.
            result = _prepare_item_pipeline(item, brand, user_id=user.id,
                                            with_image=False, db=db)
            item.prepared_content_md = result["text"] or None
            item.prepared_media_url = result["media_url"]
            item.status = "ready"
            item.cost_kop = (item.cost_kop or 0) + charged_kop
            item.error = None
            db.commit()
            db.refresh(item)
            prepared += 1
            total_charged_kop += charged_kop
            log_action(
                "creator.item_prepared_bulk", user_id=user.id,
                target_type="content_item", target_id=str(item.id),
                details={
                    "brand_id": brand.id,
                    "cost_kop": charged_kop,
                    "freemium": used_free,
                },
            )
        except Exception as e:
            log.exception("[creators.bulk-prepare] item=%s failed: %s", item.id, e)
            # Refund — деньги и freemium
            if charged_kop > 0:
                from server.billing import credit_atomic
                credit_atomic(db, user.id, charged_kop)
                db.add(Transaction(
                    user_id=user.id, type="refund", tokens_delta=charged_kop,
                    description=f"Creators refund · bulk-prepare #{item.id}",
                ))
            if used_free:
                refund_freemium(db, brand.id)
                free_remaining += 1
                total_free_used -= 1
            item.status = "planned"
            item.error = str(e)[:500]
            db.commit()
            errors.append({"item_id": item.id, "error": str(e)[:200]})
            failed_count += 1

    return {
        "total_planned": total_planned,
        "prepared": prepared,
        "failed_count": failed_count,
        "total_charged_kop": total_charged_kop,
        "total_free_used": total_free_used,
        "errors": errors,
        "remaining_planned": max(0, total_planned - len(items)),
        "freemium_after": freemium_status(brand),
    }


@router.post("/items/{item_id}/refresh-metrics")
async def refresh_item_metrics(
    item_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Manual fetch метрик одного поста (юзер нажал «Обновить» в UI).

    Cron делает это раз в 6 часов автоматически. Endpoint — для UX
    «хочу посмотреть прямо сейчас». Без биллинга (трафик копеечный).
    """
    item = (db.query(ContentItem)
              .join(ContentCalendar, ContentItem.calendar_id == ContentCalendar.id)
              .join(CreatorBrand, ContentCalendar.brand_id == CreatorBrand.id)
              .filter(ContentItem.id == item_id,
                      CreatorBrand.user_id == user.id)
              .first())
    if not item:
        raise HTTPException(404, "Пост не найден")
    if item.status != "published":
        raise HTTPException(400, "Метрики доступны только для опубликованных постов")
    if not item.external_post_id:
        raise HTTPException(400, "external_post_id не записан (старый пост без tracking)")

    # Pre-load токен бренда
    cal = db.query(ContentCalendar).filter_by(id=item.calendar_id).first()
    conn = (db.query(CreatorChannelConnection)
              .filter_by(brand_id=cal.brand_id, platform=item.platform, is_active=True)
              .first())
    token = conn.token if conn else None
    if item.platform == "vk" and not token:
        raise HTTPException(400, "Нет активного VK-канала бренда (нужен токен для wall.getById)")

    from server.creators_metrics import fetch_item_stats
    try:
        stats = await fetch_item_stats(item, token or "")
    except Exception as e:
        raise HTTPException(500, f"Не удалось получить метрики: {e!s:.140}")
    if stats is None:
        raise HTTPException(502, "API платформы не вернул данных")

    item.stats_views = int(stats.get("views") or 0)
    item.stats_likes = int(stats.get("likes") or 0)
    item.stats_comments = int(stats.get("comments") or 0)
    item.stats_shares = int(stats.get("shares") or 0)
    item.stats_fetched_at = datetime.utcnow()
    db.commit()
    db.refresh(item)

    return {"ok": True, "item": _item_dict(item)}


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
    log_action("creator.item_deleted", user_id=user.id,
               target_type="content_item", target_id=str(item_id))
    return {"ok": True}


# ── Item publish (manual) ─────────────────────────────────────────────────────

@router.post("/items/{item_id}/publish")
async def publish_item_endpoint(
    item_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Опубликовать сейчас (вручную). Требует status=ready и подключённый канал."""
    from server.creators_publisher import publish_item as _publish

    item = db.query(ContentItem).join(ContentCalendar).join(CreatorBrand).filter(
        ContentItem.id == item_id, CreatorBrand.user_id == user.id,
    ).first()
    if not item:
        raise HTTPException(404, "Пост не найден")
    result = await _publish(db, item)
    log_action("creator.item_published_manual", user_id=user.id,
               target_type="content_item", target_id=str(item.id),
               details={"ok": bool(result.get("ok"))})
    if not result.get("ok"):
        raise HTTPException(400, result.get("description") or "Не удалось опубликовать")
    db.refresh(item)
    return {"item": _item_dict(item), "result": result}


# ── Channel connections (TG в MVP, VK/YT/IG позже) ────────────────────────────

class ChannelIn(BaseModel):
    platform: str  # tg / vk / yt / ig
    channel_id: str = Field(..., min_length=1, max_length=200)
    title: Optional[str] = None
    token: str = Field(..., min_length=1, max_length=2048)


def _channel_dict(c: CreatorChannelConnection) -> dict:
    return {
        "id": c.id,
        "platform": c.platform,
        "channel_id": c.channel_id,
        "title": c.title,
        "is_active": bool(c.is_active),
        "fail_count": int(c.fail_count or 0),
        "last_error_at": c.last_error_at.isoformat() if c.last_error_at else None,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


SUPPORTED_AUTO_PUBLISH = {"tg", "vk"}  # YT/IG — позже


@router.get("/brands/{brand_id}/channels")
def list_channels(brand_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    b = db.query(CreatorBrand).filter_by(id=brand_id, user_id=user.id).first()
    if not b:
        raise HTTPException(404, "Бренд не найден")
    rows = db.query(CreatorChannelConnection).filter_by(brand_id=brand_id).order_by(CreatorChannelConnection.id).all()
    return {"channels": [_channel_dict(c) for c in rows]}


@router.post("/brands/{brand_id}/channels")
async def add_channel(
    brand_id: int,
    payload: ChannelIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Подключить канал. Для TG валидируем bot-token + права (getMe + getChat)."""
    b = db.query(CreatorBrand).filter_by(id=brand_id, user_id=user.id).first()
    if not b:
        raise HTTPException(404, "Бренд не найден")
    if payload.platform not in SUPPORTED_AUTO_PUBLISH:
        raise HTTPException(400, f"Автопостинг для платформы '{payload.platform}' пока недоступен. В MVP: TG и VK.")

    # Уникальность (brand, platform, channel_id) ловится UniqueConstraint
    existing = db.query(CreatorChannelConnection).filter_by(
        brand_id=brand_id, platform=payload.platform, channel_id=payload.channel_id,
    ).first()
    if existing:
        raise HTTPException(409, "Этот канал уже подключён")

    title = payload.title
    if payload.platform == "tg":
        from server.creators_publisher import verify_tg_channel
        v = await verify_tg_channel(payload.token, payload.channel_id)
        if not v.get("ok"):
            raise HTTPException(400, f"Не удалось подключиться: {v.get('description', 'неизвестная ошибка')}")
        title = v.get("title") or title
    elif payload.platform == "vk":
        from server.creators_vk import verify_vk_community
        v = await verify_vk_community(payload.token, payload.channel_id)
        if not v.get("ok"):
            raise HTTPException(400, f"VK: {v.get('description', 'неизвестная ошибка')}")
        title = v.get("title") or title

    c = CreatorChannelConnection(
        brand_id=brand_id,
        platform=payload.platform,
        channel_id=payload.channel_id,
        title=title,
        token=payload.token,
        is_active=True,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    log_action("creator.channel_added", user_id=user.id,
               target_type="channel", target_id=str(c.id),
               details={
                   "brand_id": brand_id,
                   "platform": payload.platform,
                   "external_channel_id": payload.channel_id,
               })
    return _channel_dict(c)


@router.put("/channels/{channel_id}/toggle")
def toggle_channel(channel_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    c = db.query(CreatorChannelConnection).join(CreatorBrand).filter(
        CreatorChannelConnection.id == channel_id, CreatorBrand.user_id == user.id,
    ).first()
    if not c:
        raise HTTPException(404, "Канал не найден")
    c.is_active = not bool(c.is_active)
    if c.is_active:
        c.fail_count = 0  # обнуляем при возврате в строй
    db.commit()
    db.refresh(c)
    return _channel_dict(c)


@router.delete("/channels/{channel_id}")
def delete_channel(channel_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    c = db.query(CreatorChannelConnection).join(CreatorBrand).filter(
        CreatorChannelConnection.id == channel_id, CreatorBrand.user_id == user.id,
    ).first()
    if not c:
        raise HTTPException(404, "Канал не найден")
    db.delete(c)
    db.commit()
    log_action("creator.channel_deleted", user_id=user.id,
               target_type="channel", target_id=str(channel_id))
    return {"ok": True}


# ── Social analysis (Perplexity + Sonnet) ────────────────────────────────────

class AnalyzeIn(BaseModel):
    target_type: str = Field(..., pattern="^(own|competitor)$")
    target_url: str = Field(..., min_length=8, max_length=500)


def _analysis_dict(a: CreatorAnalysisRun) -> dict:
    return {
        "id": a.id,
        "target_type": a.target_type,
        "target_url": a.target_url,
        "platform": a.platform,
        "status": a.status,
        "result_md": a.result_md,
        "cost_kop": int(a.cost_kop or 0),
        "error": a.error,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


@router.post("/brands/{brand_id}/analyze")
def run_brand_analysis(
    brand_id: int,
    payload: AnalyzeIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Анализ соцсети: свой профиль (150 ₽) или конкурент (200 ₽).

    Списываем ДО вызова Perplexity. Если pipeline упадёт — refund.
    """
    from server.creators_analyzer import (
        run_analysis, cost_for, detect_platform, is_valid_url, VALID_TARGET_TYPES,
    )
    from server.billing import credit_atomic

    b = db.query(CreatorBrand).filter_by(id=brand_id, user_id=user.id).first()
    if not b:
        raise HTTPException(404, "Бренд не найден")
    if payload.target_type not in VALID_TARGET_TYPES:
        raise HTTPException(400, f"target_type ∈ {sorted(VALID_TARGET_TYPES)}")
    if not is_valid_url(payload.target_url):
        raise HTTPException(400, "Невалидный URL. Нужен http(s):// и домен")

    price_kop = cost_for(payload.target_type)
    if not deduct_strict(db, user.id, price_kop):
        raise HTTPException(402, f"Недостаточно средств. Нужно {price_kop / 100:.2f} ₽")
    db.add(Transaction(
        user_id=user.id, type="usage", tokens_delta=-price_kop,
        description=f"Creators · анализ ({payload.target_type})",
        model="sonar-reasoning-pro+claude-sonnet-4-6",
    ))

    platform = detect_platform(payload.target_url)
    run = CreatorAnalysisRun(
        brand_id=brand_id,
        target_type=payload.target_type,
        target_url=payload.target_url,
        platform=platform,
        status="running",
        cost_kop=price_kop,
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    try:
        out = run_analysis(b, payload.target_url, payload.target_type, user_id=user.id)
    except Exception as e:
        log.exception("[creators.analyze] run=%s failed: %s", run.id, e)
        # Refund
        credit_atomic(db, user.id, price_kop)
        db.add(Transaction(
            user_id=user.id, type="refund", tokens_delta=price_kop,
            description=f"Creators refund · анализ #{run.id}",
        ))
        run.status = "failed"
        run.error = str(e)[:500]
        run.cost_kop = 0
        db.commit()
        raise HTTPException(500, f"Анализ не удался: {e}")

    run.result_md = out["result_md"]
    run.platform = out["platform"] or platform
    run.status = "done"
    db.commit()
    db.refresh(run)

    log_action("creator.analysis_completed", user_id=user.id,
               target_type="analysis", target_id=str(run.id),
               details={
                   "brand_id": brand_id,
                   "scope_target_type": payload.target_type,
                   "cost_kop": price_kop,
               })
    return _analysis_dict(run)


@router.get("/brands/{brand_id}/analysis")
def list_analysis(brand_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    b = db.query(CreatorBrand).filter_by(id=brand_id, user_id=user.id).first()
    if not b:
        raise HTTPException(404, "Бренд не найден")
    rows = (db.query(CreatorAnalysisRun)
              .filter_by(brand_id=brand_id)
              .order_by(CreatorAnalysisRun.id.desc())
              .limit(50).all())
    return {"runs": [_analysis_dict(r) for r in rows]}


@router.get("/analysis/{run_id}")
def get_analysis(run_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    a = db.query(CreatorAnalysisRun).join(CreatorBrand).filter(
        CreatorAnalysisRun.id == run_id, CreatorBrand.user_id == user.id,
    ).first()
    if not a:
        raise HTTPException(404, "Анализ не найден")
    return _analysis_dict(a)


@router.delete("/analysis/{run_id}")
def delete_analysis(run_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    a = db.query(CreatorAnalysisRun).join(CreatorBrand).filter(
        CreatorAnalysisRun.id == run_id, CreatorBrand.user_id == user.id,
    ).first()
    if not a:
        raise HTTPException(404, "Анализ не найден")
    db.delete(a)
    db.commit()
    return {"ok": True}


@router.post("/channels/{channel_id}/test")
async def test_channel(channel_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Отправить тестовое сообщение в канал (TG или VK)."""
    c = db.query(CreatorChannelConnection).join(CreatorBrand).filter(
        CreatorChannelConnection.id == channel_id, CreatorBrand.user_id == user.id,
    ).first()
    if not c:
        raise HTTPException(404, "Канал не найден")

    test_text = "✅ Тест из AI Студии Че: канал подключён успешно."
    if c.platform == "tg":
        from server.messaging.senders import send_telegram
        r = await send_telegram(c.token, c.channel_id, test_text, parse_mode=None)
    elif c.platform == "vk":
        from server.creators_vk import publish_to_vk_wall
        r = await publish_to_vk_wall(c.token, c.channel_id, test_text)
    else:
        raise HTTPException(400, f"Тест для платформы {c.platform} не поддерживается")

    if not r.get("ok"):
        c.fail_count = (c.fail_count or 0) + 1
        c.last_error_at = datetime.utcnow()
        db.commit()
        raise HTTPException(400, r.get("description") or "Тест провалился")
    c.fail_count = 0
    db.commit()
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
        # Метрики опубликованного поста (cron creators_metrics_loop обновляет)
        "external_post_id": i.external_post_id,
        "stats_views":    int(getattr(i, "stats_views", 0) or 0),
        "stats_likes":    int(getattr(i, "stats_likes", 0) or 0),
        "stats_comments": int(getattr(i, "stats_comments", 0) or 0),
        "stats_shares":   int(getattr(i, "stats_shares", 0) or 0),
        "stats_fetched_at": (i.stats_fetched_at.isoformat()
                             if getattr(i, "stats_fetched_at", None) else None),
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
