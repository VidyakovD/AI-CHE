"""CRUD API для постоянных чат-ботов."""
import os, logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from server.routes.deps import get_db, current_user
from server.models import ChatBot, User
from server.chatbot_engine import (
    setup_telegram_webhook, delete_telegram_webhook,
    setup_max_webhook, delete_max_webhook, get_max_me,
    set_telegram_commands,
    get_summary, generate_widget_secret,
)


# Базовый набор команд для меню «/» в TG. Подставляется при деплое любого
# TG-бота — юзер сразу видит подсказки `/start /help /menu` в меню рядом с input.
DEFAULT_TG_COMMANDS = [
    {"command": "start", "description": "Начать работу с ботом"},
    {"command": "help", "description": "Помощь и список команд"},
    {"command": "menu", "description": "Главное меню"},
]

log = logging.getLogger("chatbots")
router = APIRouter(prefix="/chatbots", tags=["chatbots"])

APP_URL = os.getenv("APP_URL", "https://aiche.ru")


class BotCreate(BaseModel):
    name: str = "Мой бот"
    model: str = "gpt"
    system_prompt: str | None = None
    workflow_json: str | None = None   # JSON граф из конструктора
    tg_token: str | None = None
    vk_token: str | None = None
    vk_group_id: str | None = None
    avito_client_id: str | None = None
    avito_client_secret: str | None = None
    avito_user_id: str | None = None
    max_token: str | None = None
    widget_enabled: bool = False
    widget_allowed_origins: str | None = None
    max_replies_day: int = 100
    cost_per_reply: int = 5


class BotUpdate(BaseModel):
    name: str | None = None
    model: str | None = None
    system_prompt: str | None = None
    workflow_json: str | None = None
    tg_token: str | None = None
    vk_token: str | None = None
    vk_group_id: str | None = None
    avito_client_id: str | None = None
    avito_client_secret: str | None = None
    avito_user_id: str | None = None
    max_token: str | None = None
    widget_enabled: bool | None = None
    widget_allowed_origins: str | None = None
    max_replies_day: int | None = None
    cost_per_reply: int | None = None


async def _fetch_tg_username(tg_token: str) -> str | None:
    """Вызывает Telegram getMe чтобы узнать @username бота."""
    try:
        import httpx as _hx
        async with _hx.AsyncClient(timeout=10) as c:
            r = await c.get(f"https://api.telegram.org/bot{tg_token}/getMe")
            data = r.json()
            if data.get("ok"):
                return data.get("result", {}).get("username")
    except Exception:
        pass
    return None


async def _auto_setup_channels(bot: ChatBot) -> dict:
    """
    Автоматически настраивает webhook'и для всех каналов где есть креды.
    Возвращает dict со статусами по каналам. Идемпотентно — можно звать каждый save.
    """
    out = {}
    if bot.tg_token:
        wh_url = f"{APP_URL}/webhook/tg/{bot.id}"
        try:
            r = await setup_telegram_webhook(bot.tg_token, wh_url)
            bot.tg_webhook_set = bool(r.get("ok"))
            username = await _fetch_tg_username(bot.tg_token)
            # Сразу выставляем меню команд — юзер видит /start /help /menu рядом с input.
            try:
                await set_telegram_commands(bot.tg_token, DEFAULT_TG_COMMANDS)
            except Exception as e:
                log.warning(f"[Bot {bot.id}] setMyCommands failed: {e}")
            out["telegram"] = {
                "ok": bot.tg_webhook_set,
                "detail": r.get("description", ""),
                "username": username,
                "url": f"https://t.me/{username}" if username else None,
            }
        except Exception as e:
            log.error(f"[Bot {bot.id}] TG setup failed: {e}")
            out["telegram"] = {"ok": False, "detail": str(e)}
    if bot.vk_token and bot.vk_group_id:
        # Авто-получение confirmation code из VK API (groups.getCallbackConfirmationCode)
        try:
            import httpx as _hx
            async with _hx.AsyncClient(timeout=10) as c:
                r = await c.get(
                    "https://api.vk.com/method/groups.getCallbackConfirmationCode",
                    params={"group_id": bot.vk_group_id.lstrip("-"),
                            "access_token": bot.vk_token, "v": "5.131"},
                )
                data = r.json()
                code = (data.get("response") or {}).get("code")
                if code:
                    bot.vk_confirmation = code
        except Exception as e:
            log.warning(f"[Bot {bot.id}] VK confirmation fetch failed: {e}")
        out["vk"] = {"ok": True, "callback_url": f"{APP_URL}/webhook/vk/{bot.id}",
                     "confirmation_code": bot.vk_confirmation,
                     "hint": "Укажите Callback URL в настройках группы VK"}
    if bot.avito_client_id and bot.avito_client_secret:
        out["avito"] = {"ok": True, "webhook_url": f"{APP_URL}/webhook/avito/{bot.id}"}
    if bot.max_token:
        wh_url = f"{APP_URL}/webhook/max/{bot.id}"
        try:
            r = await setup_max_webhook(bot.max_token, wh_url)
            bot.max_webhook_set = bool(r.get("ok"))
            me = await get_max_me(bot.max_token)
            uname = me.get("username") or me.get("name") or ""
            out["max"] = {
                "ok": bot.max_webhook_set,
                "detail": r.get("description", ""),
                "username": uname,
                "url": f"https://max.ru/{uname}" if uname else None,
                "webhook_url": wh_url,
            }
        except Exception as e:
            log.error(f"[Bot {bot.id}] MAX setup failed: {e}")
            out["max"] = {"ok": False, "detail": str(e)}
    if bot.widget_enabled:
        if not bot.widget_secret:
            bot.widget_secret = generate_widget_secret()
        out["widget"] = {"ok": True, "url": f"{APP_URL}/widget/{bot.id}.js"}
    return out


