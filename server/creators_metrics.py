"""Fetch метрик опубликованных постов (TG/VK).

VK — официальный API wall.getById:
  Возвращает {views, likes, comments, reposts}. Доступно с любым токеном
  сообщества (тот же что use'м для публикации).

TG — Bot API не даёт views для channel-постов. Workaround: парсим
  https://t.me/<channel>/<msg_id> HTML preview — там виден counter views
  в `tgme_widget_message_views` блоке. Likes/comments в TG-каналах нет,
  только views.

Используется из:
  - server/cron/creators_metrics.py (раз в 6 часов для published-постов
    младше 30 дней)
  - server/routes/creators.py (manual refresh по кнопке «Обновить метрики»)
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import httpx

log = logging.getLogger("creators.metrics")


VK_API_BASE = "https://api.vk.com/method"
VK_API_VERSION = "5.131"


# ── VK: wall.getById → views/likes/comments/reposts ──────────────────────────


async def fetch_vk_post_stats(token: str, owner_id: str | int,
                              post_id: str | int) -> Optional[dict]:
    """Достать метрики поста через VK API wall.getById.

    owner_id — отрицательный для community (-12345). post_id — id поста.

    Возвращает {"views", "likes", "comments", "shares"} или None при ошибке.
    """
    if not token or not post_id:
        return None
    # VK wall.getById принимает posts=owner_id_post_id (через подчёркивание)
    # owner_id для community с минусом
    try:
        oid = int(owner_id)
    except (ValueError, TypeError):
        return None
    if oid > 0:
        oid = -oid  # community всегда отрицательный
    composite = f"{oid}_{post_id}"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{VK_API_BASE}/wall.getById", data={
                "access_token": token,
                "v": VK_API_VERSION,
                "posts": composite,
                "extended": 0,
            })
            data = r.json() or {}
    except Exception as e:
        log.warning(f"[vk-stats] fetch failed for {composite}: {type(e).__name__}: {e}")
        return None

    if data.get("error"):
        err_code = data["error"].get("error_code")
        # 100/15/201/212 — пост удалён / доступа нет, не retry'ить
        if err_code in (100, 15, 201, 212):
            log.info(f"[vk-stats] post {composite} unavailable: {data['error'].get('error_msg')}")
            return {"views": 0, "likes": 0, "comments": 0, "shares": 0, "deleted": True}
        log.warning(f"[vk-stats] api error {composite}: {data['error']}")
        return None

    items = data.get("response") or []
    if not items:
        return None
    post = items[0]
    return {
        "views":    int((post.get("views") or {}).get("count") or 0),
        "likes":    int((post.get("likes") or {}).get("count") or 0),
        "comments": int((post.get("comments") or {}).get("count") or 0),
        "shares":   int((post.get("reposts") or {}).get("count") or 0),
    }


# ── TG: парсинг t.me/<channel>/<msg_id> ──────────────────────────────────────


# Counter views в TG embed-preview:
#   <span class="tgme_widget_message_views">1.2K</span>
_TG_VIEWS_RE = re.compile(
    r'<span class="tgme_widget_message_views[^"]*">([^<]+)</span>',
    re.I
)


def _parse_tg_views_count(s: str) -> int:
    """«1.2K» → 1200, «5.3M» → 5300000, «123» → 123."""
    s = (s or "").strip().replace(",", ".").replace("\xa0", " ").replace(" ", "")
    if not s:
        return 0
    mult = 1
    if s.endswith("K") or s.endswith("k") or s.endswith("К"):
        mult = 1000
        s = s[:-1]
    elif s.endswith("M") or s.endswith("m") or s.endswith("М"):
        mult = 1_000_000
        s = s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return 0


async def fetch_tg_post_views(channel: str, message_id: str | int) -> Optional[dict]:
    """Достать views поста из TG-канала через web-preview.

    channel — @username или просто username (без @). Numeric -100... не
    работает (preview нужен public-username). Если канал private — None.

    Возвращает {"views": int} или None. Likes/comments в TG-каналах
    нет (форумы/reactions — отдельный платный feature).
    """
    if not channel or not message_id:
        return None
    username = str(channel).lstrip("@").lstrip("-")
    # Канал должен быть public с username, не numeric ID
    if not re.match(r"^[A-Za-z][A-Za-z0-9_]{3,32}$", username):
        return None

    url = f"https://t.me/{username}/{message_id}?embed=1&mode=tme"
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; AIche-Metrics/1.0)",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
    }
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            r = await client.get(url, headers=headers)
    except Exception as e:
        log.warning(f"[tg-stats] fetch failed {username}/{message_id}: {type(e).__name__}")
        return None
    if r.status_code != 200:
        return None
    m = _TG_VIEWS_RE.search(r.text)
    if not m:
        # Preview странички пустая или сообщение удалено
        return {"views": 0, "likes": 0, "comments": 0, "shares": 0}
    views = _parse_tg_views_count(m.group(1))
    return {"views": views, "likes": 0, "comments": 0, "shares": 0}


# ── Главная функция для cron / endpoint ─────────────────────────────────────


async def fetch_item_stats(item, channel_token: str) -> Optional[dict]:
    """Дёрнуть метрики для одного ContentItem.

    item.external_post_id + item.external_chat_id + item.platform → API call.
    Возвращает dict со stats_* полями или None если не fetch'ить.
    """
    if not item.external_post_id:
        return None
    if item.platform == "vk":
        return await fetch_vk_post_stats(channel_token, item.external_chat_id,
                                          item.external_post_id)
    elif item.platform == "tg":
        # Для TG channel_token (bot-token) не нужен — парсим публичный preview
        return await fetch_tg_post_views(item.external_chat_id, item.external_post_id)
    return None
