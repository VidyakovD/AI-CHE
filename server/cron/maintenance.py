"""Maintenance cron-задачи: cleanup старых данных по retention-политикам.

Вынесено из server/scheduler.py. Все 3 функции — «delete older than N days».

Состав:
    pdf_cleanup        — /uploads/solutions/*.pdf > 30d
    audit_cleanup      — action_logs с 3-эшелонной retention (info 30d,
                          auth/payment/record info 365d, errors 90d)
    conv_cleanup       — BotConversationTurn > 30d
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta

log = logging.getLogger("scheduler")


# ── PDF cleanup: /uploads/solutions/*.pdf > 30d ──────────────────────────────

async def _cleanup_old_pdfs_tick():
    """Удаляет PDF-отчёты бизнес-решений старше 30 дней.
    Без этого /uploads/solutions/ растёт неограниченно — каждый run = новый PDF."""
    import time
    # /server/cron/maintenance.py → корень проекта = два уровня вверх
    base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    folder = os.path.join(base, "uploads", "solutions")
    if not os.path.isdir(folder):
        return
    cutoff = time.time() - 30 * 86400
    removed = 0
    for name in os.listdir(folder):
        if not name.startswith("sol_") or not name.endswith(".pdf"):
            continue
        path = os.path.join(folder, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except Exception:
            pass
    if removed:
        log.info(f"[pdf-cleanup] removed {removed} PDFs older than 30 days")


async def pdf_cleanup_loop():
    """Раз в сутки чистит старые PDF (с lock — не дублируется на multi-worker)."""
    from server.worker_lock import worker_lock
    await asyncio.sleep(600)  # подождать 10 мин после старта
    while True:
        try:
            with worker_lock("pdf_cleanup", ttl_sec=86400 + 300) as acquired:
                if acquired:
                    await _cleanup_old_pdfs_tick()
        except Exception as e:
            log.error(f"[pdf cleanup] error: {e}")
        await asyncio.sleep(86400)


# ── Audit log cleanup: 3-эшелонная retention ─────────────────────────────────

async def _cleanup_old_action_logs_tick():
    """Удаляет аудит-логи в три эшелона retention:
      - обычные info: 30 дней
      - auth.* / payment.* / record.* info: 365 дней (нужны для forensic
        и юридических вопросов: «когда я зарегистрировался?», «когда был платёж?»)
      - error/warn/critical: 90 дней (нужны для разбора инцидентов)
    """
    from server.db import db_session
    from server.models import ActionLog
    now = datetime.utcnow()
    cutoff_info_short = now - timedelta(days=30)
    cutoff_info_long = now - timedelta(days=365)
    cutoff_err = now - timedelta(days=90)
    try:
        with db_session() as db:
            # info обычные (не auth/payment) — 30 дней
            n_info = (db.query(ActionLog)
                      .filter(ActionLog.ts < cutoff_info_short,
                              ActionLog.level == "info")
                      .filter(~ActionLog.action.like("auth.%"))
                      .filter(~ActionLog.action.like("payment.%"))
                      .filter(~ActionLog.action.like("record.%"))
                      .delete(synchronize_session=False))
            # auth/payment/record info — 1 год
            n_long = (db.query(ActionLog)
                      .filter(ActionLog.ts < cutoff_info_long,
                              ActionLog.level == "info")
                      .filter(
                          ActionLog.action.like("auth.%") |
                          ActionLog.action.like("payment.%") |
                          ActionLog.action.like("record.%"))
                      .delete(synchronize_session=False))
            # warn/error/critical — 90 дней
            n_err = (db.query(ActionLog)
                     .filter(ActionLog.ts < cutoff_err,
                             ActionLog.level != "info")
                     .delete(synchronize_session=False))
            db.commit()
            if n_info or n_long or n_err:
                log.info(f"[audit-cleanup] removed info={n_info} long={n_long} non-info={n_err}")
    except Exception as e:
        log.error(f"[audit-cleanup] failed: {e}")


async def audit_cleanup_loop():
    from server.worker_lock import worker_lock
    await asyncio.sleep(1200)  # 20 мин после старта
    while True:
        try:
            with worker_lock("audit_cleanup", ttl_sec=86400 + 300) as acquired:
                if acquired:
                    await _cleanup_old_action_logs_tick()
        except Exception as e:
            log.error(f"[audit-cleanup] tick: {e}")
        await asyncio.sleep(86400)


# ── Conversations cleanup: BotConversationTurn > 30d ─────────────────────────

async def _cleanup_old_conversations_tick():
    """Удаляет тёрны диалогов старше 30 дней — иначе таблица растёт без границ.
    Каждый бот в день может писать сотни сообщений × 100k клиентов = миллионы строк."""
    from server.db import db_session
    from server.models import BotConversationTurn
    cutoff = datetime.utcnow() - timedelta(days=30)
    try:
        with db_session() as db:
            n = (db.query(BotConversationTurn)
                 .filter(BotConversationTurn.created_at < cutoff)
                 .delete(synchronize_session=False))
            db.commit()
            if n:
                log.info(f"[conv-cleanup] removed {n} turns older than 30d")
    except Exception as e:
        log.error(f"[conv-cleanup] failed: {e}")


async def conv_cleanup_loop():
    """Раз в сутки чистит старые тёрны диалогов чат-ботов."""
    from server.worker_lock import worker_lock
    await asyncio.sleep(900)  # 15 мин после старта
    while True:
        try:
            with worker_lock("conv_cleanup", ttl_sec=86400 + 300) as acquired:
                if acquired:
                    await _cleanup_old_conversations_tick()
        except Exception as e:
            log.error(f"[conv-cleanup] tick error: {e}")
        await asyncio.sleep(86400)


# ── LLM Cache cleanup: expired entries раз в сутки ──────────────────────────

async def llm_cache_cleanup_loop():
    """Раз в сутки чистит просроченные записи LLM-кэша (TTL уже прошёл).
    Без этого таблица llm_cache растёт без границ."""
    from server.worker_lock import worker_lock
    await asyncio.sleep(1200)  # 20 мин после старта
    while True:
        try:
            with worker_lock("llm_cache_cleanup", ttl_sec=86400 + 300) as acquired:
                if acquired:
                    from server.llm_cache import cleanup_expired
                    cleanup_expired()
        except Exception as e:
            log.error(f"[llm-cache-cleanup] tick error: {e}")
        await asyncio.sleep(86400)


# ── Site failed-generation monitor: алерт админу если >30% упало за час ──

async def _site_failures_monitor_tick():
    """Каждый час проверяет процент failed site-generations за последний час.
    Если > FAILURE_THRESHOLD_PCT (default 30) и не меньше 5 попыток — алертим
    админу. Защита от «тихого» breakage когда LLM-провайдеры упали — мы
    сами замечаем не дожидаясь жалоб юзеров."""
    from server.db import db_session
    from server.models import SiteProject
    threshold_pct = int(os.getenv("SITE_FAILURE_THRESHOLD_PCT", "30"))
    min_attempts = int(os.getenv("SITE_FAILURE_MIN_ATTEMPTS", "5"))
    cutoff = datetime.utcnow() - timedelta(hours=1)
    try:
        with db_session() as db:
            rows = (db.query(SiteProject)
                      .filter(SiteProject.gen_started_at != None,  # noqa: E711
                              SiteProject.gen_started_at >= cutoff,
                              SiteProject.gen_status.in_(("done", "failed")))
                      .all())
            total = len(rows)
            if total < min_attempts:
                return  # слишком мало данных
            failed = sum(1 for r in rows if r.gen_status == "failed")
            pct = failed * 100 / total
            if pct < threshold_pct:
                return
            # Берём примеры ошибок
            errors = []
            for r in rows:
                if r.gen_status == "failed" and r.gen_error:
                    errors.append(r.gen_error[:140])
                if len(errors) >= 3:
                    break
        # Алерт админу
        try:
            from server.ai import _notify_admin
            msg = (
                f"🚨 Site generations: {failed}/{total} failed за последний час "
                f"({pct:.0f}% > порог {threshold_pct}%).\n"
                f"Примеры ошибок:\n" + "\n".join(f"  • {e}" for e in errors[:3])
            )
            _notify_admin(msg)
            log.warning(f"[site-monitor] {msg}")
        except Exception as e:
            log.error(f"[site-monitor] notify failed: {e}")
    except Exception as e:
        log.error(f"[site-monitor] tick failed: {e}")


async def site_failures_monitor_loop():
    """Раз в час проверяет % failed site-generations + алерт админу."""
    from server.worker_lock import worker_lock
    await asyncio.sleep(1800)  # 30 мин после старта (стабилизация)
    while True:
        try:
            with worker_lock("site_failures_monitor", ttl_sec=3600 + 300) as acquired:
                if acquired:
                    await _site_failures_monitor_tick()
        except Exception as e:
            log.error(f"[site-monitor] loop error: {e}")
        await asyncio.sleep(3600)


# ── Calendar sync: pre-fetch событий раз в 30 мин для активных подключений ──

async def _calendar_sync_tick():
    """Обновляет cached_events_json для всех активных UserCalendarConnection.

    Это даёт быстрый ответ модулю calendar при invoke — он берёт кэш вместо
    live-запроса к Google/Yandex/ICS (экономит ~1-3 сек на каждый чат).

    Группируем по user_id чтобы один проход fetch_all_user_events обслуживал
    все подключения юзера сразу (Google+Yandex+ICS в одном запросе).
    """
    from server.db import db_session
    from server.models import UserCalendarConnection, AgentModule
    import json as _json

    # Берём юзеров с активным calendar-модулем — тех у кого fetch имеет смысл.
    # Иначе мы бы pre-fetch'или для юзеров которые модуль не используют.
    with db_session() as db:
        active_users = (db.query(AgentModule.id, AgentModule.agent_id)
                          .filter(AgentModule.slug == "calendar",
                                  AgentModule.is_enabled.is_(True))
                          .all())
        if not active_users:
            return
        # AgentModule.agent_id → Agent.user_id. Достанем через JOIN
        from server.models import Agent
        user_ids = (db.query(Agent.user_id)
                      .filter(Agent.id.in_([a.agent_id for a in active_users]))
                      .all())
        user_ids = list({uid for (uid,) in user_ids if uid})

    if not user_ids:
        return

    from server.calendar_sync import fetch_all_user_events
    for uid in user_ids[:50]:  # cap 50 юзеров за tick (защита от лавины)
        try:
            with db_session() as db:
                events = await fetch_all_user_events(db, uid, days_ahead=14)
                # Сохраняем в первое подключение юзера (упрощение — для lookup
                # нужно только один источник). Альтернатива: per-provider кэш,
                # но горизонт 14 дней едва ли пересекается с фильтрами.
                conn = (db.query(UserCalendarConnection)
                          .filter_by(user_id=uid, is_active=True)
                          .order_by(UserCalendarConnection.id.asc())
                          .first())
                if conn:
                    serializable = []
                    for e in events[:50]:
                        ev = dict(e)
                        for k in ("start", "end"):
                            if ev.get(k) and hasattr(ev[k], "isoformat"):
                                ev[k] = ev[k].isoformat()
                        serializable.append(ev)
                    conn.cached_events_json = _json.dumps(serializable, ensure_ascii=False)
                    conn.last_synced_at = datetime.utcnow()
                    conn.last_error = None
                    db.commit()
        except Exception as e:
            log.warning(f"[calendar-sync] user={uid} failed: {type(e).__name__}: {e}")
            continue


async def calendar_sync_loop():
    """Раз в 30 минут pre-fetch'ит события календарей юзеров с active calendar модулем."""
    from server.worker_lock import worker_lock
    await asyncio.sleep(900)  # 15 мин после старта
    while True:
        try:
            with worker_lock("calendar_sync", ttl_sec=1800 + 60) as acquired:
                if acquired:
                    await _calendar_sync_tick()
        except Exception as e:
            log.error(f"[calendar-sync] loop error: {e}")
        await asyncio.sleep(1800)  # 30 мин