def _has_any_channel(bot: ChatBot) -> bool:
    return bool(
        bot.tg_token
        or (bot.vk_token and bot.vk_group_id)
        or (bot.avito_client_id and bot.avito_client_secret)
        or bot.max_token
        or bot.widget_enabled
    )


def _bot_dict(b: ChatBot) -> dict:
    return {
        "id": b.id, "name": b.name, "model": b.model,
        "system_prompt": b.system_prompt,
        "tg_token": b.tg_token[:8] + "..." if b.tg_token else None,
        "tg_token_set": bool(b.tg_token),
        "tg_webhook_set": b.tg_webhook_set,
        "vk_token_set": bool(b.vk_token),
        "vk_group_id": b.vk_group_id,
        "vk_confirmed": b.vk_confirmed,
        "avito_set": bool(b.avito_client_id and b.avito_client_secret),
        "avito_user_id": b.avito_user_id,
        "max_token_set": bool(b.max_token),
        "max_webhook_set": bool(b.max_webhook_set),
        "widget_enabled": b.widget_enabled,
        "widget_url": f"{APP_URL}/widget/{b.id}.js" if b.widget_enabled else None,
        "widget_allowed_origins": getattr(b, "widget_allowed_origins", None) or "",
        "has_workflow": bool(b.workflow_json),
        "max_replies_day": b.max_replies_day,
        "cost_per_reply": b.cost_per_reply,
        "replies_today": b.replies_today or 0,
        "status": b.status,
        "auto_generated": bool(getattr(b, "auto_generated", False)),
        "parent_bot_id": getattr(b, "parent_bot_id", None),
        "created_at": b.created_at.isoformat() if b.created_at else None,
    }


@router.get("")
def list_bots(db: Session = Depends(get_db), user: User = Depends(current_user)):
    bots = db.query(ChatBot).filter_by(user_id=user.id).order_by(ChatBot.id.desc()).all()
    return [_bot_dict(b) for b in bots]


@router.post("")
async def create_bot(req: BotCreate, db: Session = Depends(get_db),
                     user: User = Depends(current_user)):
    bot = ChatBot(
        user_id=user.id,
        name=req.name,
        model=req.model,
        system_prompt=req.system_prompt,
        workflow_json=req.workflow_json,
        tg_token=req.tg_token,
        vk_token=req.vk_token,
        vk_group_id=req.vk_group_id,
        avito_client_id=req.avito_client_id,
        avito_client_secret=req.avito_client_secret,
        avito_user_id=req.avito_user_id,
        max_token=req.max_token,
        widget_enabled=req.widget_enabled,
        widget_secret=generate_widget_secret() if req.widget_enabled else None,
        widget_allowed_origins=req.widget_allowed_origins,
        max_replies_day=req.max_replies_day,
        cost_per_reply=req.cost_per_reply,
    )
    db.add(bot)
    db.commit()
    db.refresh(bot)
    # Авто-deploy если есть хоть один канал
    setup = {}
    if _has_any_channel(bot):
        setup = await _auto_setup_channels(bot)
        bot.status = "active"
        db.commit()
        db.refresh(bot)
    out = _bot_dict(bot)
    out["setup"] = setup
    return out


@router.put("/{bot_id}")
async def update_bot(bot_id: int, req: BotUpdate, db: Session = Depends(get_db),
                     user: User = Depends(current_user)):
    bot = db.query(ChatBot).filter_by(id=bot_id, user_id=user.id).first()
    if not bot:
        raise HTTPException(404, "Бот не найден")

    # Запоминаем какие каналы изменились — чтобы переустановить webhook
    old_tg = bot.tg_token
    old_vk = (bot.vk_token, bot.vk_group_id)
    old_avito = (bot.avito_client_id, bot.avito_client_secret)
    old_max = bot.max_token
    old_widget = bot.widget_enabled

    for field in ["name", "model", "system_prompt", "workflow_json", "tg_token",
                  "vk_token", "vk_group_id", "avito_client_id", "avito_client_secret",
                  "avito_user_id", "max_token", "max_replies_day", "cost_per_reply",
                  "widget_allowed_origins"]:
        val = getattr(req, field, None)
        if val is not None:
            setattr(bot, field, val)

    if req.widget_enabled is not None:
        bot.widget_enabled = req.widget_enabled
        if req.widget_enabled and not bot.widget_secret:
            bot.widget_secret = generate_widget_secret()

    # Если бот выключен и у него вдруг появились креды — снимаем со «спящего».
    # Если изменились креды активного бота — переустанавливаем webhook.
    channels_changed = (
        bot.tg_token != old_tg
        or (bot.vk_token, bot.vk_group_id) != old_vk
        or (bot.avito_client_id, bot.avito_client_secret) != old_avito
        or bot.max_token != old_max
        or bot.widget_enabled != old_widget
    )
    setup = {}
    if channels_changed and _has_any_channel(bot):
        # Если поменялся TG-токен — снять старый webhook
        if bot.tg_token != old_tg and old_tg:
            try:
                await delete_telegram_webhook(old_tg)
            except Exception as e:
                log.warning(f"[Bot {bot.id}] failed to delete old TG webhook: {e}")
        # Если поменялся MAX-токен — снять старую подписку
        if bot.max_token != old_max and old_max:
            try:
                await delete_max_webhook(old_max)
            except Exception as e:
                log.warning(f"[Bot {bot.id}] failed to delete old MAX webhook: {e}")
        setup = await _auto_setup_channels(bot)
        bot.status = "active"
    elif not _has_any_channel(bot):
        bot.status = "off"

    db.commit()
    db.refresh(bot)
    out = _bot_dict(bot)
    if setup:
        out["setup"] = setup
    return out


