"""Расписания автоматического запуска orchestra-решений.

Юзер настраивает «каждый понедельник в 09:00 запусти конкурентный анализ моей
ниши» → сохраняем OrchestraSchedule с frequency-пресетом → scheduler.py
worker раз в минуту проверяет `next_run_at <= now` и стартует orchestra.

Frequency пресеты (НЕ полный cron — простота для юзера):
  - daily_09        — каждый день в 09:00 UTC
  - weekly_mon_09   — каждый понедельник в 09:00
  - weekly_fri_18   — каждую пятницу в 18:00
  - monthly_1_09    — 1-го числа в 09:00
  - monthly_15_09   — 15-го числа в 09:00

Часовой пояс: всё в UTC. Юзер на проде в МСК = UTC+3, значит «09:00» = «12:00 МСК».
Если юзер хочет местное — добавим в будущей миграции `timezone` поле.
"""
import json
import logging
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from server.routes.deps import get_db, current_user
from server.models import User, OrchestraSchedule, Solution

log = logging.getLogger(__name__)
router = APIRouter(prefix="/orchestra-schedules", tags=["schedules"])


VALID_FREQUENCIES = {
    "daily_09":        "каждый день, 09:00 UTC (12:00 МСК)",
    "daily_18":        "каждый день, 18:00 UTC (21:00 МСК)",
    "weekly_mon_09":   "каждый понедельник, 09:00 UTC (12:00 МСК)",
    "weekly_mon_18":   "каждый понедельник, 18:00 UTC (21:00 МСК)",
    "weekly_fri_09":   "каждую пятницу, 09:00 UTC",
    "weekly_fri_18":   "каждую пятницу, 18:00 UTC",
    "monthly_1_09":    "1-го числа в 09:00 UTC",
    "monthly_15_09":   "15-го числа в 09:00 UTC",
}

MAX_SCHEDULES_PER_USER = 5


def _calc_next_run(freq: str, after: datetime | None = None) -> datetime | None:
    """Вычислить следующий момент запуска по frequency-пресету.
    Возвращает UTC-DateTime в БУДУЩЕМ относительно `after` (default: now).
    """
    base = (after or datetime.utcnow()) + timedelta(seconds=30)  # минимум 30 сек запас
    if freq.startswith("daily_"):
        try:
            hh = int(freq.split("_")[1])
        except Exception:
            return None
        candidate = base.replace(hour=hh, minute=0, second=0, microsecond=0)
        if candidate <= base:
            candidate += timedelta(days=1)
        return candidate
    if freq.startswith("weekly_"):
        parts = freq.split("_")
        if len(parts) < 3: return None
        day_map = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
        wd = day_map.get(parts[1])
        try:
            hh = int(parts[2])
        except Exception:
            return None
        if wd is None: return None
        candidate = base.replace(hour=hh, minute=0, second=0, microsecond=0)
        # Сдвиг до нужного weekday
        days_ahead = (wd - candidate.weekday()) % 7
        if days_ahead == 0 and candidate <= base:
            days_ahead = 7
        candidate += timedelta(days=days_ahead)
        return candidate
    if freq.startswith("monthly_"):
        parts = freq.split("_")
        if len(parts) < 3: return None
        try:
            day = int(parts[1])
            hh = int(parts[2])
        except Exception:
            return None
        if not (1 <= day <= 28): return None  # 28 — гарантированно есть в любом месяце
        candidate = base.replace(day=day, hour=hh, minute=0, second=0, microsecond=0)
        if candidate <= base:
            # В следующем месяце
            if candidate.month == 12:
                candidate = candidate.replace(year=candidate.year + 1, month=1)
            else:
                candidate = candidate.replace(month=candidate.month + 1)
        return candidate
    return None


def _serialize(s: OrchestraSchedule, sol_title: str | None = None) -> dict:
    return {
        "id": s.id,
        "solution_id": s.solution_id,
        "solution_title": sol_title,
        "name": s.name,
        "user_input": (s.user_input or "")[:300],
        "frequency": s.frequency,
        "frequency_label": VALID_FREQUENCIES.get(s.frequency, s.frequency),
        "is_active": bool(s.is_active),
        "last_run_at": s.last_run_at.isoformat() + "Z" if s.last_run_at else None,
        "last_run_id": s.last_run_id,
        "next_run_at": s.next_run_at.isoformat() + "Z" if s.next_run_at else None,
        "total_runs": s.total_runs or 0,
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }


