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
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from typing import Any

import secrets

from fastapi import APIRouter, Body, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from server.routes.deps import get_db, current_user
from server.models import (User, Agent, AgentMessage, AgentModule, UserMailbox,
                            FinanceTransaction)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agents", tags=["agents-modular"])


# ── Rate limiting ───────────────────────────────────────────────────────────
# Простой in-memory sliding window per user. На multi-worker (4 процесса)
# лимит размывается × workers — достаточно как первая защита.
# Redis-backed лимиты — отдельный спринт когда нагрузка появится.

_RL_LOCK = threading.Lock()
_RL_BUCKETS: dict[int, deque[float]] = defaultdict(deque)

# Лимиты для /me/messages: 20 сообщений / 60 сек, 200 / час
_RL_MSG_PER_MIN = 20
_RL_MSG_PER_HOUR = 200


def _rate_limit_check(user_id: int) -> tuple[bool, str]:
    """Sliding window: возвращает (allowed, reason)."""
    now = time.time()
    with _RL_LOCK:
        bucket = _RL_BUCKETS[user_id]
        # Чистим устаревшие (>1 час)
        while bucket and now - bucket[0] > 3600:
            bucket.popleft()
        # Считаем за последнюю минуту
        per_min = sum(1 for t in bucket if now - t <= 60)
        if per_min >= _RL_MSG_PER_MIN:
            return False, f"Слишком часто: {_RL_MSG_PER_MIN} сообщ./мин"
        if len(bucket) >= _RL_MSG_PER_HOUR:
            return False, f"Лимит {_RL_MSG_PER_HOUR} сообщ./час исчерпан"
        bucket.append(now)
    return True, ""


# ── helpers ──────────────────────────────────────────────────────────────────

DEFAULT_AGENT_NAME = "Че"
# Иконка-аватар: префикс seed: → dicebear bottts SVG; иначе обычный эмодзи.
# Default — seed:nova (одна из 16 preset-аватаров в стиле сервиса).
DEFAULT_AGENT_ICON = "seed:nova"

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


# Максимальный размер сериализованного meta_json в AgentMessage.
# Защита от раздувания при больших applied/suggest/quick_replies.
_META_MAX_BYTES = 8000


def _dump_meta(meta: dict) -> str:
    """Сериализовать meta для AgentMessage.meta_json с лимитом размера.
    При превышении — отрезаем «дешёвые» поля (applied/errors/raw)."""
    try:
        s = json.dumps(meta, ensure_ascii=False)
    except Exception:
        return "{}"
    if len(s.encode("utf-8")) <= _META_MAX_BYTES:
        return s
    # Сокращаем по убыванию приоритета
    trimmed = dict(meta)
    for k in ("raw", "applied", "errors"):
        trimmed.pop(k, None)
    try:
        s2 = json.dumps(trimmed, ensure_ascii=False)
    except Exception:
        return "{}"
    if len(s2.encode("utf-8")) > _META_MAX_BYTES:
        # Совсем минимальный набор
        keep = {k: trimmed.get(k) for k in ("mode", "slug", "level", "level_up",
                                             "activated", "ok")
                if k in trimmed}
        return json.dumps(keep, ensure_ascii=False)
    return s2


def _select_active_agents(user_id: int, db: Session) -> list[Agent]:
    return (db.query(Agent)
              .filter(Agent.user_id == user_id, Agent.status != "archived")
              .order_by(Agent.id.asc())
              .all())


def get_or_create_agent(user: User, db: Session) -> Agent:
    """Singleton: один Agent на юзера, race-safe.

    Защита от параллельных создателей на multi-worker:
      1. SELECT существующих — если есть → primary = min(id), архивируем дубли.
      2. INSERT под защитой partial UNIQUE (см. db.py LIGHTWEIGHT_INDEXES
         uq_agent_active_per_user) → IntegrityError при гонке → re-SELECT.
    """
    agents = _select_active_agents(user.id, db)
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
    try:
        db.commit()
        db.refresh(a)
    except IntegrityError:
        # Параллельный worker уже создал — забираем его
        db.rollback()
        agents = _select_active_agents(user.id, db)
        if agents:
            return agents[0]
        # Невероятный fallback — наверное UNIQUE упал по другой причине
        log.exception(f"[agents] IntegrityError without existing agent for user={user.id}")
        raise HTTPException(500, "Не удалось создать агента, попробуйте ещё раз")
    # Первое сообщение от агента — приветствие.
    # Защита от двойного greeting (если как-то прошло мимо UNIQUE): проверяем
    # что AgentMessage для этого agent_id ещё нет.
    has_msg = (db.query(AgentMessage.id)
                 .filter_by(agent_id=a.id)
                 .first())
    if not has_msg:
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