class WorkflowAiRequest(BaseModel):
    task: str


@router.post("/ai-build-workflow")
async def ai_build_workflow(req: WorkflowAiRequest, db: Session = Depends(get_db),
                            user: User = Depends(current_user)):
    """
    AI-помощник: по описанию задачи собирает граф воркфлоу.
    Списывает реальные копейки за токены Claude — обычно 5-10 ₽ за вызов.
    """
    if not user.is_verified:
        raise HTTPException(403, "Подтвердите email")
    from server.billing import get_balance, deduct_atomic
    from server.workflow_builder import build_from_task
    from server.models import Transaction
    # Минимальная блокировка — 5 ₽ (500 копеек)
    if get_balance(db, user.id) < 500:
        raise HTTPException(402, "Недостаточно средств (минимум 5 ₽)")
    try:
        result = build_from_task(req.task)
    except ValueError as e:
        msg = str(e)
        if "недоступны" in msg.lower() or "провайдер" in msg.lower():
            raise HTTPException(503, msg)
        raise HTTPException(400, msg)
    except Exception as e:
        log.error(f"ai-build-workflow error: {e}")
        raise HTTPException(500, "Не удалось собрать воркфлоу. Попробуйте переформулировать задачу.")
    # Списываем по реальным токенам Claude:
    # Sonnet тариф ~80 коп/1k input + 300 коп/1k output (= 8/30 CH × 10)
    usage = result.get("usage") or {}
    cost_kop = max(50, int(usage.get("input_tokens", 0) / 1000 * 80
                        + usage.get("output_tokens", 0) / 1000 * 300))
    charged = deduct_atomic(db, user.id, cost_kop)
    db.add(Transaction(user_id=user.id, type="usage", tokens_delta=-charged,
                       description=f"AI-сборка воркфлоу ({charged/100:.2f} ₽)",
                       model="claude"))
    db.commit()
    result["charged_kopecks"] = charged
    result["charged_rub"] = charged / 100
    return result


class AiCreateBotRequest(BaseModel):
    description: str                       # «бот для записи в салон красоты на услугу X, время Y»
    name: str | None = None                # авто из workflow если не задано
    model: str = "gpt"
    tg_token: str | None = None            # токен ДОЧЕРНЕГО бота (того, что создаём)
    max_token: str | None = None
    widget_enabled: bool = False
    parent_bot_id: int | None = None       # бот-конструктор, через который создан (опц)
    cost_per_reply: int = 5
    max_replies_day: int = 100


@router.post("/ai-create")
async def ai_create_bot(req: AiCreateBotRequest, db: Session = Depends(get_db),
                        user: User = Depends(current_user)):
    """
    AI-конструктор бота: по текстовому описанию задачи создаёт ГОТОВЫЙ ChatBot
    с собранным workflow и подключёнными каналами (TG/MAX/widget).

    Pipeline: description → workflow_builder.build_from_task(...) →
    ChatBot(workflow_json=..., auto_generated=True, parent_bot_id=...) →
    _auto_setup_channels() для регистрации webhook'ов.

    Используется как самостоятельный endpoint И как backend для бота-конструктора
    в TG/MAX (тот после диалога с клиентом-владельцем салона зовёт этот endpoint).
    """
    if not user.is_verified:
        raise HTTPException(403, "Подтвердите email")
    from server.billing import get_balance, deduct_atomic
    from server.workflow_builder import build_from_task
    from server.models import Transaction

    desc = (req.description or "").strip()
    if len(desc) < 10:
        raise HTTPException(400, "Опишите задачу подробнее (минимум 10 символов)")

    # Минимальная блокировка по балансу — нужно минимум на сборку workflow (~5 ₽)
    # + первая работа бота. Не списываем заранее — спишется по факту build_from_task.
    if get_balance(db, user.id) < 500:
        raise HTTPException(402, "Недостаточно средств (минимум 5 ₽ на сборку)")

    # Лимит автогенеренных ботов — защита от runaway (бот-конструктор в TG/MAX
    # мог бы зациклиться и наплодить 1000 ботов).
    max_auto = int(getattr(user, "max_auto_bots", 5) or 5)
    auto_count = db.query(ChatBot).filter_by(user_id=user.id, auto_generated=True).count()
    if auto_count >= max_auto:
        raise HTTPException(403, f"Лимит AI-сгенеренных ботов исчерпан ({max_auto}). "
                                  f"Удалите ненужных или попросите админа поднять лимит.")

    # Если указан parent_bot_id — проверяем владение
    if req.parent_bot_id:
        parent = db.query(ChatBot).filter_by(id=req.parent_bot_id, user_id=user.id).first()
        if not parent:
            raise HTTPException(404, "Родительский бот не найден или не ваш")

    # 1. Собираем workflow
    try:
        wf = build_from_task(desc)
    except ValueError as e:
        msg = str(e)
        if "недоступны" in msg.lower() or "провайдер" in msg.lower():
            raise HTTPException(503, msg)
        raise HTTPException(400, msg)
    except Exception as e:
        log.error(f"ai-create-bot build_from_task error: {e}")
        raise HTTPException(500, "Не удалось собрать воркфлоу. Попробуйте переформулировать.")

    # Списываем за сборку workflow (по реальным токенам)
    usage = wf.get("usage") or {}
    cost_kop = max(50, int(usage.get("input_tokens", 0) / 1000 * 80
                        + usage.get("output_tokens", 0) / 1000 * 300))
    charged = deduct_atomic(db, user.id, cost_kop)
    db.add(Transaction(user_id=user.id, type="usage", tokens_delta=-charged,
                       description=f"AI-конструктор бота ({charged/100:.2f} ₽)",
                       model="claude"))

    # 2. Подменяем токены каналов в нодах workflow на токены, переданные юзером.
    # workflow_builder ставит в trigger_tg/trigger_max/output_tg пустые токены —
    # но при подключении канала движок берёт токен из самого бота (ChatBot.tg_token).
    # Тут ничего подменять не надо — оставляем граф как есть.

    bot_name = (req.name or wf.get("name") or "AI-бот")[:60]
    import json as _json
    bot = ChatBot(
        user_id=user.id,
        name=bot_name,
        model=req.model,
        system_prompt=None,                   # логика теперь в workflow_json
        workflow_json=_json.dumps(wf, ensure_ascii=False),
        tg_token=req.tg_token,
        max_token=req.max_token,
        widget_enabled=req.widget_enabled,
        widget_secret=generate_widget_secret() if req.widget_enabled else None,
        max_replies_day=req.max_replies_day,
        cost_per_reply=req.cost_per_reply,
        parent_bot_id=req.parent_bot_id,
        auto_generated=True,
    )
    db.add(bot)
    db.commit()
    db.refresh(bot)

    # 3. Авто-deploy webhook'ов если есть токены
    setup = {}
    if _has_any_channel(bot):
        setup = await _auto_setup_channels(bot)
        bot.status = "active"
        db.commit()
        db.refresh(bot)

    out = _bot_dict(bot)
    out["setup"] = setup
    out["workflow_explanation"] = wf.get("explanation", "")
    out["build_charged_rub"] = charged / 100
    return out


