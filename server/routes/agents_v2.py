"""ИИ Агенты v2 — каталог готовых ролей + история запусков (Iter 2).

Архитектура:
  /agents/roles                    — каталог карточек (6 ролей в финале)
  /agents/roles/{slug}             — детали роли + input_schema
  POST /agents/roles/{slug}/run    — старт: создаём SolutionRun (под капотом)
                                     + AgentRun (тонкая обёртка). Списание
                                     happens в orchestra-runtime по факту.
  /agents/runs/my                  — история запусков юзера
  /agents/runs/{id}                — детали + результат
  /agents/runs/{id}/stream         — SSE прокси на solutions runtime

Реюзаем solutions_orchestra полностью через shadow-Solution: каждая роль
имеет парный Solution с тем же orchestra_json (см. seed_agent_roles.py).
Это даёт бесплатно SSE/биллинг/стадии без копипасты.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from server.routes.deps import get_db, current_user
from server.models import (
    AgentRole, AgentRun, Solution, SolutionRun, User, Transaction,
)
from server.billing import get_balance

log = logging.getLogger(__name__)
router = APIRouter(prefix="/agents", tags=["agents-v2"])


# ── helpers ──────────────────────────────────────────────────────────────────

def _role_dict(role: AgentRole, with_schema: bool = False) -> dict:
    """Карточка роли для каталога."""
    input_schema = None
    input_hint = None
    if with_schema:
        # Берём из shadow-Solution.input_schema_json (там лежит для UI-формы)
        sol = None
        if role.shadow_solution_id:
            from server.db import SessionLocal
            db = SessionLocal()
            try:
                sol = db.query(Solution).filter_by(id=role.shadow_solution_id).first()
                if sol and sol.input_schema_json:
                    try:
                        input_schema = json.loads(sol.input_schema_json)
                    except Exception:
                        pass
                if sol and sol.orchestra_json:
                    try:
                        input_hint = json.loads(sol.orchestra_json).get("input_hint")
                    except Exception:
                        pass
            finally:
                db.close()
    return {
        "id": role.id,
        "slug": role.slug,
        "title": role.title,
        "icon": role.icon or "🤖",
        "description": role.description or "",
        "short_summary": role.short_summary or "",
        "base_price_kop": role.base_price_kop or 0,
        "default_kb_categories": [c.strip() for c in (role.default_kb_categories or "").split(",") if c.strip()],
        "is_active": bool(role.is_active),
        "input_schema": input_schema,
        "input_hint": input_hint,
    }


def _run_dict(run: AgentRun, role: AgentRole | None,
              sr: SolutionRun | None) -> dict:
    """Карточка запуска (для истории)."""
    preview = ""
    if sr and sr.final_output:
        preview = sr.final_output[:240].strip()
        if len(sr.final_output) > 240:
            preview += "…"
    return {
        "id": run.id,
        "role_slug": role.slug if role else None,
        "role_title": role.title if role else "Удалённая роль",
        "role_icon": role.icon if role else "🤖",
        "input_preview": run.input_preview or "",
        "status": (sr.status if sr else "unknown"),
        "total_cost_kop": (sr.total_cost_kop or 0) if sr else 0,
        "preview": preview,
        "solution_run_id": run.solution_run_id,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "has_pdf": bool(sr.pdf_path) if sr else False,
    }


# ── каталог ──────────────────────────────────────────────────────────────────

@router.get("/roles")
def list_roles(db: Session = Depends(get_db),
               user: User = Depends(current_user)):
    """Каталог активных ролей."""
    roles = (db.query(AgentRole)
               .filter_by(is_active=True)
               .order_by(AgentRole.sort_order, AgentRole.id)
               .all())
    return {"roles": [_role_dict(r) for r in roles]}


@router.get("/roles/{slug}")
def get_role(slug: str, db: Session = Depends(get_db),
             user: User = Depends(current_user)):
    """Детали роли + input_schema из shadow-Solution для UI-формы."""
    role = db.query(AgentRole).filter_by(slug=slug, is_active=True).first()
    if not role:
        raise HTTPException(404, "Роль не найдена")
    return _role_dict(role, with_schema=True)


# ── запуск ───────────────────────────────────────────────────────────────────

class RunRolePayload(BaseModel):
    """Запуск роли. input — либо строка (для legacy ролей без input_schema),
    либо JSON-словарь полей (для ролей с input_schema, как Solutions v2)."""
    input: str | dict
    skills: list[str] | None = None  # выбранные скилы (Iter 4)


@router.post("/roles/{slug}/run")
async def run_role(slug: str, payload: RunRolePayload,
                   db: Session = Depends(get_db),
                   user: User = Depends(current_user)):
    """Запустить роль: создаём SolutionRun (под капотом для orchestra) +
    AgentRun (обёртка для UI/истории). Возвращает оба id — фронт стримит
    результат через /agents/runs/{agent_run_id}/stream.
    """
    if not user.is_verified:
        raise HTTPException(403, "Подтвердите email")
    role = db.query(AgentRole).filter_by(slug=slug, is_active=True).first()
    if not role:
        raise HTTPException(404, "Роль не найдена")
    if not role.shadow_solution_id:
        raise HTTPException(500, "Роль не привязана к pipeline (запустите seed_agent_roles.py)")

    sol = db.query(Solution).filter_by(id=role.shadow_solution_id).first()
    if not sol or not sol.orchestra_json:
        raise HTTPException(500, "Pipeline роли не найден")

    # Сериализуем input — orchestra ждёт либо JSON dict (для input_schema),
    # либо обычный текст. Для ролей с schema — клиент шлёт dict, мы JSON-им.
    if isinstance(payload.input, dict):
        user_input = json.dumps(payload.input, ensure_ascii=False)
        preview = " · ".join(f"{k}: {str(v)[:50]}" for k, v in payload.input.items() if v)[:300]
    else:
        user_input = (payload.input or "").strip()
        preview = user_input[:300]
    if len(user_input) < 5:
        raise HTTPException(400, "Опиши задачу подробнее (минимум 5 символов)")
    if len(user_input) > 20_000:
        raise HTTPException(413, "Слишком длинный input (макс 20 КБ)")

    # Pre-check баланса — orchestra списывает по стадиям, минимум 2 ₽ "на пробу"
    if get_balance(db, user.id) < 200:
        raise HTTPException(402, "Недостаточно средств. Минимум для запуска — 2 ₽")

    chat_id = uuid.uuid4().hex[:16]
    sr = SolutionRun(
        user_id=user.id,
        solution_id=sol.id,
        chat_id=chat_id,
        status="running",
        user_input=user_input,
        context=json.dumps({"agent_role_slug": role.slug}, ensure_ascii=False),
    )
    db.add(sr)
    db.flush()  # получаем sr.id без коммита

    ar = AgentRun(
        user_id=user.id,
        role_id=role.id,
        solution_run_id=sr.id,
        skills_json=(json.dumps(payload.skills) if payload.skills else None),
        input_preview=preview,
    )
    db.add(ar)
    db.commit()
    db.refresh(ar)
    db.refresh(sr)

    try:
        from server.audit_log import log_action
        log_action("agent.role_run", user_id=user.id, target_type="agent_role",
                   target_id=role.slug, details={"agent_run_id": ar.id,
                                                  "solution_run_id": sr.id})
    except Exception:
        pass

    # Запуск в фоне через ту же spawn-инфру что и Solutions
    from server.solutions_orchestra import run_orchestra
    from server._async_tasks import spawn
    spawn(run_orchestra(sr.id), name=f"agent_role:{role.slug}:run:{ar.id}")

    return {
        "agent_run_id": ar.id,
        "solution_run_id": sr.id,
        "chat_id": chat_id,
        "status": "running",
    }


# ── история ──────────────────────────────────────────────────────────────────

@router.get("/runs/my")
def my_runs(limit: int = 50, offset: int = 0, status: str = "",
            db: Session = Depends(get_db),
            user: User = Depends(current_user)):
    """История запусков юзера (свои роли)."""
    limit = max(1, min(int(limit or 50), 100))
    offset = max(0, int(offset or 0))
    q = (db.query(AgentRun, AgentRole, SolutionRun)
           .join(AgentRole, AgentRun.role_id == AgentRole.id)
           .outerjoin(SolutionRun, AgentRun.solution_run_id == SolutionRun.id)
           .filter(AgentRun.user_id == user.id))
    if status and status in ("running", "done", "error", "waiting_input"):
        q = q.filter(SolutionRun.status == status)
    total = q.count()
    rows = q.order_by(AgentRun.id.desc()).limit(limit).offset(offset).all()
    items = [_run_dict(ar, role, sr) for ar, role, sr in rows]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/runs/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_db),
            user: User = Depends(current_user)):
    """Детали запуска + результат + stages_state."""
    ar = (db.query(AgentRun)
            .filter_by(id=run_id, user_id=user.id)
            .first())
    if not ar:
        raise HTTPException(404, "Запуск не найден")
    role = db.query(AgentRole).filter_by(id=ar.role_id).first()
    sr = (db.query(SolutionRun).filter_by(id=ar.solution_run_id).first()
          if ar.solution_run_id else None)
    out = _run_dict(ar, role, sr)
    if sr:
        out["final_output"] = sr.final_output
        out["stages_state"] = (json.loads(sr.stages_state) if sr.stages_state else None)
        out["error"] = None  # error лежит в stages_state, отдельного поля нет
        out["pdf_path"] = sr.pdf_path
    return out


@router.get("/runs/{run_id}/stream")
async def stream_run(run_id: int, db: Session = Depends(get_db),
                     user: User = Depends(current_user)):
    """SSE-стрим результата. Прокси на solutions_orchestra: подписываемся
    на тот же канал что и Solutions UI, но валидируем доступ через AgentRun."""
    ar = (db.query(AgentRun)
            .filter_by(id=run_id, user_id=user.id)
            .first())
    if not ar:
        raise HTTPException(404, "Запуск не найден")
    if not ar.solution_run_id:
        raise HTTPException(500, "Запуск не привязан к pipeline")

    sr_id = ar.solution_run_id

    from server.solutions_orchestra import subscribe_run, unsubscribe_run
    import asyncio as _asyncio

    async def event_gen():
        # Сразу шлём актуальный snapshot из БД (на случай если уже завершено)
        from server.db import SessionLocal
        sess = SessionLocal()
        try:
            sr = sess.query(SolutionRun).filter_by(id=sr_id).first()
            if sr:
                snapshot = {
                    "status": sr.status,
                    "stages": (json.loads(sr.stages_state) if sr.stages_state else {}),
                    "final_output": sr.final_output or "",
                }
                yield f"data: {json.dumps(snapshot, ensure_ascii=False)}\n\n"
                if sr.status in ("done", "error"):
                    return
        finally:
            sess.close()

        q: _asyncio.Queue = subscribe_run(sr_id)
        try:
            for _ in range(600):  # ~10 минут максимум
                try:
                    msg = await _asyncio.wait_for(q.get(), timeout=1.0)
                    yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                    if isinstance(msg, dict) and msg.get("status") in ("done", "error"):
                        return
                except _asyncio.TimeoutError:
                    # heartbeat — держим соединение живым
                    yield ": ping\n\n"
        finally:
            unsubscribe_run(sr_id, q)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
