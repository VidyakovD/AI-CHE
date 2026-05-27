"""TG → Че relay: входящие сообщения от привязанных юзеров в TG management-боте
обрабатываются той же логикой что и сообщения с веб-страницы /agents-modular.html.

Архитектура:
  TG юзер пишет «напиши пост» в @aiche_bot
    ↓
  webhook /webhook/tg-mgmt → tg_management.handle_update
    ↓
  если text НЕ начинается с / И юзер привязан → tg_che_relay.process_message
    ↓
  AgentMessage user → build_reply_personal → AgentMessage assistant
    ↓
  если build_reply_personal вернул invoke_request → invoke_module → AgentMessage tool
    ↓
  Возвращаем reply_text для отправки в TG. Если был tool — присоединяем
  отдельным TG-сообщением, чтобы юзер видел: «Че: окей, поручаю» / «Копирайтер: пост...».

Эта функция переиспользует низкоуровневые helpers (build_reply_personal,
invoke_module, apply_module_memory_updates, compute_module_level,
increment_module_interaction) — те же что вызывает HTTP-endpoint
/api/agents/me/messages в server/routes/agents_modular.py.

Не дублирует FastAPI-зависимости (Depends, HTTPException) — это чистая
функция от (db, user, text). Можно вызывать из любого места: webhook,
scheduled task, internal CLI.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

log = logging.getLogger("tg-che-relay")


# Дефолтное имя если в Agent.name пусто (обычно не бывает)
_DEFAULT_AGENT_NAME = "Че"


def _safe_json(s: str | None, default: Any) -> Any:
    try:
        return json.loads(s or "")
    except Exception:
        return default if default is not None else {}


def _dump_meta(d: dict, cap: int = 8192) -> str:
    """JSON-сериализация с cap'ом (защита от огромных meta)."""
    try:
        out = json.dumps(d, ensure_ascii=False)
        if len(out) <= cap:
            return out
        # Слишком большое — режем raw/applied/errors агрессивно
        for k in ("raw", "applied", "errors"):
            if k in d:
                v = d[k]
                if isinstance(v, str):
                    d[k] = v[:200] + "...[cut]"
                elif isinstance(v, list):
                    d[k] = v[:3]
        return json.dumps(d, ensure_ascii=False)[:cap]
    except Exception:
        return "{}"