# ── «Доработать через AI»: правка существующего workflow по инструкции ─────

class AiImproveRequest(BaseModel):
    instruction: str    # «убери шаг с email», «добавь вопрос про бюджет», «сделай тон строже»


@router.post("/{bot_id}/ai-improve")
async def bot_ai_improve(bot_id: int, req: AiImproveRequest,
                          db: Session = Depends(get_db),
                          user: User = Depends(current_user)):
    """Френдли-мостик между шаблоном и Canvas-конструктором.

    Юзер пишет «что улучшить» — мы передаём текущий workflow + инструкцию
    в `workflow_builder` (тот же, что собирает с нуля), он возвращает
    обновлённый граф, мы подменяем `workflow_json` бота. Деньги списываются
    как за обычную AI-сборку (~5-10 ₽).
    """
    if not user.is_verified:
        raise HTTPException(403, "Подтвердите email")

    instr = (req.instruction or "").strip()
    if len(instr) < 5:
        raise HTTPException(400, "Опишите подробнее, что хотите изменить")

    bot = db.query(ChatBot).filter_by(id=bot_id, user_id=user.id).first()
    if not bot:
        raise HTTPException(404, "Бот не найден")

    from server.billing import get_balance, deduct_atomic
    from server.workflow_builder import build_from_task
    from server.models import Transaction
    if get_balance(db, user.id) < 500:
        raise HTTPException(402, "Недостаточно средств (минимум 5 ₽)")

    # Кормим workflow_builder и текущим графом, и инструкцией.
    # Так LLM видит контекст и не собирает с нуля, а правит.
    current_wf = bot.workflow_json or "(нет — сейчас простой автоответчик с system_prompt)"
    task = (
        "ЗАДАЧА: Улучшить существующий workflow чат-бота по инструкции владельца.\n\n"
        f"=== ТЕКУЩИЙ WORKFLOW БОТА «{bot.name}» (JSON) ===\n{current_wf}\n=== /WORKFLOW ===\n\n"
        f"=== ИНСТРУКЦИЯ ОТ ВЛАДЕЛЬЦА ===\n{instr}\n=== /ИНСТРУКЦИЯ ===\n\n"
        "Внеси изменения и верни обновлённый граф. Сохрани триггеры (trigger_tg, "
        "trigger_max и т.п.) — менять их не нужно. Если инструкция бессмысленна "
        "(нельзя реализовать) — верни исходный граф без изменений с explanation "
        "почему."
    )
    try:
        result = build_from_task(task)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        log.error(f"ai-improve error bot={bot_id}: {e}")
        raise HTTPException(500, "Не удалось обновить workflow. Переформулируйте инструкцию.")

    usage = result.get("usage") or {}
    cost_kop = max(50, int(usage.get("input_tokens", 0) / 1000 * 80
                        + usage.get("output_tokens", 0) / 1000 * 300))
    charged = deduct_atomic(db, user.id, cost_kop)
    db.add(Transaction(user_id=user.id, type="usage", tokens_delta=-charged,
                       description=f"AI-доработка бота «{bot.name}» ({charged/100:.2f} ₽)",
                       model="claude"))

    import json as _json
    bot.workflow_json = _json.dumps({
        "name": result.get("name") or bot.name,
        "wfc_nodes": result.get("wfc_nodes", []),
        "wfc_edges": result.get("wfc_edges", []),
    }, ensure_ascii=False)
    db.commit()

    return {
        "status": "ok",
        "explanation": result.get("explanation", ""),
        "charged_rub": charged / 100,
    }


# ── Аналитика бота ──────────────────────────────────────────────────────────

