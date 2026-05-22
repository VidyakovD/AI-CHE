"""Cron: обновление метрик published-постов Креаторов раз в 6 часов.

Что делаем:
  - SELECT ContentItem где status='published' AND published_at > now-30 дней
  - Для каждого — fetch_item_stats (VK API или TG preview-парсинг)
  - UPDATE stats_views/likes/comments/shares + stats_fetched_at

Лимит 30 дней — для свежих постов рост views ещё идёт, дальше плато.
Старые посты раз в день — не имеет смысла, экономим API-rate.

Worker-lock + batch 50 за тик. Между fetches 200мс задержки (защита от
VK API rate-limit ~3 req/sec, TG preview rate тоже не люблю частых).
"""
import asyncio
import logging
from datetime import datetime, timedelta

log = logging.getLogger("scheduler")


METRICS_TICK_INTERVAL = 6 * 3600  # раз в 6 часов
BATCH_SIZE = 50                   # макс постов за тик
FRESH_DAYS = 30                   # обновляем только младше N дней
INTER_REQUEST_DELAY = 0.2         # секунды между fetch'ами


async def _creators_metrics_tick():
    """Один проход: найти published-посты → fetch metrics → save."""
    from server.db import db_session
    from server.models import (ContentItem, ContentCalendar, CreatorBrand,
                                CreatorChannelConnection)
    from server.creators_metrics import fetch_item_stats

    now = datetime.utcnow()
    cutoff = now - timedelta(days=FRESH_DAYS)
    updated = 0
    failed = 0

    try:
        with db_session() as db:
            # Берём посты которые либо никогда не fetcheilись, либо давно
            # (sort by stats_fetched_at NULLS FIRST). Это значит свежепубликованные
            # без stats — в начале очереди.
            q = (db.query(ContentItem)
                   .filter(ContentItem.status == "published",
                           ContentItem.published_at.isnot(None),
                           ContentItem.published_at >= cutoff,
                           ContentItem.external_post_id.isnot(None))
                   .order_by(
                       # NULLS FIRST симулируем через is_(None)
                       ContentItem.stats_fetched_at.asc().nullsfirst()
                       if hasattr(ContentItem.stats_fetched_at.asc(), 'nullsfirst')
                       else ContentItem.stats_fetched_at.asc()
                   )
                   .limit(BATCH_SIZE))
            items = q.all()
            if not items:
                return

            # Pre-fetch tokens (избегаем N+1 query)
            brand_ids = list({
                db.query(ContentCalendar.brand_id)
                  .filter_by(id=it.calendar_id).scalar()
                for it in items
            })
            tokens_by_brand: dict[int, dict] = {}
            for bid in brand_ids:
                if bid is None:
                    continue
                conns = (db.query(CreatorChannelConnection)
                           .filter_by(brand_id=bid, is_active=True)
                           .all())
                tokens_by_brand[bid] = {c.platform: c.token for c in conns}

            for it in items:
                cal = db.query(ContentCalendar).filter_by(id=it.calendar_id).first()
                if not cal:
                    continue
                token = (tokens_by_brand.get(cal.brand_id) or {}).get(it.platform, "")
                # Для TG token не нужен — парсим публичный preview
                if it.platform == "vk" and not token:
                    continue

                try:
                    stats = await fetch_item_stats(it, token)
                except Exception as e:
                    log.warning(f"[metrics.tick] item={it.id} {it.platform} failed: {e}")
                    failed += 1
                    continue

                if stats is None:
                    failed += 1
                    continue

                it.stats_views = int(stats.get("views") or 0)
                it.stats_likes = int(stats.get("likes") or 0)
                it.stats_comments = int(stats.get("comments") or 0)
                it.stats_shares = int(stats.get("shares") or 0)
                it.stats_fetched_at = now
                updated += 1

                # Rate-limit пауза
                await asyncio.sleep(INTER_REQUEST_DELAY)

            db.commit()
            if updated or failed:
                log.info(f"[metrics.tick] updated={updated} failed={failed} "
                         f"batch={len(items)}")
    except Exception as e:
        log.error(f"[metrics.tick] fatal: {type(e).__name__}: {e}")


async def creators_metrics_loop():
    """Раз в 6 часов обновляем метрики свежих published-постов.

    Worker-lock защищает от race condition между 4 prod-воркерами.
    Старт через 7 мин — после других heavy cron'ов (proposals_followup,
    creators_publish).
    """
    from server.worker_lock import worker_lock
    await asyncio.sleep(420)
    while True:
        try:
            with worker_lock("creators_metrics",
                              ttl_sec=METRICS_TICK_INTERVAL - 60) as acquired:
                if acquired:
                    await _creators_metrics_tick()
        except Exception as e:
            log.error(f"[metrics.loop] error: {e}")
        await asyncio.sleep(METRICS_TICK_INTERVAL)
