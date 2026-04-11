"""Agent routes — AI-агенты с пошаговым выполнением."""
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
import json
import asyncio
import logging

from server.routes.deps import get_db, optional_user
from server.models import User, Transaction
from server.agent_runner import (
    create_task, submit_task, tasks as agent_tasks,
    init_agent_queue, TOOL_SCHEMAS, subscribe_task,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["agent"])


class AgentRunRequest(BaseModel):
    goal: str
    context: dict | None = None  # vk_token, tg_token, etc.


@router.post("/run")
async def agent_run(
    req: AgentRunRequest,
    user=Depends(optional_user),
    db: Session = Depends(get_db),
):
    """Запустить AI-агента. Стоимость: 50 CH."""
    if user:
        if not user.is_verified:
            raise HTTPException(403, "Подтвердите email")
        # Cost: 50 CH per agent task
        db_user = db.query(User).filter_by(id=user.id).first()
        if db_user.tokens_balance < 50:
            raise HTTPException(402, "Недостаточно токенов (нужно минимум 50 CH)")
        db_user.tokens_balance -= 50
        db.add(Transaction(
            user_id=user.id, type="usage", tokens_delta=-50,
            description=f"ИИ Агент: {req.goal[:50]}", model="agent",
        ))
        db.commit()

    ctx = req.context or {}
    if user:
        ctx["user_id"] = user.id

    task_id = create_task(user_id=user.id if user else None, goal=req.goal, context=ctx)
    await submit_task(task_id, req.goal, ctx)
    return {"task_id": task_id, "status": "queued"}


@router.get("/{task_id}/status")
def agent_status(task_id: str):
    """Получить статус задачи агента."""
    t = agent_tasks.get(task_id)
    if not t:
        raise HTTPException(404, "Задача не найдена")
    return {
        "task_id": task_id,
        "status": t["status"],
        "goal": t["goal"],
        "steps": t["steps"],
        "outputs": t.get("outputs", []),
        "result": t.get("result"),
        "created_at": t.get("created_at"),
        "updated_at": t.get("updated_at"),
    }


@router.websocket("/{task_id}/ws")
async def agent_websocket(websocket: WebSocket, task_id: str):
    """WebSocket для real-time обновлений шагов агента.
    Клиент подключается, получает текущее состояние и live-обновления."""
    await websocket.accept()
    t = agent_tasks.get(task_id)
    if not t:
        await websocket.send_json({"type": "error", "message": "Задача не найдена"})
        await websocket.close()
        return

    # Subscribe to future updates
    subscribe_task(task_id, websocket)

    try:
        # Send current state immediately
        await websocket.send_json({
            "type": "update",
            "task": {
                "task_id": task_id,
                "status": t["status"],
                "goal": t["goal"],
                "steps": t["steps"],
                "outputs": t.get("outputs", []),
                "result": t.get("result"),
            }
        })

        # Keep connection open and wait for completion
        while t["status"] not in ("done", "error"):
            try:
                # Wait for messages from client (keepalive) or close
                data = await asyncio.wait_for(websocket.receive_text(), timeout=300)
                # Echo back pong to keep connection alive
                await websocket.send_json({"type": "pong"})
            except asyncio.TimeoutError:
                # Check status on timeout
                t = agent_tasks.get(task_id, t)
                if t["status"] in ("done", "error"):
                    break
            except WebSocketDisconnect:
                break

        # Send final state
        t = agent_tasks.get(task_id, t)
        await websocket.send_json({
            "type": "done",
            "status": t.get("status", "error"),
            "result": t.get("result", ""),
            "steps": t.get("steps", []),
        })
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.error(f"WebSocket error for task {task_id}: {e}")
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass


@router.get("/{task_id}/stream")
async def agent_stream(task_id: str):
    """SSE stream для real-time обновлений шагов агента (fallback для старых клиентов)."""

    async def event_gen():
        last_step = 0
        for _ in range(300):  # ~5 min timeout
            await asyncio.sleep(1)
            t = agent_tasks.get(task_id)
            if not t:
                break
            # Send new steps
            while last_step < len(t["steps"]):
                step = t["steps"][last_step]
                data = json.dumps({"type": "step", "step": step}, ensure_ascii=False)
                yield f"data: {data}\n\n"
                last_step += 1
            # Done?
            if t["status"] in ("done", "error"):
                final = json.dumps(
                    {"type": "done", "status": t["status"], "result": t.get("result", "")},
                    ensure_ascii=False,
                )
                yield f"data: {final}\n\n"
                break

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/tools/list")
def agent_tools():
    """Список доступных инструментов агента."""
    return TOOL_SCHEMAS


# ── Agent Constructor ──────────────────────────────────────────────────────────

from server.models import AgentConfig
from pydantic import BaseModel as _BM

class AgentConfigRequest(_BM):
    name: str | None = "Мой агент"
    enabled_blocks: list | None = None
    channels: dict | None = None
    settings: dict | None = None
    status: str | None = "draft"

@router.get("/config")
def get_agent_config(db: Session = Depends(get_db), user=Depends(optional_user)):
    if not user: return {"configs": []}
    configs = db.query(AgentConfig).filter_by(user_id=user.id).order_by(AgentConfig.created_at.desc()).all()
    result = []
    for c in configs:
        result.append({
            "id": c.id, "name": c.name, "status": c.status,
            "enabled_blocks": json.loads(c.enabled_blocks) if c.enabled_blocks else [],
            "channels": json.loads(c.channels) if c.channels else {},
            "settings": json.loads(c.settings) if c.settings else {},
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "updated_at": c.updated_at.isoformat() if c.updated_at else None,
        })
    return {"configs": result}


@router.post("/config")
def create_agent_config(req: AgentConfigRequest, db: Session = Depends(get_db), user=Depends(optional_user)):
    if not user: raise HTTPException(401, "Нужна авторизация")
    c = AgentConfig(
        user_id=user.id, name=req.name,
        enabled_blocks=json.dumps(req.enabled_blocks or []),
        channels=json.dumps(req.channels or {}),
        settings=json.dumps(req.settings or {}),
        status=req.status or "draft",
    )
    db.add(c); db.commit(); db.refresh(c)
    return {"id": c.id, "status": "created"}


@router.put("/config/{config_id}")
def update_agent_config(config_id: int, req: AgentConfigRequest,
                        db: Session = Depends(get_db), user=Depends(optional_user)):
    if not user: raise HTTPException(401, "Нужна авторизация")
    c = db.query(AgentConfig).filter_by(id=config_id, user_id=user.id).first()
    if not c: raise HTTPException(404, "Конфигурация не найдена")
    if req.name is not None: c.name = req.name
    if req.enabled_blocks is not None: c.enabled_blocks = json.dumps(req.enabled_blocks)
    if req.channels is not None: c.channels = json.dumps(req.channels)
    if req.settings is not None: c.settings = json.dumps(req.settings)
    if req.status is not None: c.status = req.status
    db.commit()
    return {"status": "ok"}


@router.delete("/config/{config_id}")
def delete_agent_config(config_id: int, db: Session = Depends(get_db), user=Depends(optional_user)):
    if not user: raise HTTPException(401, "Нужна авторизация")
    c = db.query(AgentConfig).filter_by(id=config_id, user_id=user.id).first()
    if not c: raise HTTPException(404, "Конфигурация не найдена")
    db.delete(c); db.commit()
    return {"status": "deleted"}
