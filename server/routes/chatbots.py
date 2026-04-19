"""CRUD API для постоянных чат-ботов."""
import os, logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from server.routes.deps import get_db, current_user
from server.models import ChatBot, User
from server.chatbot_engine import (
    setup_telegram_webhook, delete_telegram_webhook,
    get_summary, generate_widget_secret,
)

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
    widget_enabled: bool = False
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
    widget_enabled: bool | None = None
    max_replies_day: int | None = None
    cost_per_reply: int | None = None


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
            out["telegram"] = {"ok": bot.tg_webhook_set, "detail": r.get("description", "")}
        except Exception as e:
            log.error(f"[Bot {bot.id}] TG setup failed: {e}")
            out["telegram"] = {"ok": False, "detail": str(e)}
    if bot.vk_token and bot.vk_group_id:
        out["vk"] = {"ok": True, "callback_url": f"{APP_URL}/webhook/vk/{bot.id}",
                     "hint": "Укажите этот URL в Callback API группы VK"}
    if bot.avito_client_id and bot.avito_client_secret:
        out["avito"] = {"ok": True, "webhook_url": f"{APP_URL}/webhook/avito/{bot.id}"}
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
        "widget_enabled": b.widget_enabled,
        "widget_url": f"{APP_URL}/widget/{b.id}.js" if b.widget_enabled else None,
        "has_workflow": bool(b.workflow_json),
        "max_replies_day": b.max_replies_day,
        "cost_per_reply": b.cost_per_reply,
        "replies_today": b.replies_today or 0,
        "status": b.status,
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
        widget_enabled=req.widget_enabled,
        widget_secret=generate_widget_secret() if req.widget_enabled else None,
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
    old_widget = bot.widget_enabled

    for field in ["name", "model", "system_prompt", "workflow_json", "tg_token",
                  "vk_token", "vk_group_id", "avito_client_id", "avito_client_secret",
                  "avito_user_id", "max_replies_day", "cost_per_reply"]:
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


@router.delete("/{bot_id}")
async def delete_bot(bot_id: int, db: Session = Depends(get_db),
                     user: User = Depends(current_user)):
    bot = db.query(ChatBot).filter_by(id=bot_id, user_id=user.id).first()
    if not bot:
        raise HTTPException(404, "Бот не найден")
    # Удалить webhook если был
    if bot.tg_token and bot.tg_webhook_set:
        await delete_telegram_webhook(bot.tg_token)
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
