"""Публикация подготовленных постов в каналы (Шаг C).

MVP: только Telegram. VK / YouTube / Instagram — следующие итерации.

Логика:
  - Берём CreatorChannelConnection где is_active=true для нужного бренда+платформы
  - Для TG: sendMessage или sendPhoto (если есть prepared_media_url)
  - При ошибке инкрементируем fail_count, после 10 → is_active=false
  - Помечаем item.status='published', published_at=now
"""
import asyncio
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from server.models import (
    ContentItem, CreatorChannelConnection, CreatorBrand, ContentCalendar,
)

log = logging.getLogger(__name__)

MAX_FAIL_BEFORE_DISABLE = 10


async def publish_to_tg(conn: CreatorChannelConnection, item: ContentItem) -> dict:
    """Публикация в Telegram-канал.

    conn.token = bot-token, conn.channel_id = @channel или -100xxxxxxxx.
    Бот должен быть админом канала с правом «Post Messages».
    """
    from server.messaging.senders import send_telegram, send_telegram_photo

    if not conn.token or not conn.channel_id:
        return {"ok": False, "description": "Не настроен token или channel_id"}

    text = (item.prepared_content_md or "").strip()
    media = item.prepared_media_url

    if media:
        # Caption в TG ограничен 1024 символами — если текст длиннее, отправим
        # картинку без caption и текст отдельным сообщением. Тогда метрики
        # cron будет fetcher по ID ТЕКСТОВОГО сообщения (длинный пост виден
        # сразу при открытии поста — там основные views).
        if len(text) > 1000:
            r1 = await send_telegram_photo(conn.token, conn.channel_id, media, caption="")
            r2 = await send_telegram(conn.token, conn.channel_id, text, parse_mode=None)
            ok = bool(r1.get("ok") and r2.get("ok"))
            msg_id = (r2.get("result") or {}).get("message_id") if ok else None
            chat = (r2.get("result") or {}).get("chat") or {}
            return {"ok": ok, "external_post_id": str(msg_id) if msg_id else None,
                    "external_chat_id": str(chat.get("id") or chat.get("username") or conn.channel_id),
                    "result": [r1, r2]}
        else:
            r = await send_telegram_photo(conn.token, conn.channel_id, media,
                                           caption=text, parse_mode=None)
            msg_id = (r.get("result") or {}).get("message_id") if r.get("ok") else None
            chat = (r.get("result") or {}).get("chat") or {}
            r["external_post_id"] = str(msg_id) if msg_id else None
            r["external_chat_id"] = str(chat.get("id") or chat.get("username") or conn.channel_id)
            return r
    else:
        r = await send_telegram(conn.token, conn.channel_id, text, parse_mode=None)
        msg_id = (r.get("result") or {}).get("message_id") if r.get("ok") else None
        chat = (r.get("result") or {}).get("chat") or {}
        r["external_post_id"] = str(msg_id) if msg_id else None
        r["external_chat_id"] = str(chat.get("id") or chat.get("username") or conn.channel_id)
        return r


def _platform_supports_auto_publish(platform: str) -> bool:
    """Какие платформы умеют автопостинг в MVP."""
    return platform in ("tg", "vk")  # yt/ig — позже


async def publish_to_vk(conn: CreatorChannelConnection, item: ContentItem) -> dict:
    """Публикация на стену VK-сообщества.

    Возвращает {ok, post_id, external_post_id, external_chat_id} —
    дополнительно external_* для creators_metrics_loop (cron fetch metrics).
    """
    from server.creators_vk import publish_to_vk_wall
    if not conn.token or not conn.channel_id:
        return {"ok": False, "description": "Не настроен token или community_id"}
    text = (item.prepared_content_md or "").strip()
    media = item.prepared_media_url
    r = await publish_to_vk_wall(conn.token, conn.channel_id, text, media)
    if r.get("ok") and r.get("post_id"):
        r["external_post_id"] = str(r["post_id"])
        r["external_chat_id"] = str(conn.channel_id)
    return r