class ScheduleCreateBody(BaseModel):
    solution_id: int
    user_input: str = Field(..., max_length=20000)
    frequency: str
    name: str | None = Field(None, max_length=100)
    attachments: list[dict] | None = None  # формат orchestra attachments


@router.post("")
def schedule_create(body: ScheduleCreateBody, db: Session = Depends(get_db),
                     user: User = Depends(current_user)):
    """Создать расписание. user_input будет передан как `input` в orchestra
    каждый раз, когда наступит next_run_at."""
    if not user.is_verified:
        raise HTTPException(403, "Подтвердите email")
    if body.frequency not in VALID_FREQUENCIES:
        raise HTTPException(400,
            f"Неизвестная частота. Доступные: {sorted(VALID_FREQUENCIES.keys())}")
    sol = db.query(Solution).filter_by(id=body.solution_id, is_active=True).first()
    if not sol:
        raise HTTPException(404, "Решение не найдено")
    if not sol.orchestra_json:
        raise HTTPException(400, "Расписание доступно только для orchestra-решений")
    cnt = db.query(OrchestraSchedule).filter_by(user_id=user.id, is_active=True).count()
    if cnt >= MAX_SCHEDULES_PER_USER:
        raise HTTPException(409, f"Лимит {MAX_SCHEDULES_PER_USER} активных расписаний")
    user_input = (body.user_input or "").strip()
    if len(user_input) < 10:
        raise HTTPException(400, "Опиши задачу подробнее (минимум 10 символов)")
    next_run = _calc_next_run(body.frequency)
    if not next_run:
        raise HTTPException(400, "Не удалось рассчитать next_run_at")
    s = OrchestraSchedule(
        user_id=user.id,
        solution_id=body.solution_id,
        name=(body.name or sol.title)[:100],
        user_input=user_input,
        attachments_json=(json.dumps(body.attachments, ensure_ascii=False)
                           if body.attachments else None),
        frequency=body.frequency,
        is_active=True,
        next_run_at=next_run,
    )
    db.add(s); db.commit(); db.refresh(s)
    try:
        from server.audit_log import log_action
        log_action("orchestra_schedule.created", user_id=user.id,
                   target_type="schedule", target_id=str(s.id),
                   details={"solution_id": body.solution_id,
                            "frequency": body.frequency,
                            "next_run_at": next_run.isoformat()})
    except Exception:
        pass
    return _serialize(s, sol.title)


@router.get("")
def schedule_list(db: Session = Depends(get_db),
                   user: User = Depends(current_user)):
    rows = (db.query(OrchestraSchedule, Solution.title)
              .outerjoin(Solution, Solution.id == OrchestraSchedule.solution_id)
              .filter(OrchestraSchedule.user_id == user.id)
              .order_by(OrchestraSchedule.id.desc())
              .all())
    return [_serialize(s, t) for s, t in rows]


@router.delete("/{schedule_id}")
def schedule_delete(schedule_id: int, db: Session = Depends(get_db),
                     user: User = Depends(current_user)):
    s = (db.query(OrchestraSchedule)
           .filter_by(id=schedule_id, user_id=user.id).first())
    if not s:
        raise HTTPException(404, "Расписание не найдено")
    db.delete(s); db.commit()
    return {"status": "deleted"}


class _ScheduleToggleBody(BaseModel):
    is_active: bool


@router.put("/{schedule_id}/toggle")
def schedule_toggle(schedule_id: int, body: _ScheduleToggleBody,
                     db: Session = Depends(get_db),
                     user: User = Depends(current_user)):
    """Включить/отключить расписание (без удаления)."""
    s = (db.query(OrchestraSchedule)
           .filter_by(id=schedule_id, user_id=user.id).first())
    if not s:
        raise HTTPException(404, "Расписание не найдено")
    s.is_active = bool(body.is_active)
    if s.is_active and (s.next_run_at is None or s.next_run_at < datetime.utcnow()):
        s.next_run_at = _calc_next_run(s.frequency)
    db.commit()
    return {"status": "toggled", "is_active": s.is_active,
            "next_run_at": s.next_run_at.isoformat() + "Z" if s.next_run_at else None}