@router.get("/{bot_id}/analytics")
def bot_analytics(bot_id: int, days: int = 30,
                  db: Session = Depends(get_db),
                  user: User = Depends(current_user)):
    """Сводная аналитика для одного бота за последние N дней.

    Считает прямо из bot_conversation_turns и bot_records — отдельной
    таблицы метрик не нужно, объёмы пока небольшие. Если рост — добавим
    daily-таблицу + cron.

    Возвращает:
      total_dialogs   — уникальные chat_id с любой активностью за период
      total_msgs_in   — сообщений от юзеров
      total_msgs_out  — ответов бота
      conv_rate       — % диалогов которые завершились заявкой (bot_records)
      records_total   — заявок всего за период
      records_by_type — {lead: N, booking: N, ...}
      timeseries      — [{date, dialogs, records}] для графика
      top_questions   — топ-10 первых сообщений (популярные запросы)
    """
    from server.models import BotConversationTurn, BotRecord
    from sqlalchemy import func, distinct
    from datetime import datetime, timedelta
    from collections import Counter

    bot = db.query(ChatBot).filter_by(id=bot_id, user_id=user.id).first()
    if not bot:
        raise HTTPException(404, "Бот не найден")
    days = max(1, min(int(days or 30), 365))
    since = datetime.utcnow() - timedelta(days=days)

    # Базовые агрегаты по conversation
    msgs_q = db.query(BotConversationTurn).filter(
        BotConversationTurn.bot_id == bot_id,
        BotConversationTurn.created_at >= since,
    )
    total_msgs_in = msgs_q.filter(BotConversationTurn.role == "user").count()
    total_msgs_out = msgs_q.filter(BotConversationTurn.role == "assistant").count()
    total_dialogs = db.query(func.count(distinct(BotConversationTurn.chat_id))).filter(
        BotConversationTurn.bot_id == bot_id,
        BotConversationTurn.created_at >= since,
    ).scalar() or 0

    # Записи (заявки)
    rec_q = db.query(BotRecord).filter(
        BotRecord.bot_id == bot_id,
        BotRecord.created_at >= since,
    )
    records_total = rec_q.count()
    by_type_rows = db.query(BotRecord.record_type, func.count(BotRecord.id)).filter(
        BotRecord.bot_id == bot_id,
        BotRecord.created_at >= since,
    ).group_by(BotRecord.record_type).all()
    records_by_type = {t: n for t, n in by_type_rows}

    conv_rate = round(records_total / total_dialogs * 100, 1) if total_dialogs else 0.0

    # Timeseries по дням — простым GROUP BY DATE() (SQLite поддерживает strftime)
    ts_msgs = db.execute(
        func.strftime("%Y-%m-%d", BotConversationTurn.created_at).label("d") and  # noqa
        None  # ниже сделаем raw
    )  # placeholder; используем raw SQL ниже
    from sqlalchemy import text
    ts_rows = list(db.execute(text(
        "SELECT strftime('%Y-%m-%d', created_at) as d, COUNT(DISTINCT chat_id) as dialogs "
        "FROM bot_conversation_turns WHERE bot_id=:bid AND created_at>=:since "
        "GROUP BY d ORDER BY d"
    ), {"bid": bot_id, "since": since.isoformat()}))
    rec_rows = list(db.execute(text(
        "SELECT strftime('%Y-%m-%d', created_at) as d, COUNT(*) as records "
        "FROM bot_records WHERE bot_id=:bid AND created_at>=:since "
        "GROUP BY d ORDER BY d"
    ), {"bid": bot_id, "since": since.isoformat()}))
    rec_by_day = {r[0]: r[1] for r in rec_rows}
    timeseries = [{
        "date": r[0],
        "dialogs": r[1],
        "records": rec_by_day.get(r[0], 0),
    } for r in ts_rows]

    # Топ-вопросов: первое сообщение каждого диалога, нормализованное
    first_msgs = db.execute(text(
        "SELECT chat_id, MIN(id) as fid FROM bot_conversation_turns "
        "WHERE bot_id=:bid AND role='user' AND created_at>=:since "
        "GROUP BY chat_id"
    ), {"bid": bot_id, "since": since.isoformat()}).fetchall()
    fids = [r[1] for r in first_msgs]
    counter: Counter = Counter()
    if fids:
        # Достаём content для этих ids
        from sqlalchemy import select as _sel
        msg_rows = db.execute(text(
            f"SELECT content FROM bot_conversation_turns WHERE id IN ({','.join('?' * len(fids))})"
        ), fids).fetchall() if False else \
            db.query(BotConversationTurn.content).filter(BotConversationTurn.id.in_(fids)).all()
        for (content,) in msg_rows:
            norm = (content or "").strip().lower()[:80]
            if norm:
                counter[norm] += 1
    top_questions = [{"q": q, "count": c} for q, c in counter.most_common(10)]

    return {
        "period_days": days,
        "total_dialogs": int(total_dialogs),
        "total_msgs_in": int(total_msgs_in),
        "total_msgs_out": int(total_msgs_out),
        "records_total": int(records_total),
        "records_by_type": records_by_type,
        "conv_rate_pct": conv_rate,
        "timeseries": timeseries,
        "top_questions": top_questions,
    }


# ── Превью диалога с ботом (тестовый чат прямо в браузере) ──────────────────

class PreviewMessageRequest(BaseModel):
    message: str
    chat_id: str | None = None    # фронт держит свой sticky chat_id для непрерывного диалога


@router.post("/{bot_id}/preview")
async def bot_preview(bot_id: int, req: PreviewMessageRequest,
                      db: Session = Depends(get_db),
                      user: User = Depends(current_user)):
    """Отправить тестовое сообщение боту прямо из карточки.

    Это «песочница» для владельца бота — посмотреть как бот ответит до
    публикации в TG/MAX. Использует тот же handle_message, но с фейковой
    платформой 'preview' (бот не пытается отправить ответ в TG/MAX).
    """
    from server.chatbot_engine import handle_message
    bot = db.query(ChatBot).filter_by(id=bot_id, user_id=user.id).first()
    if not bot:
        raise HTTPException(404, "Бот не найден")
    text = (req.message or "").strip()
    if not text:
        raise HTTPException(400, "Пустое сообщение")
    if len(text) > 2000:
        text = text[:2000]
    chat_id = req.chat_id or f"preview_{user.id}_{bot_id}"

    try:
        # Платформа "preview" — output-ноды (output_tg/output_max) станут no-op
        # на отправку в реальные мессенджеры, но AI-ответ всё равно вернётся.
        answer = await handle_message(bot, chat_id, text, "preview", user.name or "Owner",
                                       extra_ctx={"is_preview": True})
    except Exception as e:
        log.error(f"[Preview] bot={bot_id} error: {e}")
        raise HTTPException(503, f"Ошибка бота: {e}")

    if not answer:
        return {"answer": "(Бот не ответил — проверьте workflow или баланс)", "chat_id": chat_id}
    return {"answer": answer, "chat_id": chat_id}


