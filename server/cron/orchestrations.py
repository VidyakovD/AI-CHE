"""Cron для orchestra-инфры: расписания + idempotency cleanup.

Вынесено из server/scheduler.py.

Состав:
    _orchestra_schedules_tick / orchestra_schedules_loop —
        раз в минуту запускает созревшие OrchestraSchedule
        (пользовательские «каждый понедельник в 09:00 SWOT»).
    _idempotency_cleanup_tick / idempotency_cleanup_loop —
        раз в минуту чистит IdempotencyRecord > 5 мин (TTL).
"""
import asyncio
import logging
from datetime import datetime, timedelta

log = logging.getLogger("scheduler")


# ── Orchestra schedules: автоматический запуск пользовательских расписаний ──

async def _orchestra_schedules_tick():
    """Каждую минуту проверяем какие OrchestraSchedule созрели для запуска
    (next_run_at <= now, is_active=True). Запускаем orchestra в фоне +
    обновляем last_run_at + next_run_at."""
    from server.db import db_session
    from server.models import OrchestraSchedule, SolutionRun, Solution, User
    from server.routes.schedules import _calc_next_run
    from server.solutions_orchestra import run_orchestra
    from server.billing import get_balance
    import json as _json
    import uuid as _uuid

    now = datetime.utcnow()
    fired: list[int] = []
    try:
        with db_session() as db:
            due = (db.query(OrchestraSchedule)
                     .filter(OrchestraSchedule.is_active == True,  # noqa: E712
                             OrchestraSchedule.next_run_at <= now,
                             OrchestraSchedule.next_run_at != None)  # noqa: E711
                     .limit(20).all())
            for s in due:
                # Базовая проверка баланса — минимум 200 коп = 2 ₽
                bal = get_balance(db, s.user_id)
                if bal < 200:
                    # Сдвигаем next_run на завтра — не отключаем (юзер может пополнить)
                    s.next_run_at = _calc_next_run(s.frequency, now)
                    log.warning(f"[schedule {s.id}] skip — недостаточно баланса")
                    continue
                # Проверяем что у юзера is_active и solution существует
                user = db.query(User).filter_by(id=s.user_id, is_active=True).first()
                solution = db.query(Solution).filter_by(
                    id=s.solution_id, is_active=True).first()
                if not user or not solution or not solution.orchestra_json:
                    s.is_active = False
                    log.warning(f"[schedule {s.id}] disabled — user/solution gone")
                    continue
                # Создаём SolutionRun (как обычный orchestra-старт)
                attachments = []
                if s.attachments_json:
                    try:
                        attachments = _json.loads(s.attachments_json)
                    except Exception:
                        attachments = []
                run = SolutionRun(
                    user_id=s.user_id, solution_id=s.solution_id,
                    chat_id=f"sched_{s.id}_" + _uuid.uuid4().hex[:8],
                    status="running",
                    user_input=s.user_input,
                    attachments_json=(_json.dumps(attachments, ensure_ascii=False)
                                       if attachments else None),
                    context=_json.dumps({"_scheduled_from": s.id},
                                         ensure_ascii=False),
                )
                db.add(run); db.flush()
                fired.append(run.id)
                # Обновляем расписание
                s.last_run_at = now
                s.last_run_id = run.id
                s.total_runs = (s.total_runs or 0) + 1
                s.next_run_at = _calc_next_run(s.frequency, now)
                # Audit
                try:
                    from server.audit_log import log_action
                    log_action("orchestra_schedule.fired", user_id=s.user_id,
                               target_type="schedule", target_id=str(s.id),
                               details={"run_id": run.id})
                except Exception:
                    pass
            db.commit()
    except Exception as e:
        log.error(f"[schedules] tick error: {type(e).__name__}: {e}")
        return

    # Запускаем orchestra в фоне (после commit)
    for run_id in fired:
        try:
            asyncio.create_task(run_orchestra(run_id))
        except Exception as e:
            log.error(f"[schedules] failed to launch run {run_id}: {e}")


async def orchestra_schedules_loop():
    """Раз в минуту проверяет созревшие расписания."""
    from server.worker_lock import worker_lock
    await asyncio.sleep(60)  # ждём минуту после старта (миграции)
    while True:
        try:
            # ttl_sec > sleep — оркестра может выполнять долгий run
            with worker_lock("orchestra_schedules", ttl_sec=180) as acquired:
                if acquired:
                    await _orchestra_schedules_tick()
        except Exception as e:
            log.error(f"[schedules] loop error: {e}")
        await asyncio.sleep(60)


# ── Idempotency cleanup ──────────────────────────────────────────────────────

async def _idempotency_cleanup_tick():
    """Удаляем idempotency-записи старше 5 минут (TTL).
    Без cleanup таблица будет расти вечно — при 60 RPS это +5M записей/день."""
    from server.db import db_session
    from server.models import IdempotencyRecord
    cutoff = datetime.utcnow() - timedelta(seconds=300)
    try:
        with db_session() as db:
            n = (db.query(IdempotencyRecord)
                   .filter(IdempotencyRecord.created_at < cutoff)
                   .delete(synchronize_session=False))
            db.commit()
            if n > 0:
                log.info(f"[idempotency] cleaned {n} stale records")
    except Exception as e:
        log.error(f"[idempotency] cleanup error: {type(e).__name__}: {e}")


async def idempotency_cleanup_loop():
    """Раз в минуту чистит expired idempotency-записи (5 мин TTL)."""
    from server.worker_lock import worker_lock
    await asyncio.sleep(120)  # 2 мин после старта
    while True:
        try:
            with worker_lock("idempotency_cleanup", ttl_sec=120) as acquired:
                if acquired:
                    await _idempotency_cleanup_tick()
        except Exception as e:
            log.error(f"[idempotency] loop error: {e}")
        await asyncio.sleep(60)
