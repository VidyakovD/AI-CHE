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
