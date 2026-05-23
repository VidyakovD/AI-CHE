"""Cron-задачи для модуля Креаторы: автоматическая подготовка и публикация.

Вынесено из server/scheduler.py (split на доменные файлы).

Состав:
    _creators_prepare_tick / creators_prepare_loop — Шаг B pipeline
        (раз в 5 мин: planned-items в ближайшие 24h → Sonnet/Perplexity +
        опц. DALL-E → status=ready). Freemium 3/мес → потом 15-30 ₽.
    _creators_publish_tick / creators_publish_loop — Шаг C
        (раз в минуту: ready-items с schedule_at<=now → send в TG/VK).

Worker-lock через server.worker_lock для multi-worker safety.
"""
import asyncio
import logging
from datetime import datetime, timedelta

log = logging.getLogger("scheduler")


async def _creators_prepare_tick():
    """За 24 часа до schedule_at автоматически готовим planned-item'ы.

    Логика биллинга — та же что и при ручном вызове POST /items/{id}/prepare:
    сначала тратим freemium (3 поста/мес), потом списываем cost_kop. Если
    баланс < cost и freemium исчерпан — пост остаётся planned, юзер увидит
    что подготовка не произошла и решит сам.
    """
    from server.db import db_session
    from server.models import (
        ContentItem, ContentCalendar, CreatorBrand, Transaction,
    )
    from server.billing import deduct_strict, credit_atomic
    from server.creators_prepare import (
        prepare_item as _prepare_item_pipeline,
        compute_cost_kop, freemium_status, consume_freemium, refund_freemium,
    )
    from server.audit_log import log_action

    now = datetime.utcnow()
    horizon = now + timedelta(hours=24)
    try:
        with db_session() as db:
            # Берём максимум 5 за тик чтобы не зажиматься в одном воркере
            items = (db.query(ContentItem)
                       .filter(ContentItem.status == "planned",
                               ContentItem.schedule_at >= now,
                               ContentItem.schedule_at <= horizon)
                       .order_by(ContentItem.schedule_at)
                       .limit(5)
                       .all())
            for item in items:
                cal = db.query(ContentCalendar).filter_by(id=item.calendar_id).first()
                if not cal: continue
                brand = db.query(CreatorBrand).filter_by(id=cal.brand_id).first()
                if not brand: continue
                user_id = brand.user_id

                cost_kop = compute_cost_kop(item)
                fs = freemium_status(brand)
                use_free = fs["remaining"] > 0
                charged_kop = 0

                if use_free:
                    use_free = consume_freemium(db, brand.id)
                if not use_free:
                    if not deduct_strict(db, user_id, cost_kop):
                        log.info(f"[creators.prep_auto] item={item.id}: insufficient balance, skip")
                        continue
                    charged_kop = cost_kop
                    db.add(Transaction(
                        user_id=user_id, type="usage", tokens_delta=-cost_kop,
                        description=f"Creators · auto-prep #{item.id} (бренд {brand.id})",
                        model="claude-sonnet-4-6" + (" + sonar-pro" if item.is_news else ""),
                    ))

                item.status = "preparing"
                db.commit()

                try:
                    result = _prepare_item_pipeline(item, brand, user_id=user_id, db=db)
                except Exception as e:
                    log.exception(f"[creators.prep_auto] item={item.id} failed: {e}")
                    if charged_kop > 0:
                        credit_atomic(db, user_id, charged_kop)
                        db.add(Transaction(
                            user_id=user_id, type="refund", tokens_delta=charged_kop,
                            description=f"Creators refund · auto-prep #{item.id}",
                        ))
                    if use_free:
                        refund_freemium(db, brand.id)
                    item.status = "planned"
                    item.error = str(e)[:500]
                    db.commit()
                    continue

                item.prepared_content_md = result["text"] or None
                item.prepared_media_url = result["media_url"]
                item.status = "ready"
                item.cost_kop = (item.cost_kop or 0) + charged_kop
                item.error = None
                db.commit()
                log_action(
                    "creator.item_prepared_auto", user_id=user_id,
                    target_type="content_item", target_id=str(item.id),
                    details={
                        "brand_id": brand.id,
                        "cost_kop": charged_kop,
                        "freemium": use_free,
                    },
                )
                if items:
                    log.info(f"[creators.prep_auto] item={item.id} ready (free={use_free}, charged={charged_kop})")
    except Exception as e:
        log.error(f"[creators.prep_auto] tick error: {type(e).__name__}: {e}")


async def creators_prepare_loop():
    """Раз в 5 минут смотрит planned-items в ближайшие 24h и готовит их.
    Worker-lock защищает от дублей при multi-worker."""
    from server.worker_lock import worker_lock
    await asyncio.sleep(180)  # 3 мин после старта чтобы миграции/seed успели
    while True:
        try:
            with worker_lock("creators_prepare", ttl_sec=280) as acquired:
                if acquired:
                    await _creators_prepare_tick()
        except Exception as e:
            log.error(f"[creators.prep_auto] loop error: {e}")
        await asyncio.sleep(300)  # 5 мин


async def _creators_publish_tick():
    """Публикуем ready-items с schedule_at <= now.

    Только в каналы платформы, поддерживающей автопостинг (MVP — TG/VK).
    Для других платформ (YT/IG) оставляем ready — юзер копирует руками.
    """
    from server.db import db_session
    from server.models import ContentItem
    from server.creators_publisher import publish_item, _platform_supports_auto_publish
    from server.audit_log import log_action

    now = datetime.utcnow()
    try:
        with db_session() as db:
            items = (db.query(ContentItem)
                       .filter(ContentItem.status == "ready",
                               ContentItem.schedule_at <= now)
                       .order_by(ContentItem.schedule_at)
                       .limit(10).all())
            for item in items:
                if not _platform_supports_auto_publish(item.platform):
                    continue  # юзер опубликует сам
                try:
                    res = await publish_item(db, item)
                    log_action(
                        "creator.item_published_auto", user_id=None,
                        target_type="content_item", target_id=str(item.id),
                        details={
                            "ok": bool(res.get("ok")),
                            "description": res.get("description"),
                        },
                    )
                except Exception as e:
                    log.exception(f"[creators.publish] item={item.id} exception: {e}")
    except Exception as e:
        log.error(f"[creators.publish] tick error: {type(e).__name__}: {e}")


async def creators_publish_loop():
    """Раз в минуту публикуем созревшие ready-item'ы. Worker-lock."""
    from server.worker_lock import worker_lock
    await asyncio.sleep(240)  # старт через 4 мин (после prepare_loop)
    while True:
        try:
            with worker_lock("creators_publish", ttl_sec=55) as acquired:
                if acquired:
                    await _creators_publish_tick()
        except Exception as e:
            log.error(f"[creators.publish] loop error: {e}")
        await asyncio.sleep(60)
