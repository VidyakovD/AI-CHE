"""Bootstrap-импорт прошлых постов юзера в memory модуля `copywriter`.

Идея: когда юзер только подключил copywriter и у него ещё нет
опубликованных через Креаторы постов — модуль L0, ничего про юзера
не знает. Но у юзера УЖЕ есть бренд с подключённым VK-сообществом или
public TG-каналом и сотни постов в архиве.

Этот модуль одной командой подтягивает эти посты:
  - VK community → wall.get (последние 100 постов одним запросом)
  - TG public channel → t.me/s/{username} HTML preview парсится regex'ом
                        (Bot API НЕ даёт history → используем web preview)

Импортированные посты складываются в copywriter.examples_by_brand[brand_id]
через save_published_to_copywriter — таким же путём как реальные публикации.
После импорта interaction_count + кол-во выученных позволят прокачать
модуль с L0 сразу до L1-L2 (по compute_module_level).

Лимиты:
  TG_MAX_POSTS = 30 за один проход (preview-страница t.me/s обычно
                 содержит около 20 видимых сообщений).
  VK_MAX_POSTS = 50 за бренд (wall.get count). Если у юзера тысячи —
                 хватает свежих для стиля.
  MAX_BRANDS_PER_RUN = 10 (защита от long-running).

Не raise'ит при ошибке отдельного канала — лог + skip, остальные продолжают.
"""
from __future__ import annotations

import logging
import re
from html import unescape
from typing import Any

import httpx
from sqlalchemy.orm import Session

from server.creators_copywriter_bridge import save_published_to_copywriter

log = logging.getLogger(__name__)


TG_MAX_POSTS = 30
VK_MAX_POSTS = 50
MAX_BRANDS_PER_RUN = 10
MIN_POST_LENGTH = 20   # короче — мусор (стикеры/реакции/forward без caption)
HTTP_TIMEOUT = 20.0


async def fetch_vk_community_posts(token: str, group_id: int,
                                   limit: int = VK_MAX_POSTS) -> list[dict]:
    """wall.get последние посты community.

    Возвращает [{"text", "platform":"vk", "date": unix_ts}, ...].
    Пустой список при ошибке/пустой стене.
    """
    if not token or not group_id:
        return []
    params = {
        "access_token": token,
        "v": "5.131",
        "owner_id": -abs(int(group_id)),  # community = отрицательный
        "count": min(limit, 100),
        "filter": "owner",
    }
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            r = await client.post("https://api.vk.com/method/wall.get", data=params)
            data = r.json() or {}
    except Exception as e:
        log.warning("[bootstrap.vk] HTTP fail group=%s: %s", group_id, e)
        return []

    if data.get("error"):
        log.warning("[bootstrap.vk] API error group=%s: %s", group_id,
                    data["error"].get("error_msg"))
        return []

    items = (data.get("response") or {}).get("items") or []
    posts = []
    for p in items:
        text = (p.get("text") or "").strip()
        if not text or len(text) < MIN_POST_LENGTH:
            continue
        # Уберём рекламные/promo посты — у них marked_as_ads=1
        if p.get("marked_as_ads"):
            continue
        posts.append({
            "text": text,
            "platform": "vk",
            "date": p.get("date"),
        })
    return posts


# Регекспы для парсинга t.me/s/{channel} preview-страницы. Используем
# простые паттерны вместо BeautifulSoup — это пакет под web-preview формат
# Telegram, и держать на нём библиотечную зависимость избыточно.
_TG_MESSAGE_TEXT_RE = re.compile(
    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
    re.S
)
_TG_TAG_RE = re.compile(r'<[^>]+>')
_TG_BR_RE = re.compile(r'<br\s*/?>', re.I)
_TG_A_TAG_RE = re.compile(r'<a [^>]*>(.*?)</a>', re.S)


