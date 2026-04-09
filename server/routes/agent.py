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
