"""
MCP Server (Model Context Protocol от Anthropic).

Позволяет подключить AI Студия Че как «инструмент» к Claude Desktop, Cursor,
любому MCP-совместимому клиенту. Юзер в Claude Desktop говорит «покажи мои
последние КП» / «запусти SWOT для X» — Claude сам зовёт наш сервис.

Адаптация Dolibarr `htdocs/ai/server/mcp_server.php`.

Транспорт: HTTP JSON-RPC 2.0 (single endpoint). Auth: Bearer ApiToken
(тот же что в /api/v1 — переиспользуем infra).

Конфигурация в Claude Desktop:
    {
      "mcpServers": {
        "aiche": {
          "url": "https://aiche.ru/mcp",
          "headers": {"Authorization": "Bearer ai_che_<prefix>_<secret>"}
        }
      }
    }

Поддерживаемые методы JSON-RPC:
  - initialize           — handshake, server capabilities
  - tools/list           — каталог инструментов с input-schema
  - tools/call           — вызов конкретного tool

Tools (минимальный набор для B2B-сценариев):
  - get_balance          — баланс юзера в копейках/рублях
  - list_solutions       — каталог бизнес-решений (40 пилотов)
  - run_solution         — запустить orchestra-pilot (в фоне)
  - get_solution_status  — статус run'а (queued/running/done/failed)
  - list_proposals       — последние КП
  - get_proposal         — карточка КП по id
  - create_proposal      — создать КП-черновик (без AI-генерации)
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Depends
from sqlalchemy.orm import Session

from server.routes.deps import get_db
from server.routes.public_api import authenticate_token
from server.models import (
    User, Solution, SolutionRun, ProposalProject, ProposalBrand,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/mcp", tags=["mcp"])

# JSON-RPC 2.0 error codes
JSON_RPC_PARSE_ERROR = -32700
JSON_RPC_INVALID_REQUEST = -32600
JSON_RPC_METHOD_NOT_FOUND = -32601
JSON_RPC_INVALID_PARAMS = -32602
JSON_RPC_INTERNAL_ERROR = -32603
JSON_RPC_SERVER_ERROR = -32000

# MCP версия протокола
MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {
    "name": "aiche-mcp",
    "version": "1.0.0",
}


# ── JSON-RPC helpers ──────────────────────────────────────────────────────

def _rpc_result(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_error(req_id: Any, code: int, message: str, data: Any = None) -> dict:
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


# ── Tool registry ─────────────────────────────────────────────────────────

TOOLS: list[dict] = [
    {
        "name": "get_balance",
        "description": "Получить текущий баланс пользователя AI Студия Че (в копейках и рублях).",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "list_solutions",
        "description": "Получить каталог бизнес-решений (orchestra-пилотов). "
                       "Можно фильтровать по subcategory и поисковому запросу.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "subcategory": {
                    "type": "string",
                    "description": "Категория: research / marketing / sales / strategy / legal / finance / hr",
                },
                "query": {
                    "type": "string",
                    "description": "Поиск по названию/описанию/тегам",
                },
                "limit": {"type": "integer", "default": 50, "maximum": 100},
            },
            "required": [],
        },
    },
    {
        "name": "run_solution",
        "description": "Запустить orchestra-pilot. Возвращает run_id, статус — асинхронно. "
                       "Используй get_solution_status для отслеживания прогресса.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "solution_id": {"type": "integer", "description": "ID решения из list_solutions"},
                "input_text": {
                    "type": "string",
                    "description": "Входной текст (для legacy-решений) или JSON-объект "
                                   "{field_name: value} для v2-решений с input_schema",
                },
            },
            "required": ["solution_id", "input_text"],
        },
    },
    {
        "name": "get_solution_status",
        "description": "Узнать статус orchestra-run: queued / running / done / failed. "
                       "Если done — возвращает финальный output.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "integer"},
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "list_proposals",
        "description": "Список последних КП пользователя.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 20, "maximum": 100},
                "status": {
                    "type": "string",
                    "description": "draft / sent / opened / signed / won / lost",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_proposal",
        "description": "Карточка КП по ID: клиент / запрос / статус / public_token / даты.",
        "inputSchema": {
            "type": "object",
            "properties": {"proposal_id": {"type": "integer"}},
            "required": ["proposal_id"],
        },
    },
    {
        "name": "create_proposal",
        "description": "Создать КП-черновик (без AI-генерации). Для генерации потом "
                       "используй обычный API /api/v1/proposals/generate.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "client_name": {"type": "string"},
                "client_email": {"type": "string"},
                "client_request": {"type": "string"},
                "brand_id": {"type": "integer"},
            },
            "required": ["name", "client_request"],
        },
    },
]


# ── Tool implementations ──────────────────────────────────────────────────

def _tool_get_balance(user: User, db: Session, args: dict) -> dict:
    return {
        "balance_kop": int(user.tokens_balance or 0),
        "balance_rub": round((user.tokens_balance or 0) / 100, 2),
    }


def _tool_list_solutions(user: User, db: Session, args: dict) -> dict:
    subcategory = (args.get("subcategory") or "").strip().lower() or None
    query = (args.get("query") or "").strip().lower() or None
    limit = max(1, min(int(args.get("limit") or 50), 100))

    q = db.query(Solution).filter_by(is_active=True)
    if subcategory:
        q = q.filter(Solution.subcategory == subcategory)
    rows = q.order_by(Solution.is_featured.desc(), Solution.id.asc()).limit(limit * 3).all()

    items = []
    for s in rows:
        if query:
            haystack = f"{s.title or ''} {s.short_summary or ''} {s.description or ''} {s.tags or ''}".lower()
            if query not in haystack:
                continue
        items.append({
            "id": s.id,
            "title": s.title,
            "subcategory": s.subcategory,
            "short_summary": s.short_summary,
            "tags": s.tags,
            "is_featured": bool(s.is_featured),
            "price_kop": int(s.price_tokens or 0),
            "has_input_schema": bool(s.input_schema_json),
        })
        if len(items) >= limit:
            break
    return {"items": items, "count": len(items)}


def _tool_run_solution(user: User, db: Session, args: dict) -> dict:
    sid = int(args.get("solution_id") or 0)
    input_text = args.get("input_text") or ""
    if not sid:
        raise ValueError("solution_id обязателен")
    if not input_text:
        raise ValueError("input_text обязателен")

    sol = db.query(Solution).filter_by(id=sid, is_active=True).first()
    if not sol:
        raise ValueError(f"Решение #{sid} не найдено")

    # Создаём SolutionRun + spawn в фоне (паттерн как в /solutions/start)
    import secrets as _s
    chat_id = _s.token_urlsafe(16)
    run = SolutionRun(
        user_id=user.id,
        solution_id=sol.id,
        chat_id=chat_id,
        status="running",
        user_input=input_text[:50000],
        context=json.dumps({}, ensure_ascii=False),
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    from server.solutions_orchestra import run_orchestra
    from server._async_tasks import spawn
    spawn(run_orchestra(run.id), name=f"mcp:orchestra:{run.id}")

    return {
        "run_id": run.id,
        "chat_id": chat_id,
        "status": "running",
        "solution": {"id": sol.id, "title": sol.title, "price_kop": sol.price_tokens},
    }


def _tool_get_solution_status(user: User, db: Session, args: dict) -> dict:
    rid = int(args.get("run_id") or 0)
    if not rid:
        raise ValueError("run_id обязателен")
    run = db.query(SolutionRun).filter_by(id=rid, user_id=user.id).first()
    if not run:
        raise ValueError(f"Run #{rid} не найден или не ваш")
    out = {
        "run_id": run.id,
        "solution_id": run.solution_id,
        "status": run.status,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "total_cost_kop": int(run.total_cost_kop or 0),
        "current_step": int(run.current_step or 0),
    }
    if run.status == "done":
        out["output"] = (run.final_output or "")[:50000]
    if run.status == "error" and run.stages_state:
        try:
            st = json.loads(run.stages_state)
            errors = [
                f"{sid}: {s.get('error')}"
                for sid, s in (st.get("stages") or {}).items()
                if s.get("error")
            ]
            if errors:
                out["error"] = "; ".join(errors)[:500]
        except Exception:
            pass
    return out


def _tool_list_proposals(user: User, db: Session, args: dict) -> dict:
    limit = max(1, min(int(args.get("limit") or 20), 100))
    status = (args.get("status") or "").strip() or None
    q = db.query(ProposalProject).filter_by(user_id=user.id)
    if status:
        q = q.filter(ProposalProject.status == status)
    rows = q.order_by(ProposalProject.id.desc()).limit(limit).all()
    return {
        "items": [
            {
                "id": p.id,
                "name": p.name,
                "client_name": p.client_name,
                "status": p.status,
                "crm_stage": p.crm_stage,
                "public_token": p.public_token,
                "created_at": p.created_at.isoformat() if p.created_at else None,
                "sent_at": p.sent_at.isoformat() if p.sent_at else None,
                "opened_at": p.opened_at.isoformat() if p.opened_at else None,
            }
            for p in rows
        ]
    }


def _tool_get_proposal(user: User, db: Session, args: dict) -> dict:
    pid = int(args.get("proposal_id") or 0)
    p = db.query(ProposalProject).filter_by(id=pid, user_id=user.id).first()
    if not p:
        raise ValueError(f"КП #{pid} не найдено или не ваше")
    public_url = None
    if p.public_token:
        app_url = os.getenv("APP_URL", "https://aiche.ru").rstrip("/")
        public_url = f"{app_url}/p/{p.public_token}"
    return {
        "id": p.id,
        "name": p.name,
        "client_name": p.client_name,
        "client_email": p.client_email,
        "client_request": p.client_request,
        "status": p.status,
        "crm_stage": p.crm_stage,
        "price_kop": int(p.price_kop or 0),
        "public_url": public_url,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "sent_at": p.sent_at.isoformat() if p.sent_at else None,
        "opened_at": p.opened_at.isoformat() if p.opened_at else None,
        "won_at": p.won_at.isoformat() if p.won_at else None,
    }


def _tool_create_proposal(user: User, db: Session, args: dict) -> dict:
    name = (args.get("name") or "").strip()
    request_text = (args.get("client_request") or "").strip()
    if not name or not request_text:
        raise ValueError("name и client_request обязательны")
    brand_id = args.get("brand_id")
    if brand_id:
        b = db.query(ProposalBrand).filter_by(id=int(brand_id), user_id=user.id).first()
        if not b:
            raise ValueError(f"Бренд #{brand_id} не найден")
    p = ProposalProject(
        user_id=user.id,
        name=name[:200],
        client_name=(args.get("client_name") or "")[:200] or None,
        client_email=(args.get("client_email") or "")[:200] or None,
        client_request=request_text[:50000],
        brand_id=int(brand_id) if brand_id else None,
        status="draft",
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return {
        "id": p.id,
        "name": p.name,
        "status": "draft",
        "next_step": "Use /api/v1/proposals/generate to fill the proposal HTML",
    }


_TOOL_HANDLERS = {
    "get_balance": _tool_get_balance,
    "list_solutions": _tool_list_solutions,
    "run_solution": _tool_run_solution,
    "get_solution_status": _tool_get_solution_status,
    "list_proposals": _tool_list_proposals,
    "get_proposal": _tool_get_proposal,
    "create_proposal": _tool_create_proposal,
}


# ── JSON-RPC dispatcher ───────────────────────────────────────────────────

def _handle_initialize(req_id: Any, params: dict) -> dict:
    """MCP handshake. Возвращаем server capabilities."""
    return _rpc_result(req_id, {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {
            "tools": {"listChanged": False},
        },
        "serverInfo": SERVER_INFO,
    })


def _handle_tools_list(req_id: Any, params: dict) -> dict:
    return _rpc_result(req_id, {"tools": TOOLS})


def _handle_tools_call(req_id: Any, params: dict, user: User, db: Session) -> dict:
    name = (params or {}).get("name") or ""
    args = (params or {}).get("arguments") or {}
    handler = _TOOL_HANDLERS.get(name)
    if not handler:
        return _rpc_error(req_id, JSON_RPC_METHOD_NOT_FOUND, f"Unknown tool: {name}")
    try:
        result = handler(user, db, args)
    except ValueError as e:
        # Бизнес-ошибки (валидация, not-found) — возвращаем как tool result
        # с isError=True (по MCP spec), а не как JSON-RPC error
        return _rpc_result(req_id, {
            "content": [{"type": "text", "text": str(e)}],
            "isError": True,
        })
    except Exception as e:
        log.error(f"[mcp] tool {name} failed: {type(e).__name__}: {e}")
        return _rpc_error(req_id, JSON_RPC_INTERNAL_ERROR,
                          f"Tool execution failed: {type(e).__name__}")
    # Успех: оборачиваем в content array (по MCP spec)
    return _rpc_result(req_id, {
        "content": [{
            "type": "text",
            "text": json.dumps(result, ensure_ascii=False, indent=2, default=str),
        }],
    })


def _dispatch(req: dict, user: User, db: Session) -> dict:
    """Один JSON-RPC request → response."""
    req_id = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}

    if not method:
        return _rpc_error(req_id, JSON_RPC_INVALID_REQUEST, "Missing 'method'")

    if method == "initialize":
        return _handle_initialize(req_id, params)
    if method in ("notifications/initialized", "notifications/cancelled"):
        # Notifications не имеют id, ответ не нужен
        return None  # type: ignore[return-value]
    if method == "ping":
        return _rpc_result(req_id, {})
    if method == "tools/list":
        return _handle_tools_list(req_id, params)
    if method == "tools/call":
        return _handle_tools_call(req_id, params, user, db)

    return _rpc_error(req_id, JSON_RPC_METHOD_NOT_FOUND, f"Unknown method: {method}")


@router.post("")
async def mcp_endpoint(request: Request, db: Session = Depends(get_db)):
    """MCP JSON-RPC endpoint. POST с body { jsonrpc, id, method, params }.

    Auth обязателен: Bearer ApiToken. Используем тот же механизм что в
    /api/v1 — токен создаётся в кабинете → 'API & интеграции'.

    Поддерживается одиночный request и batch-array (по JSON-RPC spec).
    """
    user = authenticate_token(request, db)

    try:
        body = await request.json()
    except Exception:
        return _rpc_error(None, JSON_RPC_PARSE_ERROR, "Invalid JSON")

    # Batch
    if isinstance(body, list):
        out = []
        for req in body:
            if not isinstance(req, dict):
                out.append(_rpc_error(None, JSON_RPC_INVALID_REQUEST,
                                      "Batch item must be object"))
                continue
            resp = _dispatch(req, user, db)
            if resp is not None:
                out.append(resp)
        return out

    # Single
    if not isinstance(body, dict):
        return _rpc_error(None, JSON_RPC_INVALID_REQUEST, "Body must be object or array")
    resp = _dispatch(body, user, db)
    return resp if resp is not None else {"jsonrpc": "2.0"}


@router.get("")
async def mcp_info():
    """Информация о MCP-сервере (для GET-запросов от любопытных).
    Не требует auth."""
    return {
        "name": SERVER_INFO["name"],
        "version": SERVER_INFO["version"],
        "protocol": MCP_PROTOCOL_VERSION,
        "transport": "http",
        "auth": "Bearer ApiToken (создать в кабинете → 'API & интеграции')",
        "tools_count": len(TOOLS),
        "doc_url": "https://aiche.ru/api.html",
    }