@router.get("/me/full")
def get_my_agent_full(messages_limit: int = 200,
                      db: Session = Depends(get_db),
                      user: User = Depends(current_user)):
    """Bootstrap endpoint: возвращает agent + modules + messages одним запросом.

    Экономит 3 RTT + 3 раза вызов get_or_create_agent при первой загрузке UI.
    """
    a = get_or_create_agent(user, db)
    messages_limit = max(1, min(int(messages_limit or 200), 1000))
    msgs = (db.query(AgentMessage)
              .filter_by(agent_id=a.id)
              .order_by(AgentMessage.id.asc())
              .limit(messages_limit)
              .all())
    mods = (db.query(AgentModule)
              .filter_by(agent_id=a.id)
              .order_by(AgentModule.is_enabled.desc(), AgentModule.connected_at.asc())
              .all())
    return {
        "agent": _agent_dict(a, with_modules_count=True, db=db),
        "modules": [_module_dict(m, with_meta=True) for m in mods],
        "messages": [_msg_dict(m) for m in msgs],
    }


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
        # До 64 символов — поддержка "seed:nova" формата + любых эмодзи.
        a.icon = payload.icon.strip()[:64] or a.icon
    if payload.profile is not None and isinstance(payload.profile, dict):
        a.profile_json = json.dumps(payload.profile, ensure_ascii=False)
    if payload.personality is not None and isinstance(payload.personality, dict):
        a.personality_json = json.dumps(payload.personality, ensure_ascii=False)
    if payload.status in ("onboarding", "active", "paused"):
        a.status = payload.status
    a.updated_at = datetime.utcnow()
    db.commit(); db.refresh(a)

    try:
        from server.audit_log import log_action
        log_action("agent.patch", user_id=user.id, target_type="agent",
                   target_id=a.id, details={
                       "name_changed": payload.name is not None,
                       "icon_changed": payload.icon is not None,
                       "profile_changed": payload.profile is not None,
                       "personality_changed": payload.personality is not None,
                       "status": payload.status,
                   })
    except Exception:
        pass
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
                   автоматически переходит в active. Первые N сообщений
                   бесплатно (agents.onboarding_free_messages, дефолт 5).
      active     — обычный режим: списываем agents.message за каждое
                   сообщение + agents.module_invoke если агент дёрнул модуль.
    """
    # Rate limit (защита от спама / выгорания провайдер-ключей)
    allowed, reason = _rate_limit_check(user.id)
    if not allowed:
        raise HTTPException(429, reason)

    a = get_or_create_agent(user, db)
    text = (payload.content or "").strip()
    if not text:
        raise HTTPException(400, "Пустое сообщение")
    if len(text) > 10000:
        raise HTTPException(413, "Слишком длинное сообщение (макс 10 КБ)")

    mode = "onboarding" if a.status == "onboarding" else "active"

    # ── Биллинг pre-check ────────────────────────────────────────────────
    # Считаем сколько user-сообщений было до этого. Onboarding-первые N бесплатно.
    from server.pricing import get_price
    from server.billing import deduct_atomic
    from server.models import Transaction

    prior_user_msgs = (db.query(AgentMessage.id)
                         .filter(AgentMessage.agent_id == a.id,
                                 AgentMessage.role == "user")
                         .count())
    free_onboarding = get_price("agents.onboarding_free_messages", default=5)
    is_free_onboarding = (mode == "onboarding"
                          and prior_user_msgs < free_onboarding)
    msg_cost = 0 if is_free_onboarding else get_price("agents.message", default=50)
    # Pre-check баланса: если не хватает на сообщение — отказываем
    if msg_cost > 0 and int(user.tokens_balance or 0) < msg_cost:
        raise HTTPException(402, f"Недостаточно средств. Нужно {msg_cost/100:.2f} ₽")

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
            user_id=user.id,
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

    # ── Биллинг: списываем за сообщение, ТОЛЬКО если был полезный ответ ──
    # Если упали с ошибкой (нет content) — не списываем (refund-friendly UX).
    charged_kop = 0
    if msg_cost > 0 and reply and not asst_meta.get("errors"):
        charged_kop = deduct_atomic(db, user.id, msg_cost)
        if charged_kop > 0:
            db.add(Transaction(
                user_id=user.id, type="usage",
                tokens_delta=-charged_kop,
                description=f"ИИ-агент: сообщение ({charged_kop/100:.2f} ₽)",
                model="agents.message",
            ))
            asst_meta["cost_kop"] = charged_kop
    elif is_free_onboarding:
        asst_meta["free_onboarding"] = True

    asst_msg = AgentMessage(
        agent_id=a.id, role="assistant", content=reply,
        meta_json=_dump_meta(asst_meta),
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
        # Pre-check баланса для invoke_module — если недостаточно, не делегируем
        module_cost = get_price("agents.module_invoke", default=100)
        if target_mod and target_mod.is_enabled and module_cost > 0 \
                and int(user.tokens_balance or 0) < module_cost:
            # Молча скипаем invoke — Че уже ответил коротким ack, скажем юзеру
            log.info(f"[agents.invoke] skipped {slug} for user={user.id}: low balance")
            target_mod = None
            asst_meta["module_skipped_reason"] = "low_balance"
        if target_mod and target_mod.is_enabled:
            try:
                from server.agent_builder import (
                    invoke_module, apply_module_memory_updates, compute_module_level,
                    increment_module_interaction,
                )
                mod_memory = _safe_json(target_mod.module_memory_json, {})
                mod_settings = _safe_json(target_mod.custom_settings_json, {})
                inv = invoke_module(
                    slug=slug, task=task,
                    profile=profile,
                    module_memory=mod_memory,
                    custom_settings=mod_settings,
                    user_id=user.id,
                )
                # Создаём отдельное сообщение от модуля
                level_up = False
                if inv.get("ok"):
                    mod_content = inv["output"]
                    # Применяем выученное модулем к его памяти.
                    # profile передаётся для Adaptive Prompts promotion:
                    # LEARNED:global маркеры мигрируют в Memory Hub (profile.facts).
                    if inv.get("memory_updates"):
                        apply_module_memory_updates(
                            mod_memory, inv["memory_updates"], profile=profile
                        )
                        target_mod.module_memory_json = json.dumps(mod_memory, ensure_ascii=False)
                        a.profile_json = json.dumps(profile, ensure_ascii=False)
                    target_mod.last_used_at = datetime.utcnow()
                    # Прокачка считается только за успешные вызовы.
                    # Атомарный SQL +1 (multi-worker safe — заменяет RMW).
                    new_count = increment_module_interaction(db, target_mod)
                    learned_count = len((mod_memory.get("learned") or []))
                    new_lvl = compute_module_level(
                        current_level=target_mod.level or 0,
                        interaction_count=new_count,
                        agent_status=a.status,
                        learned_count=learned_count,
                    )
                    level_up = new_lvl > (target_mod.level or 0)
                    if level_up:
                        target_mod.level = new_lvl
                else:
                    mod_content = f"⚠ Модуль не справился: {inv.get('error', 'неизвестная ошибка')}"
                    # last_used_at обновляем даже при ошибке (попытка была)
                    target_mod.last_used_at = datetime.utcnow()
                # Биллинг за вызов модуля — только если он реально отработал.
                module_charged_kop = 0
                if inv.get("ok") and module_cost > 0:
                    module_charged_kop = deduct_atomic(db, user.id, module_cost)
                    if module_charged_kop > 0:
                        db.add(Transaction(
                            user_id=user.id, type="usage",
                            tokens_delta=-module_charged_kop,
                            description=f"Модуль {slug}: {module_charged_kop/100:.2f} ₽",
                            model=f"agents.module:{slug}",
                        ))
                module_msg = AgentMessage(
                    agent_id=a.id, role="tool", content=mod_content,
                    meta_json=_dump_meta({
                        "mode": "module_invoke",
                        "slug": slug,
                        "model_used": inv.get("model_used", ""),
                        "level": target_mod.level,
                        "level_up": level_up,
                        "interactions": target_mod.interaction_count,
                        "ok": bool(inv.get("ok")),
                        "cost_kop": module_charged_kop,
                    }),
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

    # Лимит одновременно подключённых модулей (из pricing_config, default 12).
    # Считаем enabled-модули и проверяем ПЕРЕД добавлением нового / re-enable
    # отключённого. Сценарии:
    #   - existing=None → подключаем новый, count должен быть < limit
    #   - existing.is_enabled=False → включаем обратно, count тоже должен быть < limit
    #   - existing.is_enabled=True → просто patch настроек, лимит не трогаем
    from server.pricing import get_price
    _limit = max(1, get_price("agents.max_enabled_modules", default=12))
    _will_increment = (existing is None) or (not existing.is_enabled)
    if _will_increment:
        _enabled_now = (db.query(AgentModule)
                          .filter_by(agent_id=a.id, is_enabled=True)
                          .count())
        if _enabled_now >= _limit:
            raise HTTPException(
                400,
                f"Максимум {_limit} агентов одновременно. Отключите ненужный, "
                "прежде чем подключать новый."
            )

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
    # ВАЖНО: level НЕ принимаем от клиента — прокачка только через
    # compute_module_level (взаимодействия + заученное). L4-автономия —
    # отдельный явный endpoint (TODO когда понадобится).


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
    db.commit(); db.refresh(m)
    return _module_dict(m, with_meta=True)


class InvokeModulePayload(BaseModel):
    task: str | None = None  # если None — используем custom_settings.cron_task


@router.post("/me/modules/{slug}/invoke")
def invoke_module_now(slug: str,
                       payload: InvokeModulePayload,
                       db: Session = Depends(get_db),
                       user: User = Depends(current_user)):
    """Запустить модуль вручную «сейчас» с заданной задачей.

    Использует ту же логику что cron-runtime (биллинг, прокачка, сообщение
    в чат role=tool). Юзер видит эту кнопку рядом с настройкой расписания —
    «протестировать прямо сейчас» / «запустить разово».
    """
    if not user.is_verified:
        raise HTTPException(403, "Подтвердите email")
    allowed, reason = _rate_limit_check(user.id)
    if not allowed:
        raise HTTPException(429, reason)

    a = get_or_create_agent(user, db)
    m = db.query(AgentModule).filter_by(agent_id=a.id, slug=slug).first()
    if not m or not m.is_enabled:
        raise HTTPException(404, "Модуль не подключён или отключён")

    settings = _safe_json(m.custom_settings_json, {})
    task = (payload.task or settings.get("cron_task") or "").strip()
    if not task:
        raise HTTPException(400, "Не задана задача — передай task в body или "
                                  "сохрани cron_task в настройках модуля")
    task = task[:8000]

    from server.pricing import get_price
    from server.billing import deduct_atomic
    from server.models import Transaction
    from server.agent_builder import (
        invoke_module, apply_module_memory_updates, compute_module_level,
        increment_module_interaction,
    )

    module_cost = get_price("agents.module_invoke", default=100)
    if module_cost > 0 and int(user.tokens_balance or 0) < module_cost:
        raise HTTPException(402, f"Недостаточно средств. Нужно {module_cost/100:.2f} ₽")

    profile = _safe_json(a.profile_json, {"facts": []})
    mod_memory = _safe_json(m.module_memory_json, {})

    try:
        inv = invoke_module(
            slug=slug, task=task,
            profile=profile,
            module_memory=mod_memory,
            custom_settings=settings,
            user_id=user.id,
        )
    except Exception as e:
        log.exception(f"[agents.invoke_now] module {slug} failed: {e}")
        raise HTTPException(500, f"Модуль упал: {e!s:.140}")

    charged_kop = 0
    level_up = False
    if inv.get("ok"):
        if module_cost > 0:
            charged_kop = deduct_atomic(db, user.id, module_cost)
            if charged_kop > 0:
                db.add(Transaction(
                    user_id=user.id, type="usage",
                    tokens_delta=-charged_kop,
                    description=f"Модуль {slug} (запуск): {charged_kop/100:.2f} ₽",
                    model=f"agents.module:{slug}",
                ))
        if inv.get("memory_updates"):
            # Adaptive Prompts: scope=global → promotion в Memory Hub.
            apply_module_memory_updates(
                mod_memory, inv["memory_updates"], profile=profile
            )
            m.module_memory_json = json.dumps(mod_memory, ensure_ascii=False)
            a.profile_json = json.dumps(profile, ensure_ascii=False)
        m.last_used_at = datetime.utcnow()
        # Атомарный SQL +1 (multi-worker safe)
        new_count = increment_module_interaction(db, m)
        learned_count = len(mod_memory.get("learned") or [])
        new_lvl = compute_module_level(
            current_level=m.level or 0,
            interaction_count=new_count,
            agent_status=a.status,
            learned_count=learned_count,
        )
        level_up = new_lvl > (m.level or 0)
        if level_up:
            m.level = new_lvl
        content = inv["output"]
    else:
        content = f"⚠ Модуль не справился: {inv.get('error', 'неизвестная ошибка')}"

    msg = AgentMessage(
        agent_id=a.id, role="tool", content=content,
        meta_json=_dump_meta({
            "mode": "manual_invoke",
            "slug": slug,
            "model_used": inv.get("model_used", ""),
            "level": m.level,
            "level_up": level_up,
            "interactions": m.interaction_count,
            "ok": bool(inv.get("ok")),
            "cost_kop": charged_kop,
        }),
    )
    db.add(msg)
    a.last_activity_at = datetime.utcnow()
    db.commit()
    db.refresh(msg)

    try:
        from server.audit_log import log_action
        log_action(
            "agent.manual_invoke", user_id=user.id,
            target_type="agent_module", target_id=m.id,
            details={"slug": slug, "ok": bool(inv.get("ok")),
                      "cost_kop": charged_kop, "level": m.level},
        )
    except Exception:
        pass

    return {
        "ok": bool(inv.get("ok")),
        "message": _msg_dict(msg),
        "module": _module_dict(m, with_meta=True),
        "level_up": level_up,
    }


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

    try:
        from server.audit_log import log_action
        log_action("agent.module_disconnect", user_id=user.id, target_type="agent_module",
                   target_id=slug, details={"slug": slug, "agent_id": a.id})
    except Exception:
        pass
    return {"status": "disconnected", "slug": slug}


# ── bootstrap: импорт прошлых постов из подключённых каналов ────────────────


@router.post("/me/modules/{slug}/bootstrap")
async def bootstrap_module_memory(slug: str,
                                  db: Session = Depends(get_db),
                                  user: User = Depends(current_user)):
    """Bootstrap-импорт прошлых публикаций юзера в memory модуля.

    Сейчас поддерживается ТОЛЬКО slug='copywriter' — тянет посты из
    каждого подключённого канала каждого бренда (Креаторы):
      - VK community → wall.get (~50 свежих постов)
      - public TG канал → t.me/s/{username} preview (~30 постов)

    Импортированные посты складываются в copywriter.examples_by_brand[brand_id]
    через тот же save_published_to_copywriter, что и реальные публикации.
    Модуль учится стилю бренда мгновенно — не нужно ждать пока юзер
    опубликует через Креаторов 10+ постов.

    Бесплатно (трафик к VK/Telegram — копейки на стороне сервера, плата
    с юзера не имеет смысла). Лимит — 1 запуск раз в 5 минут на юзера
    (через общий rate-limit).
    """
    if not user.is_verified:
        raise HTTPException(403, "Подтвердите email")
    allowed, reason = _rate_limit_check(user.id)
    if not allowed:
        raise HTTPException(429, reason)
    if slug != "copywriter":
        raise HTTPException(400, "Bootstrap пока работает только для модуля copywriter")

    a = get_or_create_agent(user, db)
    m = db.query(AgentModule).filter_by(agent_id=a.id, slug=slug).first()
    if not m or not m.is_enabled:
        raise HTTPException(404, "Модуль не подключён или отключён — подключи "
                                 "copywriter из каталога и попробуй снова")

    from server.creators_bootstrap import bootstrap_copywriter_from_channels

    try:
        result = await bootstrap_copywriter_from_channels(db, user.id)
    except Exception as e:
        log.exception("[agents.bootstrap] failed: %s", e)
        raise HTTPException(500, f"Импорт упал: {e!s:.140}")

    # Если что-то импортировали — обновим last_used_at и прокачаем уровень.
    if result.get("imported", 0) > 0:
        from server.agent_builder import compute_module_level
        m.last_used_at = datetime.utcnow()
        # interaction_count не трогаем (это для разговоров с модулем), но
        # examples теперь есть → пересчитаем уровень. compute_module_level
        # сам решит на основе interaction_count + learned + agent.status.
        memory = _safe_json(m.module_memory_json, {})
        learned_count = len(memory.get("learned") or [])
        new_lvl = compute_module_level(
            current_level=m.level or 0,
            interaction_count=m.interaction_count or 0,
            agent_status=a.status,
            learned_count=learned_count,
        )
        if new_lvl > (m.level or 0):
            m.level = new_lvl
        db.commit()
        db.refresh(m)

        # Системное сообщение в чат
        brand_breakdown = "\n".join(
            f"  • {b['brand_name']}: {b['imported']} постов"
            for b in result.get("per_brand", []) if b.get("imported", 0) > 0
        )
        content = (f"📥 Импортировал {result['imported']} прошлых постов "
                   f"из твоих каналов:\n{brand_breakdown}\n\n"
                   "Теперь Копирайтер знает твой стиль — следующая генерация "
                   "будет писать в твоей манере.")
        db.add(AgentMessage(
            agent_id=a.id, role="system", content=content,
            meta_json=json.dumps({"mode": "module_bootstrap",
                                  "slug": slug,
                                  "imported": result["imported"]},
                                  ensure_ascii=False),
        ))
        db.commit()

    try:
        from server.audit_log import log_action
        log_action(
            "agent.module_bootstrap",
            user_id=user.id, target_type="agent_module", target_id=m.id,
            details={"slug": slug, "imported": result.get("imported", 0),
                     "brands": len(result.get("per_brand", []))},
        )
    except Exception:
        pass

    return {
        "ok": True,
        "imported": result.get("imported", 0),
        "per_brand": result.get("per_brand", []),
        "skipped_brands": result.get("skipped_brands", 0),
        "errors": result.get("errors", []),
        "module": _module_dict(m, with_meta=True),
    }


# ── module memory edit (раскрытие карточки агента в sidebar) ────────────────

class PatchMemoryItemPayload(BaseModel):
    index: int                          # индекс заметки в module_memory.learned
    note: str | None = None             # новый текст (None → delete)


@router.patch("/me/modules/{slug}/memory")
def patch_module_memory(slug: str, payload: PatchMemoryItemPayload,
                        db: Session = Depends(get_db),
                        user: User = Depends(current_user)):
    """Редактирование/удаление одной заметки из памяти агента-модуля.

    Юзер кликает по «знанию» в раскрытой карточке агента в sidebar →
    откроется prompt с текущим текстом → редактирует или очищает (delete).
    """
    a = get_or_create_agent(user, db)
    m = db.query(AgentModule).filter_by(agent_id=a.id, slug=slug).first()
    if not m:
        raise HTTPException(404, "Агент не подключён")
    memory = _safe_json(m.module_memory_json, {})
    learned = memory.get("learned") or []
    if not (0 <= payload.index < len(learned)):
        raise HTTPException(400, "Неверный индекс заметки")
    note = (payload.note or "").strip()
    if not note:
        # Удалить
        learned.pop(payload.index)
    else:
        # Заменить (мутируем dict внутри, чтобы сохранить ts/прочее)
        item = learned[payload.index]
        if not isinstance(item, dict):
            item = {}
        item["note"] = note[:300]
        item["edited_at"] = datetime.utcnow().isoformat()
        learned[payload.index] = item
    memory["learned"] = learned
    m.module_memory_json = json.dumps(memory, ensure_ascii=False)
    db.commit(); db.refresh(m)

    try:
        from server.audit_log import log_action
        log_action(
            "agent.module_memory_edit",
            user_id=user.id, target_type="agent_module", target_id=m.id,
            details={"slug": slug, "index": payload.index,
                     "action": "delete" if not note else "edit"},
        )
    except Exception:
        pass
    return _module_dict(m, with_meta=True)


# ── webhook trigger (внешний вход) ───────────────────────────────────────────


@router.post("/me/modules/{slug}/webhook")
def generate_webhook_token(slug: str,
                            db: Session = Depends(get_db),
                            user: User = Depends(current_user)):
    """Сгенерировать (или ротировать) webhook-токен для модуля.

    Юзер копирует URL вида:
      https://aiche.ru/api/agents/triggers/webhook/{token}
    и втыкает в CRM / Zapier / etc. При POST на этот URL — модуль запускается.
    """
    if not user.is_verified:
        raise HTTPException(403, "Подтвердите email")
    a = get_or_create_agent(user, db)
    m = db.query(AgentModule).filter_by(agent_id=a.id, slug=slug).first()
    if not m:
        raise HTTPException(404, "Модуль не подключён")
    settings = _safe_json(m.custom_settings_json, {})
    # 32 hex символа — 128 бит энтропии, unguessable
    token = secrets.token_hex(16)
    settings["webhook_token"] = token
    m.custom_settings_json = json.dumps(settings, ensure_ascii=False)
    db.commit(); db.refresh(m)

    try:
        from server.audit_log import log_action
        log_action(
            "agent.webhook_token_create",
            user_id=user.id, target_type="agent_module", target_id=m.id,
            details={"slug": slug, "token_suffix": token[-4:]},  # full token не пишем
        )
    except Exception:
        pass
    return {
        "token": token,
        "webhook_url": f"/api/agents/triggers/webhook/{token}",
        "module": _module_dict(m, with_meta=True),
    }


@router.delete("/me/modules/{slug}/webhook")
def revoke_webhook_token(slug: str,
                         db: Session = Depends(get_db),
                         user: User = Depends(current_user)):
    """Удалить webhook-токен. Старый URL перестанет работать."""
    a = get_or_create_agent(user, db)
    m = db.query(AgentModule).filter_by(agent_id=a.id, slug=slug).first()
    if not m:
        raise HTTPException(404, "Модуль не подключён")
    settings = _safe_json(m.custom_settings_json, {})
    had_token = bool(settings.get("webhook_token"))
    settings.pop("webhook_token", None)
    m.custom_settings_json = json.dumps(settings, ensure_ascii=False)
    db.commit(); db.refresh(m)

    if had_token:
        try:
            from server.audit_log import log_action
            log_action(
                "agent.webhook_token_revoke",
                user_id=user.id, target_type="agent_module", target_id=m.id,
                details={"slug": slug},
            )
        except Exception:
            pass
    return {"module": _module_dict(m, with_meta=True)}


# Публичный endpoint — БЕЗ auth, защита через unguessable token.
# Лимит размера body — 32 KB, чтобы CRM не присылал гигабайты.
_WEBHOOK_BODY_MAX = 32 * 1024


@router.post("/triggers/webhook/{token}")
async def fire_webhook(token: str, request: Request,
                       db: Session = Depends(get_db)):
    """Внешний триггер модуля по webhook-токену.

    Тело запроса (опц., JSON или текст) попадает в задачу модуля как
    "контекст события". Пример из CRM (Bitrix24/amoCRM):
      { "event":"new_lead", "lead":{"name":"Иван","phone":"+7..."} }
    """
    # Длина токена фиксированная: 32 hex
    if not token or len(token) != 32 or not all(c in "0123456789abcdef" for c in token):
        raise HTTPException(404, "Webhook не найден")

    # Поиск модуля по токену — индекса нет, делаем full scan среди is_enabled.
    # Прода на 1000 модулей это <50ms. Если масштаб >10k — добавить колонку
    # webhook_token + UNIQUE index.
    mods = (db.query(AgentModule)
              .filter(AgentModule.is_enabled.is_(True),
                      AgentModule.custom_settings_json.like(f'%"webhook_token":%{token}%'))
              .all())
    target = None
    for m in mods:
        s = _safe_json(m.custom_settings_json, {})
        if s.get("webhook_token") == token:
            target = m
            break
    if not target:
        raise HTTPException(404, "Webhook не найден")

    # Берём владельца агента
    agent = db.query(Agent).filter_by(id=target.agent_id).first()
    if not agent or agent.status != "active":
        raise HTTPException(403, "Агент не активен")
    user = db.query(User).filter_by(id=agent.user_id).first()
    if not user:
        raise HTTPException(404, "Юзер не найден")

    # Тело запроса — текст (или JSON) для модуля
    body = await request.body()
    if len(body) > _WEBHOOK_BODY_MAX:
        raise HTTPException(413, "Тело webhook слишком большое (макс 32 KB)")
    body_text = ""
    try:
        body_text = body.decode("utf-8", errors="replace")[:_WEBHOOK_BODY_MAX]
    except Exception:
        body_text = ""

    settings = _safe_json(target.custom_settings_json, {})
    base_task = (settings.get("webhook_task")
                  or settings.get("cron_task")
                  or "").strip()
    if not base_task:
        raise HTTPException(400, "У модуля не задана webhook_task / cron_task — "
                                  "нечего выполнять. Настрой задачу в UI.")
    task = base_task
    if body_text.strip():
        task = f"{base_task}\n\n=== Данные события ===\n{body_text}"

    # Биллинг + invoke (та же логика что cron)
    from server.pricing import get_price
    from server.billing import deduct_atomic
    from server.models import Transaction
    from server.agent_builder import (
        invoke_module, apply_module_memory_updates, compute_module_level,
        increment_module_interaction,
    )
    module_cost = get_price("agents.module_invoke", default=100)
    if module_cost > 0 and int(user.tokens_balance or 0) < module_cost:
        raise HTTPException(402, "Недостаточно средств на балансе агента")

    profile = _safe_json(agent.profile_json, {"facts": []})
    mod_memory = _safe_json(target.module_memory_json, {})

    try:
        inv = invoke_module(
            slug=target.slug, task=task[:8000],
            profile=profile,
            module_memory=mod_memory,
            custom_settings=settings,
            user_id=user.id,
        )
    except Exception as e:
        log.exception(f"[agents.webhook] {target.slug}: {e}")
        raise HTTPException(500, f"Модуль упал: {e!s:.140}")

    charged_kop = 0
    level_up = False
    if inv.get("ok"):
        if module_cost > 0:
            charged_kop = deduct_atomic(db, user.id, module_cost)
            if charged_kop > 0:
                db.add(Transaction(
                    user_id=user.id, type="usage",
                    tokens_delta=-charged_kop,
                    description=f"Модуль {target.slug} (webhook): {charged_kop/100:.2f} ₽",
                    model=f"agents.module:{target.slug}",
                ))
        if inv.get("memory_updates"):
            # Adaptive Prompts: scope=global → promotion в Memory Hub.
            apply_module_memory_updates(
                mod_memory, inv["memory_updates"], profile=profile
            )
            target.module_memory_json = json.dumps(mod_memory, ensure_ascii=False)
            agent.profile_json = json.dumps(profile, ensure_ascii=False)
        target.last_used_at = datetime.utcnow()
        # Атомарный SQL +1 (multi-worker safe)
        new_count = increment_module_interaction(db, target)
        learned_count = len(mod_memory.get("learned") or [])
        new_lvl = compute_module_level(
            current_level=target.level or 0,
            interaction_count=new_count,
            agent_status=agent.status,
            learned_count=learned_count,
        )
        level_up = new_lvl > (target.level or 0)
        if level_up:
            target.level = new_lvl
        content = inv["output"]
    else:
        content = f"⚠ Webhook модуля {target.slug}: {inv.get('error', 'ошибка')}"

    db.add(AgentMessage(
        agent_id=agent.id, role="tool", content=content,
        meta_json=_dump_meta({
            "mode": "webhook_invoke",
            "slug": target.slug,
            "model_used": inv.get("model_used", ""),
            "level": target.level,
            "level_up": level_up,
            "ok": bool(inv.get("ok")),
            "cost_kop": charged_kop,
        }),
    ))
    agent.last_activity_at = datetime.utcnow()
    db.commit()

    try:
        from server.audit_log import log_action
        log_action(
            "agent.webhook_invoke", user_id=user.id,
            target_type="agent_module", target_id=target.id,
            details={"slug": target.slug, "ok": bool(inv.get("ok")),
                      "cost_kop": charged_kop},
        )
    except Exception:
        pass

    return {"ok": bool(inv.get("ok")), "output": content[:4000],
             "cost_kop": charged_kop}


# ── catalog (доступные модули) ───────────────────────────────────────────────

# Категории каталога — определяются по slug (мапа надстраивается поверх
# AGENT_REGISTRY без изменения регистра). UI рисует фильтр-чипы по category.
_CATEGORY_MAP = {
    # Контент / SMM
    "smm": "content", "copywriter": "content", "scriptwriter": "content",
    "seo": "content", "designer": "content", "video": "content",
    # Маркетинг
    "marketer": "marketing", "email_marketer": "marketing", "media_buyer": "marketing",
    "brand": "marketing",
    # Документы / юридическое
    "lawyer": "docs", "accountant": "docs", "hr_docs": "docs", "kp": "docs",
    "contract": "docs",
    # Аналитика
    "analyst": "analytics", "fin_analyst": "analytics", "data_analyst": "analytics",
    "researcher": "analytics", "comp_intel": "analytics",
    # Тендеры
    "tender_parser": "tenders", "tender_analyst": "tenders", "tender_writer": "tenders",
    # Автоматизация
    "bot_tg": "automation", "bot_site": "automation", "bot_vk": "automation",
    # Личные ассистенты (для самого юзера, а не для его клиентов)
    "mail": "personal", "finance": "personal",
    "nutrition": "personal", "notes": "personal", "calendar": "personal",
    # Разработка
    "developer": "dev",
}
_CATEGORY_LABELS = {
    "content": "Контент",
    "marketing": "Маркетинг",
    "docs": "Документы",
    "analytics": "Аналитика",
    "tenders": "Тендеры",
    "automation": "Автоматизация",
    "personal": "Личный ассистент",
    "dev": "Разработка",
    "other": "Прочее",
}


def _slug_category(slug: str) -> str:
    return _CATEGORY_MAP.get(slug, "other")


@router.get("/catalog")
def get_catalog(db: Session = Depends(get_db),
                user: User = Depends(current_user)):
    """Каталог доступных модулей из AGENT_REGISTRY (48 ролей).
    Помечает уже подключённые — UI не даст подключить дважды.
    Добавляет category для фильтра-чипов в UI."""
    a = get_or_create_agent(user, db)
    connected = {m.slug for m in db.query(AgentModule).filter_by(agent_id=a.id).all()}

    from server.agent_runner import AGENT_REGISTRY
    items = []
    cat_counts: dict[str, int] = {}
    for slug, meta in sorted(AGENT_REGISTRY.items()):
        cat = _slug_category(slug)
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
        items.append({
            "slug": slug,
            "name": meta.get("name", slug),
            "description": meta.get("description", ""),
            "keywords": meta.get("keywords", []),
            "category": cat,
            "is_connected": slug in connected,
        })
    categories = [
        {"key": k, "label": _CATEGORY_LABELS.get(k, k), "count": cat_counts.get(k, 0)}
        for k in ["content", "marketing", "docs", "analytics", "tenders",
                  "automation", "personal", "dev", "other"]
        if cat_counts.get(k, 0) > 0
    ]
    return {"modules": items, "total": len(items), "categories": categories}


# ── mailboxes: подключения IMAP для модуля mail ─────────────────────────────


class MailboxConnectPayload(BaseModel):
    email: str
    password: str          # app-password (НЕ обычный пароль от аккаунта)
    label: str | None = None
    provider: str | None = None  # "yandex"/"gmail"/"mailru"/"other"; если None — autodetect
    host: str | None = None      # для "other" — указывает юзер вручную
    port: int | None = None


@router.get("/me/mailboxes")
def list_my_mailboxes(db: Session = Depends(get_db),
                     user: User = Depends(current_user)):
    """Список подключённых ящиков юзера. Password НЕ возвращаем."""
    boxes = (db.query(UserMailbox)
               .filter(UserMailbox.user_id == user.id)
               .order_by(UserMailbox.id.asc())
               .all())
    return [{
        "id": b.id,
        "provider": b.provider,
        "label": b.label,
        "email": b.email,
        "host": b.host,
        "port": b.port,
        "is_active": b.is_active,
        "last_synced_at": b.last_synced_at.isoformat() if b.last_synced_at else None,
        "last_error": b.last_error,
    } for b in boxes]


@router.post("/me/mailboxes")
async def connect_my_mailbox(payload: MailboxConnectPayload,
                              db: Session = Depends(get_db),
                              user: User = Depends(current_user)):
    """Подключить новый IMAP-ящик. Проверяет логин до сохранения."""
    if not user.is_verified:
        raise HTTPException(403, "Подтвердите email")
    allowed, reason = _rate_limit_check(user.id)
    if not allowed:
        raise HTTPException(429, reason)

    from server.mailbox_runtime import (
        verify_mailbox_connection, detect_provider, PRESETS,
    )

    email = (payload.email or "").strip().lower()
    password = (payload.password or "").strip()
    if not email or "@" not in email or not password:
        raise HTTPException(400, "Нужен корректный email и app-password")
    if len(password) < 6 or len(password) > 256:
        raise HTTPException(400, "Неверная длина app-password")

    provider = (payload.provider or detect_provider(email)).strip().lower()
    preset = PRESETS.get(provider, PRESETS["other"])
    host = (payload.host or preset.get("host") or "").strip()
    port = int(payload.port or preset.get("port") or 993)
    if not host:
        raise HTTPException(400, "Не задан IMAP host (для provider=other укажи host вручную)")

    # Лимит ящиков на юзера — 5 (без UI обоснования, защита от абуза)
    existing = db.query(UserMailbox).filter(UserMailbox.user_id == user.id).count()
    if existing >= 5:
        raise HTTPException(400, "Лимит 5 подключённых ящиков на юзера. "
                                  "Удали ненужный, чтобы подключить ещё.")

    # Дубликат? — UNIQUE(user_id, email) словит, но дадим friendly error.
    dup = (db.query(UserMailbox)
             .filter(UserMailbox.user_id == user.id,
                     UserMailbox.email == email)
             .first())
    if dup:
        raise HTTPException(400, f"Ящик {email} уже подключён")

    # Verify
    result = await verify_mailbox_connection(host, port, email, password)
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error", "не удалось подключиться")}

    box = UserMailbox(
        user_id=user.id, provider=provider, label=payload.label or None,
        email=email, host=host, port=port, password=password,
        is_active=True,
    )
    db.add(box); db.commit(); db.refresh(box)

    try:
        from server.audit_log import log_action
        log_action("agent.mailbox_connect", user_id=user.id,
                   target_type="user_mailbox", target_id=box.id,
                   details={"provider": provider, "host": host})
    except Exception:
        pass

    return {
        "ok": True,
        "mailbox": {
            "id": box.id, "provider": provider, "label": box.label,
            "email": email, "host": host, "port": port,
            "is_active": True,
            "messages_total": result.get("messages_total", 0),
        },
    }


@router.delete("/me/mailboxes/{mailbox_id}")
def delete_my_mailbox(mailbox_id: int,
                     db: Session = Depends(get_db),
                     user: User = Depends(current_user)):
    """Отключить ящик. Письма не хранятся локально — просто удаляем
    credentials. При следующем invoke модуля mail этот ящик исчезнет
    из контекста."""
    box = (db.query(UserMailbox)
             .filter(UserMailbox.id == mailbox_id,
                     UserMailbox.user_id == user.id)
             .first())
    if not box:
        raise HTTPException(404, "Ящик не найден")
    db.delete(box); db.commit()
    try:
        from server.audit_log import log_action
        log_action("agent.mailbox_disconnect", user_id=user.id,
                   target_type="user_mailbox", target_id=mailbox_id,
                   details={"email": box.email})
    except Exception:
        pass
    return {"status": "deleted", "id": mailbox_id}


@router.get("/mailbox-presets")
def get_mailbox_presets(user: User = Depends(current_user)):
    """Помощь UI: пресеты host/port для популярных провайдеров +
    ссылка на инструкцию по созданию app-password."""
    from server.mailbox_runtime import PRESETS
    return {
        key: {
            "host": meta.get("host", ""),
            "port": meta.get("port", 993),
            "help_url": meta.get("help_url"),
            "help_text": meta.get("help_text", ""),
        }
        for key, meta in PRESETS.items()
    }


# ── finance: CSV-импорт банковских выписок ──────────────────────────────────


@router.post("/me/modules/finance/import-csv")
async def import_finance_csv(file: UploadFile = File(...),
                              db: Session = Depends(get_db),
                              user: User = Depends(current_user)):
    """Загрузить CSV-выписку банка → парсинг → keyword-категоризация → сохранение.

    Поддерживает Tinkoff/Sber/Alfa/generic форматы (detect по заголовкам).
    Дедупликация по UNIQUE(user_id, date, amount_kop, description_hash):
    если юзер загрузил тот же файл дважды — дубликаты молча пропускаются.
    Без LLM (категоризация keyword-based, ~мгновенно для 1000 строк).
    """
    if not user.is_verified:
        raise HTTPException(403, "Подтвердите email")
    allowed, reason = _rate_limit_check(user.id)
    if not allowed:
        raise HTTPException(429, reason)

    a = get_or_create_agent(user, db)
    m = db.query(AgentModule).filter_by(agent_id=a.id, slug="finance").first()
    if not m or not m.is_enabled:
        raise HTTPException(404, "Модуль finance не подключён — подключи из каталога")

    # Лимит размера: 5 МБ (~50k строк типичная выписка)
    MAX_BYTES = 5 * 1024 * 1024
    content = await file.read()
    if len(content) > MAX_BYTES:
        raise HTTPException(400, f"Файл больше {MAX_BYTES//(1024*1024)} МБ — слишком крупный. "
                                  "Разбей на части или экспортируй меньший период.")
    if not content:
        raise HTTPException(400, "Файл пуст")

    from server.finance_csv import parse_csv_statement

    result = parse_csv_statement(content, file.filename or "")
    if not result["rows"]:
        return {
            "ok": False,
            "source": result["source"],
            "imported": 0,
            "errors": result["errors"] or ["Не удалось распознать ни одну транзакцию"],
        }

    # Записываем в БД. Дубликаты ловим через UNIQUE.
    imported = 0
    duplicates = 0
    errors_db: list[str] = []
    for row in result["rows"]:
        tx = FinanceTransaction(
            user_id=user.id,
            source=result["source"],
            date=row["date"],
            amount_kop=row["amount_kop"],
            currency=row["currency"],
            description=row["description"],
            category=row["category"],
            description_hash=row["description_hash"],
        )
        try:
            db.add(tx)
            db.flush()
            imported += 1
        except IntegrityError:
            db.rollback()
            duplicates += 1
        except Exception as e:
            db.rollback()
            errors_db.append(str(e)[:100])
            if len(errors_db) > 10:
                break
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(500, f"Ошибка сохранения в БД: {e!s:.140}")

    m.last_used_at = datetime.utcnow()
    db.commit()

    try:
        from server.audit_log import log_action
        log_action("agent.finance_import", user_id=user.id,
                   target_type="agent_module", target_id=m.id,
                   details={"source": result["source"], "imported": imported,
                            "duplicates": duplicates})
    except Exception:
        pass

    if imported > 0:
        # Системное сообщение в чат
        msg_text = (f"💰 Импортировано {imported} транзакций из {result['source']}. "
                    f"Дубликатов пропущено: {duplicates}.\n\n"
                    "Теперь можешь спросить меня: «куда я трачу деньги?», "
                    "«сколько потратил на еду в этом месяце?», «есть ли подписки которые я не помню?»")
        db.add(AgentMessage(
            agent_id=a.id, role="system", content=msg_text,
            meta_json=json.dumps({"mode": "finance_import",
                                  "source": result["source"],
                                  "imported": imported}, ensure_ascii=False),
        ))
        db.commit()

    return {
        "ok": True,
        "source": result["source"],
        "imported": imported,
        "duplicates": duplicates,
        "parse_errors": result["errors"],
        "db_errors": errors_db,
    }


@router.get("/me/finance/summary")
def finance_summary(db: Session = Depends(get_db),
                    user: User = Depends(current_user)):
    """Сводка финансов для UI карточки модуля. Не подгружает все транзакции
    клиенту — только агрегаты + последние 10."""
    rows = (db.query(FinanceTransaction)
              .filter(FinanceTransaction.user_id == user.id)
              .order_by(FinanceTransaction.date.desc())
              .limit(100)
              .all())
    if not rows:
        return {"total": 0, "imported": 0, "by_category": [], "last": []}

    total_in = sum(r.amount_kop for r in rows if r.amount_kop > 0)
    total_out = sum(-r.amount_kop for r in rows if r.amount_kop < 0)

    by_cat: dict[str, int] = {}
    for r in rows:
        if r.amount_kop < 0:
            c = r.category or "other"
            by_cat[c] = by_cat.get(c, 0) + (-r.amount_kop)

    from server.finance_csv import CATEGORIES
    by_category = [
        {"key": k, "label": CATEGORIES.get(k, k), "amount_kop": v}
        for k, v in sorted(by_cat.items(), key=lambda kv: -kv[1])
    ]

    last = [{
        "id": r.id,
        "date": r.date.isoformat() if r.date else None,
        "amount_kop": r.amount_kop,
        "category": r.category,
        "category_label": CATEGORIES.get(r.category or "other", r.category),
        "description": (r.description or "")[:120],
    } for r in rows[:10]]

    total = db.query(FinanceTransaction).filter(FinanceTransaction.user_id == user.id).count()

    return {
        "total": total,
        "in_kop": total_in, "out_kop": total_out,
        "balance_kop": total_in - total_out,
        "by_category": by_category,
        "last": last,
    }


@router.delete("/me/finance/transactions")
def clear_finance_transactions(db: Session = Depends(get_db),
                               user: User = Depends(current_user)):
    """Очистить ВСЕ транзакции юзера. Для случая «загрузил не тот файл,
    хочу начать с нуля». Подтверждение делает UI."""
    n = db.query(FinanceTransaction).filter(FinanceTransaction.user_id == user.id).delete()
    db.commit()
    try:
        from server.audit_log import log_action
        log_action("agent.finance_clear", user_id=user.id,
                   details={"deleted_count": n})
    except Exception:
        pass
    return {"status": "cleared", "deleted": n}
