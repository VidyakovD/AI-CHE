"""Модульные ИИ Агенты (раздел 23) — singleton-агент на юзера + модули.

См. docs/modules/23-agents-modular-roadmap.md.

Архитектура (правка 2026-05-16):
  User (1) ←→ (1) Agent ←→ (N) AgentModule
                  ↓ messages (история диалога)
                  ↓ profile_json (Memory Hub)
                  ↓ personality_json (имя/иконка/стиль)

Endpoints:
  GET    /api/agents/me                       — мой агент (get-or-create singleton)
  PATCH  /api/agents/me                       — обновить имя/иконку/profile/personality/status
  GET    /api/agents/me/messages              — история диалога
  POST   /api/agents/me/messages              — отправить сообщение → ответ агента
  GET    /api/agents/me/modules               — подключённые модули
  POST   /api/agents/me/modules               — подключить модуль из каталога
  PATCH  /api/agents/me/modules/{slug}        — обновить настройки/уровень/enabled
  DELETE /api/agents/me/modules/{slug}        — отключить (удалить) модуль
  GET    /api/agents/catalog                  — каталог доступных модулей (из AGENT_REGISTRY)

UI: views/agents-modular.html (chat-first главный экран + sidebar модулей).
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
from server.models import User, Agent, AgentMessage, AgentModule

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agents", tags=["agents-modular"])


# ── helpers ──────────────────────────────────────────────────────────────────

DEFAULT_AGENT_NAME = "Че"
DEFAULT_AGENT_ICON = "🤖"

# Дефолтное приветствие при первом онбординге — без LLM-вызова чтобы UI не ждал.
ONBOARDING_GREETING = (
    "Привет! Я твой ИИ-агент. Меня зовут Че. Давай знакомиться 🤝\n\n"
    "Чтобы я был максимально полезным, расскажи о себе:\n"
    "• Как тебя зовут?\n"
    "• Чем занимаешься (бизнес, фриланс, хобби)?\n"
    "• Какой стиль общения предпочитаешь — деловой / дружеский / лаконичный?\n\n"
    "Можешь рассказать кратко или подробно — я запомню и буду использовать это в работе."
)


def _safe_json(s: str | None, default: Any = None) -> Any:
    if not s:
        return default if default is not None else {}
    try:
        return json.loads(s)
    except Exception:
        return default if default is not None else {}


def get_or_create_agent(user: User, db: Session) -> Agent:
    """Singleton: один Agent на юзера. Если есть несколько (старые дубли) —
    берём min(id), остальные архивируем (status=archived)."""
    agents = (db.query(Agent)
                .filter(Agent.user_id == user.id, Agent.status != "archived")
                .order_by(Agent.id.asc())
                .all())
    if agents:
        primary = agents[0]
        # Архивируем дубли (если есть) — silent cleanup
        for dup in agents[1:]:
            dup.status = "archived"
            log.info(f"[agents] archived duplicate Agent id={dup.id} for user={user.id}")
        if len(agents) > 1:
            db.commit()
        return primary

    # Создаём первого — статус onboarding (агент будет знакомиться)
    a = Agent(
        user_id=user.id,
        name=DEFAULT_AGENT_NAME,
        icon=DEFAULT_AGENT_ICON,
        spec_json=json.dumps({"version": 1}, ensure_ascii=False),
        profile_json=json.dumps({"facts": []}, ensure_ascii=False),
        personality_json=json.dumps({
            "display_name": DEFAULT_AGENT_NAME,
            "icon": DEFAULT_AGENT_ICON,
            "voice": "friendly",
        }, ensure_ascii=False),
        status="onboarding",
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    # Первое сообщение от агента — приветствие
    db.add(AgentMessage(
        agent_id=a.id, role="assistant", content=ONBOARDING_GREETING,
        meta_json=json.dumps({"mode": "onboarding", "step": "greeting"}, ensure_ascii=False),
    ))
    db.commit()
    log.info(f"[agents] created personal agent for user={user.id} (onboarding)")
    return a


def _agent_dict(a: Agent, *, with_modules_count: bool = False, db: Session | None = None) -> dict:
    profile = _safe_json(a.profile_json, {"facts": []})
    personality = _safe_json(a.personality_json, {})
    out = {
        "id": a.id,
        "name": a.name or DEFAULT_AGENT_NAME,
        "icon": a.icon or DEFAULT_AGENT_ICON,
        "status": a.status or "onboarding",
        "profile": profile,
        "personality": personality,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "last_activity_at": a.last_activity_at.isoformat() if a.last_activity_at else None,
    }
    if with_modules_count and db is not None:
        out["modules_count"] = (db.query(AgentModule)
                                  .filter_by(agent_id=a.id, is_enabled=True)
                                  .count())
    return out


def _module_dict(m: AgentModule, *, with_meta: bool = False) -> dict:
    """Карточка модуля. with_meta=True — добавляет описание из AGENT_REGISTRY."""
    out = {
        "id": m.id,
        "slug": m.slug,
        "level": int(m.level or 0),
        "interaction_count": int(m.interaction_count or 0),
        "schedule_cron": m.schedule_cron,
        "is_enabled": bool(m.is_enabled),
        "settings": _safe_json(m.custom_settings_json, {}),
        "memory": _safe_json(m.module_memory_json, {}),
        "connected_at": m.connected_at.isoformat() if m.connected_at else None,
        "last_used_at": m.last_used_at.isoformat() if m.last_used_at else None,
    }
    if with_meta:
        try:
            from server.agent_runner import AGENT_REGISTRY
            meta = AGENT_REGISTRY.get(m.slug, {})
            out["name"] = meta.get("name", m.slug)
            out["description"] = meta.get("description", "")
        except Exception:
            out["name"] = m.slug
            out["description"] = ""
    return out


def _msg_dict(m: AgentMessage) -> dict:
    return {
        "id": m.id,
        "role": m.role,
        "content": m.content or "",
        "meta": _safe_json(m.meta_json, {}),
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


# ── singleton agent ──────────────────────────────────────────────────────────

@router.get("/me")
def get_my_agent(db: Session = Depends(get_db),
                 user: User = Depends(current_user)):
    """Получить (или создать при первом обращении) моего агента."""
    a = get_or_create_agent(user, db)
    return _agent_dict(a, with_modules_count=True, db=db)


class PatchAgentPayload(BaseModel):
    name: str | None = None
    icon: str | None = None
    profile: dict | None = None        # Memory Hub (полный override)
    personality: dict | None = None    # имя/стиль/voice
    status: str | None = None          # onboarding|active|paused


@router.patch("/me")
def patch_my_agent(payload: PatchAgentPayload,
                   db: Session = Depends(get_db),
                   user: User = Depends(current_user)):
    a = get_or_create_agent(user, db)
    if payload.name is not None:
        a.name = payload.name.strip()[:80] or a.name
    if payload.icon is not None:
        a.icon = payload.icon.strip()[:4] or a.icon
    if payload.profile is not None and isinstance(payload.profile, dict):
        a.profile_json = json.dumps(payload.profile, ensure_ascii=False)
    if payload.personality is not None and isinstance(payload.personality, dict):
        a.personality_json = json.dumps(payload.personality, ensure_ascii=False)
    if payload.status in ("onboarding", "active", "paused"):
        a.status = payload.status
    a.updated_at = datetime.utcnow()
    db.commit(); db.refresh(a)
    return _agent_dict(a, with_modules_count=True, db=db)


# ── messages ──────────────────────────────────────────────────────────────────

@router.get("/me/messages")
def list_messages(limit: int = 200,
                  db: Session = Depends(get_db),
                  user: User = Depends(current_user)):
    a = get_or_create_agent(user, db)
    limit = max(1, min(int(limit or 200), 1000))
    msgs = (db.query(AgentMessage)
              .filter_by(agent_id=a.id)
              .order_by(AgentMessage.id.asc())
              .limit(limit)
              .all())
    return {"messages": [_msg_dict(m) for m in msgs]}


class SendMessagePayload(BaseModel):
    content: str


@router.post("/me/messages")
def send_message(payload: SendMessagePayload,
                 db: Session = Depends(get_db),
                 user: User = Depends(current_user)):
    """Отправить сообщение моему агенту.

    Режимы:
      onboarding — агент задаёт вопросы для Memory Hub, после 5+ сообщений
                   автоматически переходит в active
      active     — обычный режим: команды, изменения настроек, общение
    """
    a = get_or_create_agent(user, db)
    text = (payload.content or "").strip()
    if not text:
        raise HTTPException(400, "Пустое сообщение")
    if len(text) > 10000:
        raise HTTPException(413, "Слишком длинное сообщение (макс 10 КБ)")

    mode = "onboarding" if a.status == "onboarding" else "active"

    # 1. Сохраняем сообщение юзера
    user_msg = AgentMessage(
        agent_id=a.id, role="user", content=text,
        meta_json=json.dumps({"mode": mode}, ensure_ascii=False),
    )
    db.add(user_msg)
    db.flush()

    # 2. Готовим контекст для агента (история + профиль + подключённые модули)
    history = (db.query(AgentMessage)
                 .filter(AgentMessage.agent_id == a.id,
                         AgentMessage.id < user_msg.id,
                         AgentMessage.role.in_(("user", "assistant")))
                 .order_by(AgentMessage.id.asc())
                 .all())
    history_dicts = [{"role": m.role, "content": m.content or ""} for m in history]

    modules = (db.query(AgentModule)
                 .filter_by(agent_id=a.id, is_enabled=True)
                 .all())
    modules_summary = [{"slug": m.slug, "level": m.level} for m in modules]

    profile = _safe_json(a.profile_json, {"facts": []})
    personality = _safe_json(a.personality_json, {})

    asst_meta: dict = {"mode": mode}
    reply = ""
    invoke_request = None
    suggest_modules: list[str] = []

    try:
        from server.agent_builder import build_reply_personal
        result = build_reply_personal(
            agent_name=a.name or DEFAULT_AGENT_NAME,
            mode=mode,
            profile=profile,
            personality=personality,
            modules=modules_summary,
            history=history_dicts,
            user_input=text,
        )
        reply = result["reply"]
        asst_meta["applied"] = result.get("applied", [])
        if result.get("errors"):
            asst_meta["errors"] = result["errors"]
        # Сохраняем обновлённый profile (Memory Hub после изменений)
        if result.get("profile_changed"):
            a.profile_json = json.dumps(profile, ensure_ascii=False)
        # suggest_modules — клиенту, для UI чипов
        suggest_modules = result.get("suggest_modules", [])
        if suggest_modules:
            asst_meta["suggest_modules"] = suggest_modules
        # quick_replies — варианты ответа кнопками
        qrs = result.get("quick_replies", [])
        if qrs:
            asst_meta["quick_replies"] = qrs
        # invoke_module — делегирование уже подключённому модулю
        invoke_request = result.get("invoke_request")
        # Авто-активация после онбординга если агент решил что готов
        if result.get("ready_for_active") and a.status == "onboarding":
            a.status = "active"
            asst_meta["activated"] = True
    except Exception as e:
        log.exception(f"[agents.send] failed: {e}")
        reply = "Что-то пошло не так при обработке 😔 Попробуй ещё раз."
        asst_meta["errors"] = [str(e)[:200]]

    asst_msg = AgentMessage(
        agent_id=a.id, role="assistant", content=reply,
        meta_json=json.dumps(asst_meta, ensure_ascii=False),
    )
    db.add(asst_msg)
    db.flush()

    # ── Если агент попросил delegate → запускаем модуль СИНХРОННО ──
    # MVP: short-running task (≤8s типично). Long-running через cron позже.
    module_msg = None
    if invoke_request and isinstance(invoke_request, dict):
        slug = invoke_request.get("slug")
        task = invoke_request.get("task", "")
        target_mod = next((m for m in modules if m.slug == slug), None)
        if target_mod and target_mod.is_enabled:
            try:
                from server.agent_builder import (
                    invoke_module, apply_module_memory_updates, compute_module_level,
                )
                mod_memory = _safe_json(target_mod.module_memory_json, {})
                mod_settings = _safe_json(target_mod.custom_settings_json, {})
                inv = invoke_module(
                    slug=slug, task=task,
                    profile=profile,
                    module_memory=mod_memory,
                    custom_settings=mod_settings,
                )
                # Создаём отдельное сообщение от модуля
                if inv.get("ok"):
                    mod_content = inv["output"]
                    # Применяем выученное модулем к его памяти
                    if inv.get("memory_updates"):
                        apply_module_memory_updates(mod_memory, inv["memory_updates"])
                        target_mod.module_memory_json = json.dumps(mod_memory, ensure_ascii=False)
                else:
                    mod_content = f"⚠ Модуль не справился: {inv.get('error', 'неизвестная ошибка')}"
                # Increment interaction_count + level recalc
                target_mod.interaction_count = (target_mod.interaction_count or 0) + 1
                target_mod.last_used_at = datetime.utcnow()
                learned_count = len((mod_memory.get("learned") or []))
                new_lvl = compute_module_level(
                    current_level=target_mod.level or 0,
                    interaction_count=target_mod.interaction_count,
                    agent_status=a.status,
                    learned_count=learned_count,
                )
                level_up = new_lvl > (target_mod.level or 0)
                if level_up:
                    target_mod.level = new_lvl
                module_msg = AgentMessage(
                    agent_id=a.id, role="tool", content=mod_content,
                    meta_json=json.dumps({
                        "mode": "module_invoke",
                        "slug": slug,
                        "model_used": inv.get("model_used", ""),
                        "level": target_mod.level,
                        "level_up": level_up,
                        "interactions": target_mod.interaction_count,
                    }, ensure_ascii=False),
                )
                db.add(module_msg)
            except Exception as e:
                log.exception(f"[agents.invoke] module {slug} failed: {e}")
                module_msg = AgentMessage(
                    agent_id=a.id, role="system",
                    content=f"⚠ Ошибка модуля {slug}: {e!s:.140}",
                )
                db.add(module_msg)

    a.last_activity_at = datetime.utcnow()
    a.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(user_msg); db.refresh(asst_msg); db.refresh(a)
    if module_msg:
        db.refresh(module_msg)

    out = {
        "user_message": _msg_dict(user_msg),
        "assistant_message": _msg_dict(asst_msg),
        "agent": _agent_dict(a, with_modules_count=True, db=db),
    }
    if module_msg:
        out["module_message"] = _msg_dict(module_msg)
    return out


# ── modules ──────────────────────────────────────────────────────────────────

@router.get("/me/modules")
def list_my_modules(db: Session = Depends(get_db),
                    user: User = Depends(current_user)):
    a = get_or_create_agent(user, db)
    mods = (db.query(AgentModule)
              .filter_by(agent_id=a.id)
              .order_by(AgentModule.is_enabled.desc(), AgentModule.connected_at.asc())
              .all())
    return {"modules": [_module_dict(m, with_meta=True) for m in mods]}


class ConnectModulePayload(BaseModel):
    slug: str
    schedule_cron: str | None = None
    settings: dict | None = None


@router.post("/me/modules")
def connect_module(payload: ConnectModulePayload,
                   db: Session = Depends(get_db),
                   user: User = Depends(current_user)):
    """Подключить модуль из каталога AGENT_REGISTRY к моему агенту."""
    if not user.is_verified:
        raise HTTPException(403, "Подтвердите email")
    a = get_or_create_agent(user, db)
    slug = (payload.slug or "").strip()
    if not slug:
        raise HTTPException(400, "Не указан slug модуля")

    from server.agent_runner import AGENT_REGISTRY
    if slug not in AGENT_REGISTRY:
        raise HTTPException(404, f"Модуль {slug!r} не найден в каталоге")

    existing = (db.query(AgentModule)
                  .filter_by(agent_id=a.id, slug=slug)
                  .first())
    if existing:
        # Если был отключён — включаем обратно
        if not existing.is_enabled:
            existing.is_enabled = True
        if payload.schedule_cron is not None:
            existing.schedule_cron = payload.schedule_cron or None
        if payload.settings is not None:
            existing.custom_settings_json = json.dumps(payload.settings, ensure_ascii=False)
        db.commit(); db.refresh(existing)
        return _module_dict(existing, with_meta=True)

    m = AgentModule(
        agent_id=a.id,
        slug=slug,
        level=0,
        is_enabled=True,
        schedule_cron=payload.schedule_cron,
        custom_settings_json=(json.dumps(payload.settings, ensure_ascii=False)
                              if payload.settings else None),
    )
    db.add(m); db.commit(); db.refresh(m)

    # Системное сообщение в чат — «подключён модуль X»
    meta = AGENT_REGISTRY[slug]
    db.add(AgentMessage(
        agent_id=a.id, role="system",
        content=f"✓ Подключён агент: {meta['name']} ({slug})",
        meta_json=json.dumps({"mode": "module_connected", "slug": slug}, ensure_ascii=False),
    ))
    a.last_activity_at = datetime.utcnow()
    db.commit()

    try:
        from server.audit_log import log_action
        log_action("agent.module_connect", user_id=user.id, target_type="agent_module",
                   target_id=m.id, details={"slug": slug})
    except Exception:
        pass

    return _module_dict(m, with_meta=True)


class PatchModulePayload(BaseModel):
    is_enabled: bool | None = None
    schedule_cron: str | None = None
    settings: dict | None = None
    level: int | None = None


@router.patch("/me/modules/{slug}")
def patch_module(slug: str, payload: PatchModulePayload,
                 db: Session = Depends(get_db),
                 user: User = Depends(current_user)):
    a = get_or_create_agent(user, db)
    m = db.query(AgentModule).filter_by(agent_id=a.id, slug=slug).first()
    if not m:
        raise HTTPException(404, "Модуль не подключён")
    if payload.is_enabled is not None:
        m.is_enabled = bool(payload.is_enabled)
    if payload.schedule_cron is not None:
        m.schedule_cron = payload.schedule_cron or None
    if payload.settings is not None:
        m.custom_settings_json = json.dumps(payload.settings, ensure_ascii=False)
    if payload.level is not None:
        m.level = max(0, min(4, int(payload.level)))
    db.commit(); db.refresh(m)
    return _module_dict(m, with_meta=True)


@router.delete("/me/modules/{slug}")
def disconnect_module(slug: str,
                      db: Session = Depends(get_db),
                      user: User = Depends(current_user)):
    """Отключить модуль (удалить). Память модуля стирается."""
    a = get_or_create_agent(user, db)
    m = db.query(AgentModule).filter_by(agent_id=a.id, slug=slug).first()
    if not m:
        raise HTTPException(404, "Модуль не подключён")
    db.delete(m)
    # Системное сообщение в чат
    try:
        from server.agent_runner import AGENT_REGISTRY
        meta = AGENT_REGISTRY.get(slug, {})
        name = meta.get("name", slug)
    except Exception:
        name = slug
    db.add(AgentMessage(
        agent_id=a.id, role="system",
        content=f"✗ Отключён агент: {name}",
        meta_json=json.dumps({"mode": "module_disconnected", "slug": slug}, ensure_ascii=False),
    ))
    db.commit()
    return {"status": "disconnected", "slug": slug}


# ── catalog (доступные модули) ───────────────────────────────────────────────

@router.get("/catalog")
def get_catalog(db: Session = Depends(get_db),
                user: User = Depends(current_user)):
    """Каталог доступных модулей из AGENT_REGISTRY (48 ролей).
    Помечает уже подключённые — UI не даст подключить дважды."""
    a = get_or_create_agent(user, db)
    connected = {m.slug for m in db.query(AgentModule).filter_by(agent_id=a.id).all()}

    from server.agent_runner import AGENT_REGISTRY
    items = []
    for slug, meta in sorted(AGENT_REGISTRY.items()):
        items.append({
            "slug": slug,
            "name": meta.get("name", slug),
            "description": meta.get("description", ""),
            "keywords": meta.get("keywords", []),
            "is_connected": slug in connected,
        })
    return {"modules": items, "total": len(items)}
