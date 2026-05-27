"""Чат-виджет на опубликованном сайте: подключение, конфигурация, runtime.

Два режима:
  - relay: сообщения посетителей сайта → личный TG/MAX юзера, он отвечает сам.
  - ai:    подключён AI-агент с предобучением, реальный LLM real_cost × margin.

Endpoints (управление — для владельца сайта):
  POST   /sites/projects/{id}/widget       — создать/обновить виджет (mode, config)
  GET    /sites/projects/{id}/widget       — текущее состояние
  DELETE /sites/projects/{id}/widget       — отключить виджет
  GET    /sites/projects/{id}/widget/messages — диалоги посетителей

Endpoints (runtime — для встроенного JS на hosted сайте):
  GET    /sites/widget/{public_token}/config — public конфигурация виджета
  POST   /sites/widget/{public_token}/message — посетитель пишет сообщение
  GET    /sites/widget/{public_token}/poll?since=N — для relay-режима, poll новых ответов
  GET    /sites/widget/{public_token}/embed.js — embed-скрипт

Биллинг ai-режима: real_token_cost × ai.reply_margin_pct (×3), минимум
agents.message=50 коп. Списывается со счёта ВЛАДЕЛЬЦА сайта.
"""
from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Header
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from server.routes.deps import get_db, current_user
from server.models import (User, SiteProject, SiteChatWidget,
                            SiteChatMessage, Transaction, Agent, AgentModule)

log = logging.getLogger(__name__)
router = APIRouter(tags=["site-widget"])

_VISITOR_TOKEN_LEN = 16
_MAX_MSG_LEN = 4000
_MAX_HISTORY = 50


# ── Helpers ──────────────────────────────────────────────────────────────────


def _widget_dict(w: SiteChatWidget) -> dict:
    """Сериализация виджета для админ-UI."""
    cfg = {}
    try:
        cfg = json.loads(w.config_json) if w.config_json else {}
    except Exception:
        cfg = {}
    return {
        "id": w.id,
        "site_id": w.site_id,
        "mode": w.mode,
        "is_active": bool(w.is_active),
        "config": cfg,
        "agent_module_id": w.agent_module_id,
        "total_messages": int(w.total_messages or 0),
        "last_message_at": w.last_message_at.isoformat() if w.last_message_at else None,
        "created_at": w.created_at.isoformat() if w.created_at else None,
    }


def _public_widget_config(w: SiteChatWidget) -> dict:
    """Только то что безопасно отдать посетителям сайта."""
    cfg = {}
    try:
        cfg = json.loads(w.config_json) if w.config_json else {}
    except Exception:
        cfg = {}
    return {
        "mode": w.mode,
        "is_active": bool(w.is_active),
        "welcome_text": cfg.get("welcome_text") or "Здравствуйте! Чем могу помочь?",
        "color": cfg.get("color") or "#ff8c42",
        "name": cfg.get("name") or "Чат",
    }


def _site_by_token(token: str, db: Session) -> SiteProject | None:
    if not token or len(token) < 8 or len(token) > 80:
        return None
    return db.query(SiteProject).filter_by(public_token=token).first()


def _new_visitor_token() -> str:
    return secrets.token_urlsafe(_VISITOR_TOKEN_LEN)[:_VISITOR_TOKEN_LEN]


# ── Управление виджетом (владелец сайта) ─────────────────────────────────────


class WidgetUpsertPayload(BaseModel):
    mode: str  # relay / ai
    welcome_text: str | None = None
    name: str | None = None  # display-имя виджета («Менеджер Алина»)
    color: str | None = None  # accent #hex для UI
    # для relay
    tg_chat_id: str | None = None
    max_user_id: str | None = None
    hours: str | None = None  # «9-21 МСК» — информационно для посетителя
    # для ai
    system_prompt: str | None = None
    faq: str | None = None