def process_message(db: Session, user, text: str) -> dict:
    """Обработать сообщение от TG-юзера к Че.

    Возвращает:
      {
        "reply": str,             # текст от Че (всегда есть)
        "module_reply": str|None, # ответ модуля (если был invoke)
        "module_slug": str|None,  # какой модуль ответил
        "level_up": bool,         # модуль прокачался
        "new_level": int,         # новый уровень модуля (если level_up)
        "free_onboarding": bool,  # это было бесплатное онбординг-сообщение
        "cost_kop": int,          # сколько списали (сумма message + module)
        "error": str|None,        # если что-то фатально упало
      }

    Не raise'ит — все ошибки логируются и оборачиваются в reply.
    """
    from server.models import Agent, AgentMessage, AgentModule, Transaction
    from server.pricing import get_price
    from server.billing import deduct_atomic
    from server.agent_builder import (
        build_reply_personal, invoke_module,
        apply_module_memory_updates, compute_module_level,
        increment_module_interaction,
    )

    out: dict[str, Any] = {
        "reply": "",
        "module_reply": None,
        "module_slug": None,
        "level_up": False,
        "new_level": 0,
        "free_onboarding": False,
        "cost_kop": 0,
        "error": None,
    }

    text = (text or "").strip()
    if not text:
        out["error"] = "Пустое сообщение"
        out["reply"] = "Пустое сообщение — нечего обработать."
        return out
    if len(text) > 10000:
        text = text[:10000]

    # ── Найти/создать singleton-агента юзера ───────────────────────────────
    a = (db.query(Agent)
           .filter(Agent.user_id == user.id,
                   Agent.status != "archived")
           .first())
    if not a:
        # Создаём агента в онбординге — как при первом заходе на web
        a = Agent(user_id=user.id, name=_DEFAULT_AGENT_NAME,
                  status="onboarding",
                  profile_json="{}", personality_json="{}")
        db.add(a)
        db.commit()
        db.refresh(a)

    mode = "onboarding" if a.status == "onboarding" else "active"

    # ── Биллинг pre-check ────────────────────────────────────────────────
    prior_user_msgs = (db.query(AgentMessage.id)
                         .filter(AgentMessage.agent_id == a.id,
                                 AgentMessage.role == "user")
                         .count())
    free_onboarding_n = get_price("agents.onboarding_free_messages", default=5)
    is_free_onboarding = (mode == "onboarding"
                          and prior_user_msgs < free_onboarding_n)
    out["free_onboarding"] = is_free_onboarding
    base_msg_min = 0 if is_free_onboarding else get_price("agents.message", default=50)

    if base_msg_min > 0 and int(user.tokens_balance or 0) < base_msg_min:
        out["error"] = "insufficient_funds"
        out["reply"] = (f"⚠ Недостаточно средств: для одного сообщения "
                        f"нужно минимум {base_msg_min/100:.2f} ₽. "
                        f"Пополни баланс на aiche.ru → Кабинет → 📊 Пополнение.")
        return out

    # ── 1. Сохраняем user-сообщение ────────────────────────────────────
    user_msg = AgentMessage(
        agent_id=a.id, role="user", content=text,
        meta_json=json.dumps({"mode": mode, "source": "tg"},
                             ensure_ascii=False),
    )
    db.add(user_msg)
    db.flush()

    # ── 2. Готовим контекст для агента ─────────────────────────────────
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

    # ── 3. Зовём Че через build_reply_personal ──────────────────────────
    asst_meta: dict[str, Any] = {"mode": mode, "source": "tg"}
    reply = ""
    invoke_request = None
    try:
        result = build_reply_personal(
            agent_name=a.name or _DEFAULT_AGENT_NAME,
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
        if result.get("profile_changed"):
            a.profile_json = json.dumps(profile, ensure_ascii=False)
        invoke_request = result.get("invoke_request")
        # Авто-активация после онбординга
        if result.get("ready_for_active") and a.status == "onboarding":
            a.status = "active"
            asst_meta["activated"] = True
    except Exception as e:
        log.exception(f"[tg-relay] build_reply failed: {e}")
        reply = "Что-то пошло не так при обработке 😔 Попробуй ещё раз."
        asst_meta["errors"] = [str(e)[:200]]

    # ── 4. Биллинг за сообщение: real_cost × margin (×3), мин base_msg_min ──
    if base_msg_min > 0 and reply and not asst_meta.get("errors"):
        from server.pricing import calc_agent_cost_kop
        _u = (result.get("usage") if 'result' in locals() else None) or {}
        msg_cost = calc_agent_cost_kop(
            model=_u.get("model_used") or "",
            input_tokens=_u.get("input_tokens", 0),
            output_tokens=_u.get("output_tokens", 0),
            base_min_kop=base_msg_min,
        )
        charged_kop = deduct_atomic(db, user.id, msg_cost)
        if charged_kop > 0:
            db.add(Transaction(
                user_id=user.id, type="usage",
                tokens_delta=-charged_kop,
                description=(
                    f"ИИ-агент (TG): {_u.get('input_tokens',0)}→{_u.get('output_tokens',0)} ток. "
                    f"({charged_kop/100:.2f} ₽)"
                ),
                model="agents.message",
            ))
            asst_meta["cost_kop"] = charged_kop
            out["cost_kop"] += charged_kop

    asst_msg = AgentMessage(
        agent_id=a.id, role="assistant", content=reply,
        meta_json=_dump_meta(asst_meta),
    )
    db.add(asst_msg)
    db.flush()
    out["reply"] = reply

    # ── 5. Если Че попросил delegate → запускаем модуль ─────────────────
    if invoke_request and isinstance(invoke_request, dict):
        slug = invoke_request.get("slug")
        task = (invoke_request.get("task") or "").strip()
        target_mod = next((m for m in modules if m.slug == slug), None)
        module_cost = get_price("agents.module_invoke", default=100)
        if target_mod and target_mod.is_enabled and module_cost > 0 \
                and int(user.tokens_balance or 0) < module_cost:
            log.info(f"[tg-relay] skipped invoke {slug} user={user.id}: low balance")
            target_mod = None
        if target_mod and target_mod.is_enabled and task:
            try:
                mod_memory = _safe_json(target_mod.module_memory_json, {})
                mod_settings = _safe_json(target_mod.custom_settings_json, {})
                inv = invoke_module(
                    slug=slug, task=task,
                    profile=profile,
                    module_memory=mod_memory,
                    custom_settings=mod_settings,
                    user_id=user.id,
                    enabled_skills=target_mod.enabled_skills,
                )
                level_up = False
                if inv.get("ok"):
                    mod_content = inv["output"]
                    if inv.get("memory_updates"):
                        apply_module_memory_updates(
                            mod_memory, inv["memory_updates"], profile=profile
                        )
                        target_mod.module_memory_json = json.dumps(mod_memory, ensure_ascii=False)
                        a.profile_json = json.dumps(profile, ensure_ascii=False)
                    target_mod.last_used_at = datetime.utcnow()
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
                    target_mod.last_used_at = datetime.utcnow()

                module_charged_kop = 0
                if inv.get("ok") and module_cost > 0:
                    from server.pricing import calc_agent_cost_kop
                    _mu = inv.get("usage") or {}
                    real_module_cost = calc_agent_cost_kop(
                        model=_mu.get("model_used") or inv.get("model_used") or "",
                        input_tokens=_mu.get("input_tokens", 0),
                        output_tokens=_mu.get("output_tokens", 0),
                        base_min_kop=module_cost,
                    )
                    module_charged_kop = deduct_atomic(db, user.id, real_module_cost)
                    if module_charged_kop > 0:
                        db.add(Transaction(
                            user_id=user.id, type="usage",
                            tokens_delta=-module_charged_kop,
                            description=(
                                f"Модуль {slug} (TG): "
                                f"{_mu.get('input_tokens',0)}→{_mu.get('output_tokens',0)} ток. "
                                f"({module_charged_kop/100:.2f} ₽)"
                            ),
                            model=f"agents.module:{slug}",
                        ))
                        out["cost_kop"] += module_charged_kop

                module_msg = AgentMessage(
                    agent_id=a.id, role="tool", content=mod_content,
                    meta_json=_dump_meta({
                        "mode": "module_invoke",
                        "source": "tg",
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

                out["module_reply"] = mod_content
                out["module_slug"] = slug
                out["level_up"] = level_up
                out["new_level"] = target_mod.level
            except Exception as e:
                log.exception(f"[tg-relay] module {slug} failed: {e}")
                err_msg = AgentMessage(
                    agent_id=a.id, role="system",
                    content=f"⚠ Ошибка модуля {slug}: {e!s:.140}",
                )
                db.add(err_msg)

    a.last_activity_at = datetime.utcnow()
    a.updated_at = datetime.utcnow()
    try:
        db.commit()
    except Exception as e:
        log.exception(f"[tg-relay] commit failed: {e}")
        db.rollback()
        out["error"] = "commit_failed"

    return out


def format_for_tg(result: dict, agent_name: str = "Че") -> list[str]:
    """Превратить result в 1-2 TG-сообщения.

    Если был invoke модуля — отправляем 2 сообщения:
    1. Че (короткое подтверждение «окей, поручаю»)
    2. Модуль (длинный ответ с level-up бейджем если был)

    HTML-форматирование для TG (parse_mode=HTML).
    """
    parts: list[str] = []

    # Сообщение от Че
    reply = (result.get("reply") or "").strip()
    if reply:
        # Экранируем HTML (TG parse_mode=HTML понимает < > & как теги)
        safe_reply = (reply.replace("&", "&amp;")
                            .replace("<", "&lt;")
                            .replace(">", "&gt;"))
        parts.append(safe_reply)

    # Сообщение от модуля
    mod_reply = (result.get("module_reply") or "").strip()
    mod_slug = result.get("module_slug")
    if mod_reply and mod_slug:
        # Заголовок модуля + уровень
        level = result.get("new_level", 0)
        level_label = f"L{level}"
        level_up = result.get("level_up", False)
        header = f"🧩 <b>{mod_slug}</b> · {level_label}"
        if level_up:
            header += f" ⬆ прокачался до {level_label}!"
        safe_mod = (mod_reply.replace("&", "&amp;")
                              .replace("<", "&lt;")
                              .replace(">", "&gt;"))
        parts.append(f"{header}\n\n{safe_mod}")

    return parts or ["(пустой ответ)"]