async def fetch_tg_channel_preview(channel_username: str,
                                   limit: int = TG_MAX_POSTS) -> list[dict]:
    """Парсить https://t.me/s/{username} HTML.

    channel_username — без '@'. Работает ТОЛЬКО для public-каналов
    (private каналы preview не отдают). Numeric -100... тоже не подходит —
    нужен именно username.

    Бот API через bot_token не даёт читать историю канала вообще
    (только новые сообщения через getUpdates). Поэтому web-preview —
    единственный лёгкий способ забрать архив без MTProto/Telethon.
    """
    username = (channel_username or "").lstrip("@").strip()
    if not username or username.startswith("-100") or username.startswith("-"):
        return []  # numeric ID — preview не работает
    # Sanity: username — латиница/цифры/нижнее подчёркивание
    if not re.match(r"^[A-Za-z][A-Za-z0-9_]{3,32}$", username):
        return []

    url = f"https://t.me/s/{username}"
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; AIche-Bootstrap/1.0)",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
    }
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT,
                                     follow_redirects=True) as client:
            r = await client.get(url, headers=headers)
    except Exception as e:
        log.warning("[bootstrap.tg] HTTP fail %s: %s", username, e)
        return []

    if r.status_code != 200:
        log.warning("[bootstrap.tg] status=%s for %s", r.status_code, username)
        return []

    html = r.text
    posts = []
    for m in _TG_MESSAGE_TEXT_RE.finditer(html):
        inner = m.group(1)
        # <br> → \n
        inner = _TG_BR_RE.sub("\n", inner)
        # <a href="...">label</a> → label
        inner = _TG_A_TAG_RE.sub(r"\1", inner)
        # Все остальные теги
        text = _TG_TAG_RE.sub("", inner)
        text = unescape(text).strip()
        if not text or len(text) < MIN_POST_LENGTH:
            continue
        posts.append({"text": text, "platform": "tg", "date": None})
        if len(posts) >= limit:
            break
    return posts


async def bootstrap_copywriter_from_channels(db: Session, user_id: int) -> dict:
    """Главный вызов для endpoint'а.

    Идём по всем брендам юзера (CreatorBrand) → их активным каналам
    (CreatorChannelConnection) → достаём свежие посты → сохраняем в
    memory copywriter через save_published_to_copywriter.

    Возвращает сводку:
      {
        "imported": int,                              # всего постов
        "per_brand": [{"brand_id", "brand_name",      # детально по брендам
                       "imported", "channels": [..]}],
        "skipped_brands": int,                        # без активных каналов
        "errors": [str, ...]                          # human-readable
      }
    """
    from server.models import CreatorBrand, CreatorChannelConnection

    brands = (db.query(CreatorBrand)
                .filter(CreatorBrand.user_id == user_id)
                .order_by(CreatorBrand.id.asc())
                .limit(MAX_BRANDS_PER_RUN)
                .all())
    if not brands:
        return {
            "imported": 0,
            "per_brand": [],
            "skipped_brands": 0,
            "errors": ["У тебя нет ни одного бренда в Креаторах. "
                       "Создай бренд и подключи канал — потом запусти импорт."],
        }

    total = 0
    per_brand_summary: list[dict] = []
    skipped = 0
    errors: list[str] = []

    for brand in brands:
        channels = (db.query(CreatorChannelConnection)
                      .filter(CreatorChannelConnection.brand_id == brand.id,
                              CreatorChannelConnection.is_active.is_(True))
                      .all())
        if not channels:
            skipped += 1
            continue

        brand_imported = 0
        brand_channels_info: list[dict] = []

        for ch in channels:
            posts: list[dict] = []
            ch_label = f"{ch.platform}:{ch.title or ch.channel_id or '?'}"
            try:
                if ch.platform == "vk" and ch.token and ch.channel_id:
                    try:
                        gid = abs(int(str(ch.channel_id).lstrip("-club").lstrip("public")))
                    except ValueError:
                        gid = 0
                    if gid:
                        posts = await fetch_vk_community_posts(ch.token, gid)
                elif ch.platform == "tg" and ch.channel_id:
                    posts = await fetch_tg_channel_preview(str(ch.channel_id))
                else:
                    # yt/ig — пока не поддерживаем bootstrap (нет API READ)
                    continue
            except Exception as e:
                log.exception("[bootstrap] channel %s failed: %s", ch_label, e)
                errors.append(f"{ch_label}: {e!s:.120}")
                continue

            saved = 0
            for p in posts:
                if save_published_to_copywriter(
                    db, user_id, p["text"], p["platform"], brand_id=brand.id
                ):
                    saved += 1
            brand_imported += saved
            total += saved
            brand_channels_info.append({
                "platform": ch.platform,
                "title": ch.title or ch.channel_id or "?",
                "imported": saved,
            })

        per_brand_summary.append({
            "brand_id": brand.id,
            "brand_name": brand.name or f"Бренд {brand.id}",
            "imported": brand_imported,
            "channels": brand_channels_info,
        })

    return {
        "imported": total,
        "per_brand": per_brand_summary,
        "skipped_brands": skipped,
        "errors": errors,
    }