@router.post("/sites/projects/{project_id}/widget")
def upsert_widget(project_id: int, payload: WidgetUpsertPayload,
                   db: Session = Depends(get_db),
                   user: User = Depends(current_user)):
    """Создать или обновить чат-виджет на сайте.

    Создаёт SiteChatWidget. Если mode='ai' — также создаёт скрытый
    AgentModule под слугом 'site_chatbot_{site_id}' для биллинга/прокачки.
    """
    if not user.is_verified:
        raise HTTPException(403, "Подтвердите email")
    p = db.query(SiteProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    if not p.public_token:
        raise HTTPException(400, "Сайт не опубликован — сначала нажмите «Опубликовать»")

    mode = (payload.mode or "").strip().lower()
    if mode not in ("relay", "ai"):
        raise HTTPException(400, "mode должен быть 'relay' или 'ai'")

    # Валидация в зависимости от режима
    if mode == "relay":
        if not (payload.tg_chat_id or payload.max_user_id):
            raise HTTPException(400,
                "Для relay-режима укажи tg_chat_id или max_user_id — куда "
                "пересылать сообщения посетителей. Получи свой chat_id через "
                "@userinfobot в Telegram.")
    if mode == "ai":
        if not (payload.system_prompt and payload.system_prompt.strip()):
            raise HTTPException(400,
                "Для AI-режима укажи system_prompt (роль и стиль бота: "
                "«Ты менеджер кофейни, отвечай тёплым тоном, фокусируй на "
                "записи на дегустацию»).")

    config = {
        "welcome_text": (payload.welcome_text or "").strip()[:500] or
                         ("Здравствуйте! Чем могу помочь?" if mode == "ai"
                          else "Напишите сюда, мы ответим в TG"),
        "name": (payload.name or "").strip()[:80] or "Чат с менеджером",
        "color": (payload.color or "").strip()[:20] or "#ff8c42",
    }
    if mode == "relay":
        if payload.tg_chat_id:
            config["tg_chat_id"] = payload.tg_chat_id.strip()[:50]
        if payload.max_user_id:
            config["max_user_id"] = payload.max_user_id.strip()[:50]
        if payload.hours:
            config["hours"] = payload.hours.strip()[:80]
    else:  # ai
        config["system_prompt"] = payload.system_prompt.strip()[:4000]
        if payload.faq:
            config["faq"] = payload.faq.strip()[:8000]

    existing = db.query(SiteChatWidget).filter_by(site_id=p.id).first()
    if existing:
        existing.mode = mode
        existing.is_active = True
        existing.config_json = json.dumps(config, ensure_ascii=False)
        existing.updated_at = datetime.utcnow()
        w = existing
    else:
        w = SiteChatWidget(
            site_id=p.id, user_id=user.id,
            mode=mode, is_active=True,
            config_json=json.dumps(config, ensure_ascii=False),
        )
        db.add(w)
    db.flush()

    # AI-режим: создаём AgentModule чтобы юзер видел/мог редактировать в /agents-modular
    if mode == "ai":
        # Регистрируем «виртуальный» slug для этого сайта в реестре runtime'а
        _ensure_site_chatbot_registered(p.id, config)
        # Найдём агента юзера (singleton)
        a = db.query(Agent).filter_by(user_id=user.id).first()
        if a:
            slug = f"site_chatbot_{p.id}"
            am = db.query(AgentModule).filter_by(agent_id=a.id, slug=slug).first()
            if not am:
                am = AgentModule(
                    agent_id=a.id, slug=slug, level=0, is_enabled=True,
                    custom_settings_json=json.dumps({
                        "site_id": p.id,
                        "site_name": p.name,
                    }, ensure_ascii=False),
                )
                db.add(am); db.flush()
            w.agent_module_id = am.id

    db.commit(); db.refresh(w)

    # Перезаписываем index.html опубликованного сайта чтобы embed.js появился сразу
    try:
        from server.routes.sites import _rewrite_hosted_index_html
        _rewrite_hosted_index_html(p.id, db)
    except Exception as e:
        log.warning(f"[widget] rewrite hosted index failed: {e}")

    try:
        from server.audit_log import log_action
        log_action("site.widget_upsert", user_id=user.id, target_type="site",
                   target_id=str(p.id), details={"mode": mode})
    except Exception:
        pass

    return _widget_dict(w)


@router.get("/sites/projects/{project_id}/widget")
def get_widget(project_id: int,
               db: Session = Depends(get_db),
               user: User = Depends(current_user)):
    """Текущее состояние виджета (или null если не подключён)."""
    p = db.query(SiteProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    w = db.query(SiteChatWidget).filter_by(site_id=p.id).first()
    if not w:
        return {"widget": None}
    return {"widget": _widget_dict(w)}


@router.delete("/sites/projects/{project_id}/widget")
def disable_widget(project_id: int,
                   db: Session = Depends(get_db),
                   user: User = Depends(current_user)):
    """Отключить виджет (soft — is_active=False, диалоги остаются)."""
    p = db.query(SiteProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    w = db.query(SiteChatWidget).filter_by(site_id=p.id).first()
    if not w:
        return {"status": "no_widget"}
    w.is_active = False
    db.commit()
    try:
        from server.routes.sites import _rewrite_hosted_index_html
        _rewrite_hosted_index_html(p.id, db)
    except Exception as e:
        log.warning(f"[widget] rewrite on disable failed: {e}")
    return {"status": "disabled"}


@router.get("/sites/projects/{project_id}/widget/messages")
def list_widget_messages(project_id: int, limit: int = 50,
                         db: Session = Depends(get_db),
                         user: User = Depends(current_user)):
    """История сообщений виджета (для админ-UI)."""
    p = db.query(SiteProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    w = db.query(SiteChatWidget).filter_by(site_id=p.id).first()
    if not w:
        return {"messages": []}
    limit = max(1, min(limit, 200))
    msgs = (db.query(SiteChatMessage)
              .filter_by(widget_id=w.id)
              .order_by(SiteChatMessage.id.desc())
              .limit(limit).all())
    return {
        "messages": [{
            "id": m.id, "visitor_id": m.visitor_id, "role": m.role,
            "content": m.content, "cost_kop": m.cost_kop,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        } for m in reversed(msgs)],
    }


# ── Runtime: посетители сайта ────────────────────────────────────────────────


@router.get("/sites/widget/{public_token}/config")
def widget_config(public_token: str, db: Session = Depends(get_db)):
    """Конфигурация виджета для встроенного JS. Без авторизации."""
    p = _site_by_token(public_token, db)
    if not p:
        raise HTTPException(404, "Сайт не найден")
    w = db.query(SiteChatWidget).filter_by(site_id=p.id, is_active=True).first()
    if not w:
        return {"active": False}
    return {"active": True, **_public_widget_config(w)}


class WidgetMessagePayload(BaseModel):
    text: str
    visitor_id: str | None = None


@router.post("/sites/widget/{public_token}/message")
async def widget_message(public_token: str,
                          payload: WidgetMessagePayload,
                          request: Request,
                          db: Session = Depends(get_db)):
    """Посетитель отправил сообщение в чат сайта.

    relay-режим: пушим в TG/MAX юзера, возвращаем заглушку «отправлено».
    ai-режим: вызываем LLM с system_prompt + историей диалога этого визитера,
              возвращаем ответ, списываем real×3 с юзера-владельца.
    """
    p = _site_by_token(public_token, db)
    if not p:
        raise HTTPException(404, "Сайт не найден")
    w = db.query(SiteChatWidget).filter_by(site_id=p.id, is_active=True).first()
    if not w:
        raise HTTPException(404, "Виджет не активен")
    text = (payload.text or "").strip()[:_MAX_MSG_LEN]
    if not text:
        raise HTTPException(400, "Пустое сообщение")

    visitor_id = (payload.visitor_id or "").strip()[:32] or _new_visitor_token()

    # Простая защита от спама: max 30 сообщений / 5 минут с одного visitor_id.
    recent_n = (db.query(SiteChatMessage.id)
                  .filter(SiteChatMessage.widget_id == w.id,
                          SiteChatMessage.visitor_id == visitor_id,
                          SiteChatMessage.role == "user",
                          SiteChatMessage.created_at >
                              datetime.utcnow() - timedelta(minutes=5))
                  .count())
    if recent_n >= 30:
        raise HTTPException(429, "Слишком много сообщений. Подождите 5 минут.")

    # Сохраняем сообщение посетителя
    user_msg = SiteChatMessage(
        widget_id=w.id, visitor_id=visitor_id,
        role="user", content=text, cost_kop=0,
    )
    db.add(user_msg)
    w.total_messages = (w.total_messages or 0) + 1
    w.last_message_at = datetime.utcnow()
    db.commit(); db.refresh(user_msg)

    cfg = {}
    try:
        cfg = json.loads(w.config_json) if w.config_json else {}
    except Exception:
        cfg = {}

    if w.mode == "relay":
        # Пушим в TG/MAX владельца
        return await _relay_to_owner(w, cfg, visitor_id, text, db)
    elif w.mode == "ai":
        # AI-ответ с биллингом
        return _ai_reply(w, cfg, visitor_id, text, db)
    else:
        raise HTTPException(500, "Неизвестный режим виджета")


async def _relay_to_owner(w: SiteChatWidget, cfg: dict, visitor_id: str,
                            text: str, db: Session) -> dict:
    """Шлёт сообщение в TG/MAX владельца сайта."""
    site = db.query(SiteProject).filter_by(id=w.site_id).first()
    owner = db.query(User).filter_by(id=w.user_id).first()
    if not site or not owner:
        return {"status": "queued", "visitor_id": visitor_id}

    delivered = False
    notify_text = (
        f"🌐 Сайт «{(site.name or 'без названия')[:60]}»\n"
        f"Посетитель ({visitor_id[:6]}): {text[:1000]}"
    )
    # Пытаемся через personal_tg_bot_token (свой бот юзера)
    tg_chat_id = cfg.get("tg_chat_id")
    if tg_chat_id and owner.personal_tg_bot_token:
        try:
            from server.personal_bot_relay import tg_send_message
            delivered = await tg_send_message(
                owner.personal_tg_bot_token, tg_chat_id, notify_text,
                parse_mode="HTML",
            )
        except Exception as e:
            log.warning(f"[widget-relay] TG send failed: {e}")
    # TODO MAX-доставка аналогично через personal_max_bot_token

    # Сохраняем system-msg с факт-доставки
    sys_note = "✓ доставлено владельцу в TG" if delivered else "⌛ ожидает ответа владельца"
    db.add(SiteChatMessage(
        widget_id=w.id, visitor_id=visitor_id,
        role="system", content=sys_note,
    ))
    db.commit()
    return {
        "status": "queued", "visitor_id": visitor_id,
        "reply": "Спасибо! Менеджер свяжется в ближайшее время.",
    }


def _ai_reply(w: SiteChatWidget, cfg: dict, visitor_id: str,
               text: str, db: Session) -> dict:
    """AI-режим: LLM с system_prompt + история визитера. Списание real×3."""
    from server.pricing import calc_agent_cost_kop, get_price
    from server.billing import deduct_atomic
    from server.ai import generate_response

    owner = db.query(User).filter_by(id=w.user_id).first()
    if not owner:
        raise HTTPException(404, "Владелец не найден")

    base_min = get_price("agents.message", default=50)
    if int(owner.tokens_balance or 0) < base_min:
        # Владельцу нужно пополнить. Посетитель получает вежливое сообщение.
        return {
            "status": "out_of_funds",
            "visitor_id": visitor_id,
            "reply": "К сожалению, чат временно недоступен. "
                     "Свяжитесь с менеджером другим способом.",
        }

    # История диалога этого visitor (последние 10 пар user/assistant)
    history = (db.query(SiteChatMessage)
                 .filter(SiteChatMessage.widget_id == w.id,
                         SiteChatMessage.visitor_id == visitor_id,
                         SiteChatMessage.role.in_(("user", "assistant")))
                 .order_by(SiteChatMessage.id.desc())
                 .limit(20).all())
    # reverse + skip только что добавленное user-сообщение (последнее)
    history = list(reversed(history))[:-1]

    system_prompt = (cfg.get("system_prompt") or "").strip()
    faq = (cfg.get("faq") or "").strip()
    name = cfg.get("name") or "Менеджер"
    full_system = f"""Ты — {name}, чат-бот на сайте.

{system_prompt}

ВАЖНО:
- Отвечай по делу, не лей воды.
- Не упоминай что ты AI — представляйся ролью.
- Не уводи разговор на политику/религию/общие темы.
- При сложных вопросах или жалобах — предложи связаться с менеджером
  напрямую (телефон / TG / email — если есть в FAQ).
"""
    if faq:
        full_system += f"\n═══ ИНФОРМАЦИЯ ДЛЯ ОТВЕТОВ (FAQ) ═══\n{faq[:6000]}"

    messages = [{"role": "system", "content": full_system}]
    for m in history:
        messages.append({"role": m.role, "content": m.content})
    messages.append({"role": "user", "content": text})

    try:
        result = generate_response(
            "claude-haiku", messages,
            extra={"max_tokens": 800, "temperature": 0.5,
                   "_purpose": f"site_widget:{w.site_id}",
                   "_user_id": owner.id},
        )
        reply_text = (result.get("content") if isinstance(result, dict) else "") or \
                     "Извините, не могу ответить сейчас. Попробуйте позже."
    except Exception as e:
        log.exception(f"[widget-ai] LLM failed: {e}")
        return {"status": "error", "visitor_id": visitor_id,
                "reply": "Извините, временно недоступен."}

    in_tok = int(result.get("input_tokens", 0) or 0) if isinstance(result, dict) else 0
    out_tok = int(result.get("output_tokens", 0) or 0) if isinstance(result, dict) else 0
    model_used = result.get("model") if isinstance(result, dict) else "claude-haiku-4-5-20251001"

    cost = calc_agent_cost_kop(
        model=model_used or "",
        input_tokens=in_tok, output_tokens=out_tok,
        base_min_kop=base_min,
        alt_model="claude-haiku",
    )
    charged = deduct_atomic(db, owner.id, cost)
    if charged > 0:
        db.add(Transaction(
            user_id=owner.id, type="usage",
            tokens_delta=-charged,
            description=(
                f"Виджет сайта #{w.site_id}: {in_tok}→{out_tok} ток. "
                f"({charged/100:.2f} ₽)"
            ),
            model=f"site_widget:{w.site_id}",
        ))

    # Сохраняем ответ
    db.add(SiteChatMessage(
        widget_id=w.id, visitor_id=visitor_id,
        role="assistant", content=reply_text[:_MAX_MSG_LEN], cost_kop=charged,
    ))
    db.commit()

    return {
        "status": "ok",
        "visitor_id": visitor_id,
        "reply": reply_text[:_MAX_MSG_LEN],
        "cost_kop": charged,
    }


@router.get("/sites/widget/{public_token}/poll")
def widget_poll(public_token: str, visitor_id: str, since: int = 0,
                db: Session = Depends(get_db)):
    """Poll новых ответов для relay-режима (юзер ответил в TG → push сюда).

    MVP: возвращает все сообщения widget'а для этого visitor с id > since.
    """
    p = _site_by_token(public_token, db)
    if not p:
        raise HTTPException(404, "Сайт не найден")
    w = db.query(SiteChatWidget).filter_by(site_id=p.id, is_active=True).first()
    if not w:
        return {"messages": []}
    visitor_id = (visitor_id or "").strip()[:32]
    if not visitor_id:
        return {"messages": []}
    msgs = (db.query(SiteChatMessage)
              .filter(SiteChatMessage.widget_id == w.id,
                      SiteChatMessage.visitor_id == visitor_id,
                      SiteChatMessage.id > since)
              .order_by(SiteChatMessage.id.asc())
              .limit(50).all())
    return {
        "messages": [{
            "id": m.id, "role": m.role,
            "content": m.content,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        } for m in msgs],
    }


@router.get("/sites/widget/{public_token}/embed.js")
def widget_embed_js(public_token: str, db: Session = Depends(get_db)):
    """Embed-скрипт виджета. Юзер вставляет в HTML сайта:
        <script async src="https://aiche.ru/sites/widget/<token>/embed.js"></script>
    """
    p = _site_by_token(public_token, db)
    if not p:
        # Возвращаем no-op чтобы не ломать страницу
        return Response("// site not found", media_type="application/javascript")

    js = _EMBED_JS_TEMPLATE.replace("__TOKEN__", public_token)
    return Response(js, media_type="application/javascript",
                    headers={"Cache-Control": "public, max-age=300"})


def _ensure_site_chatbot_registered(site_id: int, config: dict) -> None:
    """Зарегистрировать виртуальный модуль site_chatbot_{id} в AGENT_REGISTRY.

    Это позволяет юзеру в /agents-modular видеть свой сайт-чатбот как модуль
    и редактировать его настройки через стандартный UI (системный промпт,
    FAQ — через custom_settings). На рантайме invoke этого модуля = ai_reply.
    """
    try:
        from server.agent_runner import register_agent, AGENT_REGISTRY
        slug = f"site_chatbot_{site_id}"
        if slug in AGENT_REGISTRY:
            return
        register_agent(
            agent_id=slug,
            name=f"🌐 Чат-бот на сайте #{site_id}",
            description=(f"AI-бот на твоём опубликованном сайте. "
                          f"Отвечает посетителям по предобучению ({config.get('name', '')[:40]})."),
            keywords=[f"сайт {site_id}", "виджет", "чат бот сайта"],
            system_prompt=config.get("system_prompt") or "Ты чат-бот на сайте.",
            allowed_tools=["run_llm", "write_output", "finish"],
        )
    except Exception as e:
        log.warning(f"[widget] register_agent failed: {e}")


# JS-template embed-скрипта. Минимальный плавающий виджет.
_EMBED_JS_TEMPLATE = r"""
(function(){
  if(window.__aicheWidget) return;
  window.__aicheWidget = true;
  var TOKEN = "__TOKEN__";
  var BASE = (function(){ try { return new URL(document.currentScript.src).origin; } catch(_) { return ""; } })();
  var visitorId = localStorage.getItem("aiche_visitor_id");
  if(!visitorId){ visitorId = Math.random().toString(36).slice(2,18); localStorage.setItem("aiche_visitor_id", visitorId); }
  var lastId = 0;

  function el(tag, attrs, children){
    var e = document.createElement(tag);
    Object.keys(attrs||{}).forEach(function(k){
      if(k === "style") Object.assign(e.style, attrs[k]);
      else if(k.startsWith("on")) e[k.toLowerCase()] = attrs[k];
      else e.setAttribute(k, attrs[k]);
    });
    (children||[]).forEach(function(c){
      e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return e;
  }

  fetch(BASE + "/sites/widget/" + TOKEN + "/config")
    .then(function(r){ return r.json(); })
    .then(function(cfg){
      if(!cfg || !cfg.active) return;
      var color = cfg.color || "#ff8c42";
      var btn = el("div", {style:{
        position:"fixed", right:"20px", bottom:"20px", width:"60px", height:"60px",
        borderRadius:"50%", background:color, color:"#fff", fontSize:"28px",
        display:"flex", alignItems:"center", justifyContent:"center",
        cursor:"pointer", boxShadow:"0 4px 16px rgba(0,0,0,.25)", zIndex:"99999",
      }}, ["💬"]);

      var panel = el("div", {style:{
        position:"fixed", right:"20px", bottom:"90px", width:"340px", maxWidth:"92vw",
        height:"460px", maxHeight:"80vh", background:"#fff", borderRadius:"14px",
        boxShadow:"0 8px 32px rgba(0,0,0,.25)", display:"none",
        flexDirection:"column", overflow:"hidden", zIndex:"99999",
        fontFamily:"system-ui,Segoe UI,Roboto,sans-serif", color:"#222",
      }});
      var header = el("div", {style:{
        background:color, color:"#fff", padding:"12px 16px", fontWeight:"600",
        display:"flex", justifyContent:"space-between", alignItems:"center",
      }}, [cfg.name || "Чат", el("span", {style:{cursor:"pointer", fontSize:"20px"}, onclick:function(){panel.style.display="none";}}, ["✕"])]);
      var body = el("div", {style:{
        flex:"1", padding:"12px", overflowY:"auto", background:"#f7f6f3",
        fontSize:"14px", lineHeight:"1.45",
      }});
      function addMsg(role, text){
        var bubble = el("div", {style:{
          display:"inline-block", padding:"8px 12px", borderRadius:"12px",
          background: role==="user" ? color : "#fff",
          color: role==="user" ? "#fff" : "#222",
          maxWidth:"82%", margin:"4px 0", whiteSpace:"pre-wrap",
          border: role==="user" ? "none" : "1px solid #e2dfd8",
        }}, [text]);
        var wrap = el("div", {style:{textAlign: role==="user" ? "right" : "left"}}, [bubble]);
        body.appendChild(wrap);
        body.scrollTop = body.scrollHeight;
      }
      if(cfg.welcome_text) addMsg("assistant", cfg.welcome_text);

      var inputRow = el("div", {style:{display:"flex", borderTop:"1px solid #e2dfd8", padding:"8px", gap:"6px", background:"#fff"}});
      var ta = el("textarea", {placeholder:"Сообщение...", style:{
        flex:"1", border:"1px solid #d8d4cc", borderRadius:"8px", padding:"8px",
        resize:"none", fontFamily:"inherit", fontSize:"14px", height:"40px",
      }});
      var sendBtn = el("button", {style:{
        background:color, color:"#fff", border:"none", borderRadius:"8px",
        padding:"0 16px", cursor:"pointer", fontSize:"14px",
      }, onclick:send}, ["→"]);
      inputRow.appendChild(ta); inputRow.appendChild(sendBtn);

      panel.appendChild(header); panel.appendChild(body); panel.appendChild(inputRow);
      btn.onclick = function(){ panel.style.display = panel.style.display==="flex" ? "none" : "flex"; };
      document.body.appendChild(btn); document.body.appendChild(panel);

      ta.addEventListener("keydown", function(e){
        if(e.key==="Enter" && !e.shiftKey){ e.preventDefault(); send(); }
      });

      function send(){
        var text = (ta.value||"").trim();
        if(!text) return;
        addMsg("user", text);
        ta.value = "";
        fetch(BASE + "/sites/widget/" + TOKEN + "/message", {
          method:"POST", headers:{"Content-Type":"application/json"},
          body: JSON.stringify({text: text, visitor_id: visitorId}),
        }).then(function(r){ return r.json(); })
          .then(function(d){
            if(d && d.reply) addMsg("assistant", d.reply);
            if(d && d.status === "out_of_funds") addMsg("system", "(Чат временно недоступен)");
          })
          .catch(function(){ addMsg("system", "Ошибка сети, попробуйте ещё раз."); });
      }

      // Для relay-режима — периодический poll новых ответов из TG
      if(cfg.mode === "relay"){
        setInterval(function(){
          fetch(BASE + "/sites/widget/" + TOKEN + "/poll?visitor_id=" + encodeURIComponent(visitorId) + "&since=" + lastId)
            .then(function(r){ return r.json(); })
            .then(function(d){
              (d.messages||[]).forEach(function(m){
                if(m.id > lastId) lastId = m.id;
                if(m.role === "assistant") addMsg("assistant", m.content);
              });
            }).catch(function(){});
        }, 15000);
      }
    });
})();
"""
