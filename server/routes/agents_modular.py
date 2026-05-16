"""Модульные ИИ Агенты (раздел 23) — CRUD + диалоговый интерфейс.

См. docs/modules/23-agents-modular-roadmap.md.

Endpoints:
  GET    /api/agents                       — список агентов юзера
  POST   /api/agents                       — создать draft (имя + способ управления)
  GET    /api/agents/{id}                  — детали + spec
  PATCH  /api/agents/{id}                  — обновить (name/spec/status)
  DELETE /api/agents/{id}                  — soft-delete (status=archived)
  GET    /api/agents/{id}/messages         — история диалога
  POST   /api/agents/{id}/messages         — отправить сообщение → ответ агента
                                             (в этом коммите — stub; реальный
                                             Agent Builder/Runtime — следующий шаг)

UI: views/agents-modular.html (отдельная страница).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from server.routes.deps import get_db, current_user
from server.models import User, Agent, AgentMessage

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agents", tags=["agents-modular"])


# ── helpers ──────────────────────────────────────────────────────────────────

def _safe_spec(spec_json: str | None) -> dict:
    if not spec_json:
        return {}
    try:
        return json.loads(spec_json)
    except Exception:
        return {}


def _agent_dict(a: Agent, *, include_spec: bool = False,
                msg_count: int | None = None) -> dict:
    """Карточка агента для списка / детального GET."""
    spec = _safe_spec(a.spec_json)
    out = {
        "id": a.id,
        "name": a.name,
        "icon": a.icon or "🤖",
        "status": a.status or "draft",
        "modules": spec.get("modules", []) if isinstance(spec, dict) else [],
        "schedule": spec.get("schedule") if isinstance(spec, dict) else None,
        "triggers": spec.get("triggers", []) if isinstance(spec, dict) else [],
        "goals": spec.get("goals", "") if isinstance(spec, dict) else "",
        "channels": spec.get("channels", []) if isinstance(spec, dict) else [],
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
        "last_activity_at": a.last_activity_at.isoformat() if a.last_activity_at else None,
    }
    if include_spec:
        out["spec"] = spec
    if msg_count is not None:
        out["message_count"] = msg_count
    return out


def _msg_dict(m: AgentMessage) -> dict:
    meta = {}
    if m.meta_json:
        try:
            meta = json.loads(m.meta_json)
        except Exception:
            meta = {}
    return {
        "id": m.id,
        "role": m.role,
        "content": m.content or "",
        "meta": meta,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


def _get_owned_agent(agent_id: int, user: User, db: Session) -> Agent:
    a = db.query(Agent).filter_by(id=agent_id, user_id=user.id).first()
    if not a:
        raise HTTPException(404, "Агент не найден")
    if a.status == "archived":
        raise HTTPException(410, "Агент удалён")
    return a


# ── CRUD ──────────────────────────────────────────────────────────────────────

VALID_CONTROL_MODES = {"chat", "schedule", "triggers", "hybrid"}


class CreateAgentPayload(BaseModel):
    """Минимум для создания draft-агента: имя + способ управления.
    Дальше всё спецификация набирается через диалог с Builder."""
    name: str = "Новый агент"
    icon: str | None = None
    control_mode: str = "chat"     # chat|schedule|triggers|hybrid
    goals: str | None = None        # опц. первичная цель если юзер сразу указал


@router.get("")
def list_agents(db: Session = Depends(get_db),
                user: User = Depends(current_user)):
    """Список агентов юзера (без archived). Со счётчиком сообщений для UI."""
    agents = (db.query(Agent)
                .filter(Agent.user_id == user.id, Agent.status != "archived")
                .order_by(Agent.updated_at.desc())
                .all())
    if not agents:
        return {"agents": []}
    # Один запрос для счётчиков (избегаем N+1)
    from sqlalchemy import func
    counts = dict(
        db.query(AgentMessage.agent_id, func.count(AgentMessage.id))
          .filter(AgentMessage.agent_id.in_([a.id for a in agents]))
          .group_by(AgentMessage.agent_id)
          .all()
    )
    return {"agents": [_agent_dict(a, msg_count=counts.get(a.id, 0)) for a in agents]}


@router.post("")
def create_agent(payload: CreateAgentPayload,
                 db: Session = Depends(get_db),
                 user: User = Depends(current_user)):
    """Создать draft-агента. Spec пустая — заполняется через Agent Builder."""
    if not user.is_verified:
        raise HTTPException(403, "Подтвердите email")
    name = (payload.name or "Новый агент").strip()[:80]
    icon = (payload.icon or "🤖").strip()[:4]
    control_mode = payload.control_mode if payload.control_mode in VALID_CONTROL_MODES else "chat"

    spec = {
        "modules": [],
        "schedule": None,
        "triggers": [],
        "goals": (payload.goals or "").strip()[:1000],
        "channels": ["web"],
        "control_mode": control_mode,
        "system_prompt_addon": "",
        "module_configs": {},
    }
    a = Agent(
        user_id=user.id,
        name=name,
        icon=icon,
        spec_json=json.dumps(spec, ensure_ascii=False),
        status="draft",
    )
    db.add(a); db.commit(); db.refresh(a)

    # Засеять первое системное сообщение от Builder — приветствие
    greeting = (
        "Привет! Я помогу настроить нового агента. "
        "Что хочешь чтобы он делал? Опиши простыми словами — например "
        "«отвечать клиентам в Telegram», «писать пост в ВК каждый день в 9 утра», "
        "«анализировать мою почту и отвечать на типовые письма»."
    )
    if (payload.goals or "").strip():
        greeting = (
            f"Понял задачу: «{payload.goals.strip()[:200]}». "
            "Сейчас уточню пару деталей чтобы правильно подобрать модули и расписание."
        )
    db.add(AgentMessage(
        agent_id=a.id, role="assistant", content=greeting,
        meta_json=json.dumps({"mode": "build"}, ensure_ascii=False),
    ))
    db.commit()

    try:
        from server.audit_log import log_action
        log_action("agent.create", user_id=user.id, target_type="agent",
                   target_id=a.id, details={"name": name, "mode": control_mode})
    except Exception:
        pass

    return _agent_dict(a, include_spec=True, msg_count=1)


@router.get("/{agent_id}")
def get_agent(agent_id: int, db: Session = Depends(get_db),
              user: User = Depends(current_user)):
    a = _get_owned_agent(agent_id, user, db)
    from sqlalchemy import func
    count = (db.query(func.count(AgentMessage.id))
               .filter_by(agent_id=a.id).scalar() or 0)
    return _agent_dict(a, include_spec=True, msg_count=int(count))


class PatchAgentPayload(BaseModel):
    name: str | None = None
    icon: str | None = None
    spec: dict | None = None       # полный override spec (для UI редактора)
    status: str | None = None      # draft|active|paused


@router.patch("/{agent_id}")
def patch_agent(agent_id: int, payload: PatchAgentPayload,
                db: Session = Depends(get_db),
                user: User = Depends(current_user)):
    a = _get_owned_agent(agent_id, user, db)
    if payload.name is not None:
        a.name = payload.name.strip()[:80] or a.name
    if payload.icon is not None:
        a.icon = payload.icon.strip()[:4] or a.icon
    if payload.spec is not None and isinstance(payload.spec, dict):
        a.spec_json = json.dumps(payload.spec, ensure_ascii=False)
    if payload.status in ("draft", "active", "paused"):
        a.status = payload.status
    a.updated_at = datetime.utcnow()
    db.commit(); db.refresh(a)
    return _agent_dict(a, include_spec=True)


@router.delete("/{agent_id}")
def delete_agent(agent_id: int, db: Session = Depends(get_db),
                 user: User = Depends(current_user)):
    """Soft-delete: status=archived. История диалога сохраняется."""
    a = _get_owned_agent(agent_id, user, db)
    a.status = "archived"
    a.updated_at = datetime.utcnow()
    db.commit()
    try:
        from server.audit_log import log_action
        log_action("agent.archive", user_id=user.id, target_type="agent", target_id=a.id)
    except Exception:
        pass
    return {"status": "archived"}


# ── messages ──────────────────────────────────────────────────────────────────

@router.get("/{agent_id}/messages")
def list_messages(agent_id: int, limit: int = 200,
                  db: Session = Depends(get_db),
                  user: User = Depends(current_user)):
    a = _get_owned_agent(agent_id, user, db)
    limit = max(1, min(int(limit or 200), 1000))
    msgs = (db.query(AgentMessage)
              .filter_by(agent_id=a.id)
              .order_by(AgentMessage.id.asc())
              .limit(limit)
              .all())
    return {"messages": [_msg_dict(m) for m in msgs]}


class SendMessagePayload(BaseModel):
    content: str


@router.post("/{agent_id}/messages")
def send_message(agent_id: int, payload: SendMessagePayload,
                 db: Session = Depends(get_db),
                 user: User = Depends(current_user)):
    """Отправить сообщение агенту.

    Для draft-агентов → Agent Builder (LLM подбирает модули/расписание/триггеры,
    применяет к spec, ведёт диалог до готовности).
    Для active-агентов → пока stub команд (Runtime подключим в следующем шаге).
    """
    a = _get_owned_agent(agent_id, user, db)
    text = (payload.content or "").strip()
    if not text:
        raise HTTPException(400, "Пустое сообщение")
    if len(text) > 10000:
        raise HTTPException(413, "Слишком длинное сообщение (макс 10 КБ)")

    mode = "build" if a.status == "draft" else "command"

    # 1. Сохраняем сообщение юзера
    user_msg = AgentMessage(
        agent_id=a.id, role="user", content=text,
        meta_json=json.dumps({"mode": mode}, ensure_ascii=False),
    )
    db.add(user_msg)
    db.flush()  # чтобы user_msg.id появился

    asst_meta: dict = {"mode": mode}

    if mode == "build":
        # ── Agent Builder ──
        # Готовим историю (без только что добавленного user_msg — Builder его добавит)
        history = (db.query(AgentMessage)
                     .filter(AgentMessage.agent_id == a.id,
                             AgentMessage.id < user_msg.id,
                             AgentMessage.role.in_(("user", "assistant")))
                     .order_by(AgentMessage.id.asc())
                     .all())
        history_dicts = [{"role": m.role, "content": m.content or ""} for m in history]
        spec = _safe_spec(a.spec_json)
        control_mode = spec.get("control_mode", "chat")

        try:
            from server.agent_builder import build_reply
            result = build_reply(
                agent_name=a.name,
                control_mode=control_mode,
                spec=spec,            # мутируется in-place
                history=history_dicts,
                user_input=text,
            )
        except Exception as e:
            log.exception(f"[agent.builder] failed for agent {a.id}: {e}")
            result = {
                "reply": "Что-то пошло не так при обработке 😔 Попробуй ещё раз.",
                "applied": [], "errors": [str(e)[:200]], "ready_to_activate": False, "raw": "",
            }

        reply = result["reply"]
        # Сохраняем обновлённый spec
        a.spec_json = json.dumps(spec, ensure_ascii=False)

        # Активация по запросу Builder'а (модель сама поняла что готово)
        if result.get("ready_to_activate"):
            # Минимальная проверка: должна быть хоть какая-то цель ИЛИ модули
            if (spec.get("goals") or "").strip() or spec.get("modules"):
                a.status = "active"
                asst_meta["activated"] = True

        asst_meta["applied"] = result.get("applied", [])
        if result.get("errors"):
            asst_meta["errors"] = result["errors"]
    else:
        # ── Active-агент: пока stub команд ──
        reply = (
            "✅ Команда принята. Реальное выполнение задач (расписания, "
            "триггеры, запуск модулей) подключу в следующем обновлении. "
            "Пока сообщение записано в историю."
        )
        asst_meta["stub"] = True

    asst_msg = AgentMessage(
        agent_id=a.id, role="assistant", content=reply,
        meta_json=json.dumps(asst_meta, ensure_ascii=False),
    )
    db.add(asst_msg)

    a.last_activity_at = datetime.utcnow()
    a.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(user_msg); db.refresh(asst_msg); db.refresh(a)

    return {
        "user_message": _msg_dict(user_msg),
        "assistant_message": _msg_dict(asst_msg),
        # Возвращаем актуальный snapshot агента — UI обновит sidebar/статус без re-fetch
        "agent": _agent_dict(a, include_spec=True),
    }