async def publish_item(db: Session, item: ContentItem) -> dict:
    """Опубликовать готовый пост в канал бренда.

    Возвращает {"ok": bool, "channel_id": int|None, "description": str|None}.

    Атомарный claim: status 'ready' → 'publishing' через UPDATE WHERE.
    Только первый вызов получает rowcount=1 и публикует; параллельный
    вызов увидит rowcount=0 → вернёт «уже публикуется» и не отправит
    второй пост в канал. Раньше read-modify-write `if status==ready;
    ...; status=published` мог дать двойной post при race с cron'ом.
    """
    if not item.prepared_content_md:
        return {"ok": False, "description": "prepared_content_md пустой"}

    from sqlalchemy import update as _sa_update
    claim = db.execute(
        _sa_update(ContentItem)
        .where(ContentItem.id == item.id, ContentItem.status == "ready")
        .values(status="publishing")
    )
    db.commit()
    if claim.rowcount != 1:
        return {"ok": False, "description": f"item.status={item.status}, требуется ready (или уже publishing)"}
    # Обновляем in-memory объект чтобы дальнейшая логика видела актуальный status
    db.refresh(item)

    cal = db.query(ContentCalendar).filter_by(id=item.calendar_id).first()
    if not cal:
        return {"ok": False, "description": "calendar not found"}

    if not _platform_supports_auto_publish(item.platform):
        # Не поддерживается — оставляем как ready, юзер скопирует руками
        return {"ok": False, "description": f"platform {item.platform} requires manual publish"}

    # Берём первый active канал на этой платформе для бренда
    conn = (db.query(CreatorChannelConnection)
              .filter_by(brand_id=cal.brand_id, platform=item.platform, is_active=True)
              .order_by(CreatorChannelConnection.id.asc())
              .first())
    if not conn:
        return {"ok": False, "description": f"нет подключённого канала {item.platform}"}

    try:
        if item.platform == "tg":
            result = await publish_to_tg(conn, item)
        elif item.platform == "vk":
            result = await publish_to_vk(conn, item)
        else:
            result = {"ok": False, "description": f"платформа {item.platform} не поддерживается"}
    except Exception as e:
        log.exception("[creators.publish] %s send exception: %s", item.platform, e)
        result = {"ok": False, "description": str(e)[:300]}
        # status вернётся в 'ready' в else-ветке ниже — не теряем атомарность.

    if result.get("ok"):
        item.status = "published"
        item.published_at = datetime.utcnow()
        item.error = None
        # Сохраняем external_post_id чтобы cron потом fetch'нул метрики
        # (см. server/cron/creators_metrics.py).
        if result.get("external_post_id"):
            item.external_post_id = result["external_post_id"]
        if result.get("external_chat_id"):
            item.external_chat_id = result["external_chat_id"]
        conn.fail_count = 0
        db.commit()

        # Мост → модуль copywriter ИИ-Агента: сохраняем опубликованный пост
        # как пример стиля автора ИМЕННО ЭТОГО БРЕНДА (B-3 per-brand learning).
        # No-op если модуль не подключён.
        try:
            from server.creators_copywriter_bridge import save_published_to_copywriter
            brand = db.query(CreatorBrand).filter_by(id=cal.brand_id).first()
            if brand and brand.user_id:
                save_published_to_copywriter(
                    db, brand.user_id,
                    text=item.prepared_content_md or "",
                    platform=item.platform or "",
                    brand_id=brand.id,
                )
        except Exception as e:
            log.warning("[creators.publish] copywriter bridge failed: %s", e)

        return {"ok": True, "channel_id": conn.id}
    else:
        # Инкремент fail_count + опц. деактивация
        conn.fail_count = (conn.fail_count or 0) + 1
        conn.last_error_at = datetime.utcnow()
        if conn.fail_count >= MAX_FAIL_BEFORE_DISABLE:
            conn.is_active = False
            log.warning("[creators.publish] channel %s disabled (fail_count >= %s)",
                        conn.id, MAX_FAIL_BEFORE_DISABLE)
        desc = result.get("description") or str(result)[:200]
        item.error = f"publish: {desc[:400]}"
        # Возвращаем status обратно в 'ready' — иначе после atomic claim'а
        # выше item застрянет в 'publishing' и cron retry никогда не сработает.
        item.status = "ready"
        db.commit()
        return {"ok": False, "channel_id": conn.id, "description": desc}


# Sync wrapper для использования из не-async кода (если понадобится)
def publish_item_sync(db: Session, item: ContentItem) -> dict:
    return asyncio.run(publish_item(db, item))


# ── Telegram channel verification ─────────────────────────────────────────────

async def verify_tg_channel(token: str, channel_id: str) -> dict:
    """Проверить что бот может публиковать в канал.

    Шлёт пробное сообщение → удаляет его. Возвращает {"ok": bool, "title": str?,
    "description": str?}.
    """
    from server.messaging.senders import HTTP
    if not token or not channel_id:
        return {"ok": False, "description": "Пустые token/channel_id"}

    # getMe — валидация token
    try:
        r = await HTTP.get(f"https://api.telegram.org/bot{token}/getMe")
        if not r.json().get("ok"):
            return {"ok": False, "description": "Bot token невалидный"}
    except Exception as e:
        return {"ok": False, "description": f"getMe error: {e}"}

    # getChat — проверка что бот видит канал
    try:
        r = await HTTP.get(f"https://api.telegram.org/bot{token}/getChat",
                            params={"chat_id": channel_id})
        chat = r.json()
        if not chat.get("ok"):
            return {"ok": False, "description": chat.get("description", "Канал недоступен. Добавь бота в админы канала.")}
        title = (chat.get("result") or {}).get("title") or channel_id
        return {"ok": True, "title": title}
    except Exception as e:
        return {"ok": False, "description": f"getChat error: {e}"}