@router.delete("/{bot_id}/preview")
def bot_preview_reset(bot_id: int, chat_id: str | None = None,
                       db: Session = Depends(get_db),
                       user: User = Depends(current_user)):
    """Сбросить превью-диалог: чистит conversation history для preview-чата."""
    from server.models import BotConversationTurn
    bot = db.query(ChatBot).filter_by(id=bot_id, user_id=user.id).first()
    if not bot:
        raise HTTPException(404, "Бот не найден")
    cid = chat_id or f"preview_{user.id}_{bot_id}"
    db.query(BotConversationTurn).filter_by(bot_id=bot_id, chat_id=cid).delete()
    db.commit()
    return {"status": "reset", "chat_id": cid}


# ── Готовые шаблоны бизнес-ботов ────────────────────────────────────────────

@router.get("/templates")
def list_bot_templates():
    """Каталог 6 готовых шаблонов для галереи на /chatbots.html.
    Эндпоинт публичный (без current_user) — превью можно показать всем."""
    from server.bot_templates import list_templates
    return {"templates": list_templates()}


class FromTemplateRequest(BaseModel):
    bot_name: str | None = None
    params: dict = {}              # значения для customizable-полей шаблона
    tg_token: str | None = None
    max_token: str | None = None
    widget_enabled: bool = False


@router.post("/from-template/{slug}")
async def create_from_template(slug: str, req: FromTemplateRequest,
                                db: Session = Depends(get_db),
                                user: User = Depends(current_user)):
    """Создать бота из готового шаблона. Подставляет {{параметры}} в workflow,
    создаёт ChatBot, поднимает webhook'и каналов если переданы токены."""
    from server.bot_templates import render_template, TEMPLATES_BY_SLUG
    if slug not in TEMPLATES_BY_SLUG:
        raise HTTPException(404, "Шаблон не найден")
    if not user.is_verified:
        raise HTTPException(403, "Подтвердите email")

    # Валидация обязательных полей шаблона
    tpl = TEMPLATES_BY_SLUG[slug]
    missing = [f["label"] for f in tpl["customizable"]
               if f.get("required") and not (req.params or {}).get(f["key"])]
    if missing:
        raise HTTPException(400, f"Заполните обязательные поля: {', '.join(missing)}")

    rendered = render_template(slug, {**(req.params or {}), "bot_name": req.bot_name})
    if not rendered:
        raise HTTPException(500, "Не удалось подготовить шаблон")

    bot = ChatBot(
        user_id=user.id,
        name=rendered["name"][:60],
        model=rendered["recommended_model"],
        workflow_json=rendered["workflow_json"],
        tg_token=req.tg_token,
        max_token=req.max_token,
        widget_enabled=req.widget_enabled,
        widget_secret=generate_widget_secret() if req.widget_enabled else None,
        max_replies_day=200,
        cost_per_reply=5,
        auto_generated=False,  # это шаблон, а не AI-сборка
    )
    db.add(bot); db.commit(); db.refresh(bot)

    setup = {}
    if _has_any_channel(bot):
        setup = await _auto_setup_channels(bot)
        bot.status = "active"
        db.commit()
        db.refresh(bot)

    out = _bot_dict(bot)
    out["setup"] = setup
    out["template_slug"] = slug
    out["template_name"] = tpl["name"]
    return out


# ── Записи бота (заявки/брони/заказы) ───────────────────────────────────────

