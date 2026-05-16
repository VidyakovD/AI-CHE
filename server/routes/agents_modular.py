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

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from server.routes.deps import get_db, current_user
from server.models import User, Agent, AgentMessage, AgentModule

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
                    # Применяем выученное модулем к его памяти
                    if inv.get("memory_updates"):
                        apply_module_memory_updates(mod_memory, inv["memory_updates"])
                        target_mod.module_memory_json = json.dumps(mod_memory, ensure_ascii=False)
                    # Прокачка считается только за успешные вызовы.
                    # Фейлы (LLM-error / refusal) не должны прокачивать модуль.
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
    return _module_dict(m, with_meta=True)


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
                  "automation", "dev", "other"]
        if cat_counts.get(k, 0) > 0
    ]
    return {"modules": items, "total": len(items), "categories": categories}