@router.get("/{bot_id}/records")
def list_bot_records(bot_id: int, type: str | None = None,
                     status: str | None = None,
                     offset: int = 0, limit: int = 100,
                     paginated: int = 0,
                     db: Session = Depends(get_db),
                     user: User = Depends(current_user)):
    """Список записей (заявки/брони) для карточки бота. Фильтры + пагинация.

    Совместимость:
      - paginated=0 (default) → массив items (legacy фронт)
      - paginated=1           → {items, total, offset, limit}
    """
    from server.models import BotRecord
    bot = db.query(ChatBot).filter_by(id=bot_id, user_id=user.id).first()
    if not bot:
        raise HTTPException(404, "Бот не найден")
    limit = max(1, min(int(limit or 100), 500))
    offset = max(0, int(offset or 0))
    q = db.query(BotRecord).filter_by(bot_id=bot_id)
    if type:
        q = q.filter(BotRecord.record_type == type)
    if status:
        q = q.filter(BotRecord.status == status)
    total = q.count() if paginated else None
    rows = q.order_by(BotRecord.id.desc()).offset(offset).limit(limit).all()
    import json as _json
    items = [{
        "id": r.id,
        "type": r.record_type,
        "name": r.customer_name,
        "phone": r.customer_phone,
        "email": r.customer_email,
        "payload": _json.loads(r.payload) if r.payload else {},
        "status": r.status,
        "notes": r.notes,
        "chat_id": r.chat_id,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]
    if paginated:
        return {"items": items, "total": total, "offset": offset, "limit": limit}
    return items


class RecordUpdateBody(BaseModel):
    status: str | None = None      # new | processed | cancelled
    notes: str | None = None


@router.patch("/{bot_id}/records/{rec_id}")
def update_bot_record(bot_id: int, rec_id: int, body: RecordUpdateBody,
                      db: Session = Depends(get_db),
                      user: User = Depends(current_user)):
    """Обновить статус заявки или внутренние заметки владельца."""
    from server.models import BotRecord
    bot = db.query(ChatBot).filter_by(id=bot_id, user_id=user.id).first()
    if not bot:
        raise HTTPException(404, "Бот не найден")
    rec = db.query(BotRecord).filter_by(id=rec_id, bot_id=bot_id).first()
    if not rec:
        raise HTTPException(404, "Запись не найдена")
    if body.status is not None:
        if body.status not in ("new", "processed", "cancelled"):
            raise HTTPException(400, "status: new / processed / cancelled")
        rec.status = body.status
    if body.notes is not None:
        rec.notes = body.notes[:2000]
    db.commit()
    return {"status": "ok", "record_status": rec.status}


@router.delete("/{bot_id}/records/{rec_id}")
def delete_bot_record(bot_id: int, rec_id: int,
                      db: Session = Depends(get_db),
                      user: User = Depends(current_user)):
    from server.models import BotRecord
    bot = db.query(ChatBot).filter_by(id=bot_id, user_id=user.id).first()
    if not bot:
        raise HTTPException(404, "Бот не найден")
    rec = db.query(BotRecord).filter_by(id=rec_id, bot_id=bot_id).first()
    if not rec:
        raise HTTPException(404)
    db.delete(rec); db.commit()
    return {"status": "deleted"}


@router.get("/{bot_id}/records.csv")
def export_bot_records_csv(bot_id: int, type: str | None = None,
                           db: Session = Depends(get_db),
                           user: User = Depends(current_user)):
    """CSV-экспорт записей: открывается в Excel/Numbers/Google Sheets.
    Полезно когда у владельца 50+ заявок и нужно отдать менеджеру."""
    from server.models import BotRecord
    from fastapi.responses import StreamingResponse
    import csv, io, json as _json
    bot = db.query(ChatBot).filter_by(id=bot_id, user_id=user.id).first()
    if not bot:
        raise HTTPException(404, "Бот не найден")
    q = db.query(BotRecord).filter_by(bot_id=bot_id)
    if type:
        q = q.filter(BotRecord.record_type == type)
    rows = q.order_by(BotRecord.id.desc()).all()

    buf = io.StringIO()
    # BOM для корректной кириллицы при открытии в Excel
    buf.write("\ufeff")
    w = csv.writer(buf, delimiter=";")
    w.writerow(["id", "type", "created_at", "name", "phone", "email", "status", "notes", "payload"])
    for r in rows:
        w.writerow([
            r.id, r.record_type,
            r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "",
            r.customer_name or "", r.customer_phone or "", r.customer_email or "",
            r.status or "", (r.notes or "").replace("\n", " "),
            r.payload or "",
        ])
    buf.seek(0)
    safe_name = "".join(c if c.isalnum() else "_" for c in (bot.name or "bot"))[:40]
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}_records.csv"'},
    )


class DeployConstructorRequest(BaseModel):
    tg_token: str | None = None
    max_token: str | None = None


@router.post("/deploy-constructor")
async def deploy_bot_constructor(req: DeployConstructorRequest,
                                  db: Session = Depends(get_db),
                                  user: User = Depends(current_user)):
    """Задеплоить готового бота-конструктора (TG/MAX) под этим аккаунтом.

    Это бот, в котором клиенты диалогом описывают свою задачу и через «/build»
    запускают создание дочернего бота. Создаёт ChatBot из шаблона
    server/bot_constructor_template.py и поднимает webhook'и.

    Только для админов (защита от случайного запуска юзерами).
    """
    from server.security import require_admin
    require_admin(user)
    if not req.tg_token and not req.max_token:
        raise HTTPException(400, "Укажите хотя бы один токен (TG или MAX)")

    import json as _json
    from server.bot_constructor_template import (
        CONSTRUCTOR_WORKFLOW, CONSTRUCTOR_WORKFLOW_MAX, CONSTRUCTOR_SYSTEM_PROMPT,
    )

    # TG-версия и MAX-версия — отдельные ChatBot'ы (разные workflow с разными триггерами)
    deployed = []
    if req.tg_token:
        bot = ChatBot(
            user_id=user.id,
            name="🪄 AI-конструктор (TG)",
            model="claude",
            system_prompt=CONSTRUCTOR_SYSTEM_PROMPT,
            workflow_json=_json.dumps(CONSTRUCTOR_WORKFLOW, ensure_ascii=False),
            tg_token=req.tg_token,
            max_replies_day=10000,
            cost_per_reply=10,
            auto_generated=False,  # это сам конструктор, не сгенеренный
        )
        db.add(bot); db.commit(); db.refresh(bot)
        setup = await _auto_setup_channels(bot)
        bot.status = "active"; db.commit()
        deployed.append({"channel": "telegram", "bot_id": bot.id, "setup": setup})

    if req.max_token:
        bot = ChatBot(
            user_id=user.id,
            name="🪄 AI-конструктор (MAX)",
            model="claude",
            system_prompt=CONSTRUCTOR_SYSTEM_PROMPT,
            workflow_json=_json.dumps(CONSTRUCTOR_WORKFLOW_MAX, ensure_ascii=False),
            max_token=req.max_token,
            max_replies_day=10000,
            cost_per_reply=10,
            auto_generated=False,
        )
        db.add(bot); db.commit(); db.refresh(bot)
        setup = await _auto_setup_channels(bot)
        bot.status = "active"; db.commit()
        deployed.append({"channel": "max", "bot_id": bot.id, "setup": setup})

    return {"deployed": deployed}


@router.delete("/{bot_id}")
async def delete_bot(bot_id: int, db: Session = Depends(get_db),
                     user: User = Depends(current_user)):
    bot = db.query(ChatBot).filter_by(id=bot_id, user_id=user.id).first()
    if not bot:
        raise HTTPException(404, "Бот не найден")
    # Удалить webhook если был
    if bot.tg_token and bot.tg_webhook_set:
        await delete_telegram_webhook(bot.tg_token)
    if bot.max_token and bot.max_webhook_set:
        try:
            await delete_max_webhook(bot.max_token)
        except Exception as e:
            log.warning(f"[Bot {bot.id}] delete MAX webhook failed: {e}")
    db.delete(bot)
    db.commit()
    return {"status": "deleted"}


@router.post("/{bot_id}/deploy")
async def deploy_bot(bot_id: int, db: Session = Depends(get_db),
                     user: User = Depends(current_user)):
    """Активировать бота — установить webhooks."""
    bot = db.query(ChatBot).filter_by(id=bot_id, user_id=user.id).first()
    if not bot:
        raise HTTPException(404, "Бот не найден")

    results = {}

    # Telegram
    if bot.tg_token:
        wh_url = f"{APP_URL}/webhook/tg/{bot.id}"
        r = await setup_telegram_webhook(bot.tg_token, wh_url)
        bot.tg_webhook_set = r.get("ok", False)
        results["telegram"] = r

    # VK — confirmation code будет установлен при первом callback
    if bot.vk_token and bot.vk_group_id:
        results["vk"] = {"status": "ready", "callback_url": f"{APP_URL}/webhook/vk/{bot.id}"}

    # Авито
    if bot.avito_client_id and bot.avito_client_secret:
        results["avito"] = {"status": "ready", "webhook_url": f"{APP_URL}/webhook/avito/{bot.id}"}

    # MAX
    if bot.max_token:
        wh_url = f"{APP_URL}/webhook/max/{bot.id}"
        r = await setup_max_webhook(bot.max_token, wh_url)
        bot.max_webhook_set = bool(r.get("ok"))
        me = await get_max_me(bot.max_token)
        results["max"] = {**r, "username": me.get("username") or me.get("name") or ""}

    # Виджет
    if bot.widget_enabled:
        if not bot.widget_secret:
            bot.widget_secret = generate_widget_secret()
        results["widget"] = {"url": f"{APP_URL}/widget/{bot.id}.js"}

    bot.status = "active"
    db.commit()
    return {"status": "deployed", "channels": results}


@router.post("/{bot_id}/pause")
async def pause_bot(bot_id: int, db: Session = Depends(get_db),
                    user: User = Depends(current_user)):
    """Поставить бота на паузу."""
    bot = db.query(ChatBot).filter_by(id=bot_id, user_id=user.id).first()
    if not bot:
        raise HTTPException(404, "Бот не найден")

    if bot.tg_token and bot.tg_webhook_set:
        await delete_telegram_webhook(bot.tg_token)
        bot.tg_webhook_set = False
    if bot.max_token and bot.max_webhook_set:
        try:
            await delete_max_webhook(bot.max_token)
            bot.max_webhook_set = False
        except Exception as e:
            log.warning(f"[Bot {bot.id}] pause MAX failed: {e}")

    bot.status = "paused"
    db.commit()
    return {"status": "paused"}


@router.get("/{bot_id}/summary")
async def bot_summary(bot_id: int, db: Session = Depends(get_db),
                      user: User = Depends(current_user)):
    """Краткий пересказ диалогов бота."""
    bot = db.query(ChatBot).filter_by(id=bot_id, user_id=user.id).first()
    if not bot:
        raise HTTPException(404, "Бот не найден")
    return await get_summary(bot)


# ── База знаний бота ──────────────────────────────────────────────────────────

@router.get("/{bot_id}/kb")
def bot_kb_list(bot_id: int, db: Session = Depends(get_db),
                user: User = Depends(current_user)):
    bot = db.query(ChatBot).filter_by(id=bot_id, user_id=user.id).first()
    if not bot: raise HTTPException(404)
    from server.knowledge import get_all_files
    return get_all_files(bot.id)


@router.post("/{bot_id}/kb/add")
def bot_kb_add(bot_id: int, body: dict,
               db: Session = Depends(get_db),
               user: User = Depends(current_user)):
    """Добавить файл в БЗ. body: {name, path, content_text (опц.)}"""
    bot = db.query(ChatBot).filter_by(id=bot_id, user_id=user.id).first()
    if not bot: raise HTTPException(404)
    from server.knowledge import add_file
    from server.chatbot_engine import _extract_text_from_file
    import os as _os
    path = body.get("path", "")
    name = body.get("name") or _os.path.basename(path) or "file"
    content = body.get("content_text") or _extract_text_from_file(path)
    result = add_file(bot_id=bot.id, name=name, path=path,
                      size=body.get("size", 0), content_text=content)
    return result


@router.delete("/{bot_id}/kb/{file_id}")
def bot_kb_delete(bot_id: int, file_id: int,
                  db: Session = Depends(get_db),
                  user: User = Depends(current_user)):
    bot = db.query(ChatBot).filter_by(id=bot_id, user_id=user.id).first()
    if not bot: raise HTTPException(404)
    from server.models import KnowledgeFile
    f = db.query(KnowledgeFile).filter_by(id=file_id, bot_id=bot.id).first()
    if not f: raise HTTPException(404)
    db.delete(f); db.commit()
    return {"status": "deleted"}
