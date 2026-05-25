"""VK API helpers: верификация + публикация на стену сообщества.

Для публикации нужен **community access_token** (тип «Сообщество»), который
юзер получает в VK Developers → Управление сообществом → Работа с API.

Картинки: 3-шаговый upload:
  1. photos.getWallUploadServer → upload_url
  2. POST file на upload_url (multipart)
  3. photos.saveWallPhoto → attachment id вида 'photo{owner_id}_{photo_id}'
"""
import logging
import os
from typing import Optional

import httpx

log = logging.getLogger(__name__)

VK_API_VERSION = "5.131"
VK_API_BASE = "https://api.vk.com/method"
HTTP = httpx.AsyncClient(timeout=60)


async def _vk_call(method: str, token: str, **params) -> dict:
    p = {"access_token": token, "v": VK_API_VERSION, **params}
    try:
        r = await HTTP.post(f"{VK_API_BASE}/{method}", data=p)
        return r.json() or {}
    except Exception as e:
        log.error(f"[VK] {method} HTTP error: {e}")
        return {"error": {"error_code": -1, "error_msg": str(e)}}


def _normalize_group_id(raw: str) -> Optional[int]:
    """Преобразовать '123', '-123', 'club123', 'public123' → 123 (положительное)."""
    s = (raw or "").strip().lstrip("@")
    if s.startswith(("club", "public")):
        s = s.replace("club", "", 1).replace("public", "", 1)
    try:
        n = abs(int(s))
        return n if n > 0 else None
    except ValueError:
        return None


async def verify_vk_community(token: str, channel_id: str) -> dict:
    """Проверить что token валиден и связан с сообществом channel_id.

    Возвращает {"ok": bool, "title": str?, "group_id": int?, "description": str?}.
    """
    gid = _normalize_group_id(channel_id)
    if not gid:
        return {"ok": False, "description": "Неверный формат community_id. Используй число (например 123456) или 'club123456'"}

    # groups.getById вернёт инфо
    r = await _vk_call("groups.getById", token, group_id=str(gid))
    if r.get("error"):
        return {"ok": False, "description": f"VK: {r['error'].get('error_msg', 'unknown error')}"}
    items = r.get("response") or []
    if isinstance(items, dict):
        items = items.get("groups") or []
    if not items:
        return {"ok": False, "description": "Сообщество не найдено или токен не имеет к нему доступа"}
    grp = items[0]
    return {"ok": True, "title": grp.get("name") or f"club{gid}", "group_id": gid}


async def _upload_photo_to_wall(token: str, group_id: int, photo_path_or_url: str) -> Optional[str]:
    """Загрузить картинку для стены. Возвращает attachment id 'photo{owner}_{id}' или None."""
    # 1. getWallUploadServer
    r = await _vk_call("photos.getWallUploadServer", token, group_id=group_id)
    if r.get("error"):
        log.error(f"[VK] getWallUploadServer: {r['error']}")
        return None
    upload_url = (r.get("response") or {}).get("upload_url")
    if not upload_url:
        return None

    # 2. POST file
    # Защита: SSRF на http(s) URL (нельзя fetch'нуть localhost / 169.254.169.254 / private),
    # path traversal на локальный путь (нельзя `/uploads/../../etc/passwd`).
    _MAX_IMG_BYTES = 15 * 1024 * 1024  # 15 MB cap
    try:
        if photo_path_or_url.startswith(("http://", "https://")):
            try:
                from server.security import validate_external_url
                validate_external_url(photo_path_or_url)
            except Exception as e:
                log.warning(f"[VK] SSRF blocked photo URL {photo_path_or_url[:80]}: {e}")
                return None
            img_r = await HTTP.get(photo_path_or_url, follow_redirects=False)
            if img_r.status_code != 200:
                log.warning(f"[VK] photo fetch HTTP {img_r.status_code}")
                return None
            img_bytes = img_r.content
            if len(img_bytes) > _MAX_IMG_BYTES:
                log.warning(f"[VK] photo too big: {len(img_bytes)} bytes")
                return None
            filename = "image.jpg"
        else:
            # /uploads/... — локальный. ВАЖНО: проверяем realpath чтобы '..' не
            # вырвался из base/uploads (иначе LFI на любой файл проекта).
            base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            uploads_root = os.path.realpath(os.path.join(base, "uploads"))
            candidate = os.path.realpath(os.path.join(base, photo_path_or_url.lstrip("/")))
            if not (candidate == uploads_root or candidate.startswith(uploads_root + os.sep)):
                log.warning(f"[VK] path traversal blocked: {photo_path_or_url}")
                return None
            if not os.path.exists(candidate):
                log.error(f"[VK] local photo not found: {candidate}")
                return None
            if os.path.getsize(candidate) > _MAX_IMG_BYTES:
                log.warning(f"[VK] local photo too big: {candidate}")
                return None
            with open(candidate, "rb") as f:
                img_bytes = f.read()
            filename = os.path.basename(candidate)

        upload_r = await HTTP.post(upload_url, files={"photo": (filename, img_bytes, "image/jpeg")})
        upload_data = upload_r.json()
    except Exception as e:
        log.error(f"[VK] photo upload HTTP error: {e}")
        return None

    # 3. saveWallPhoto
    save = await _vk_call(
        "photos.saveWallPhoto", token,
        group_id=group_id,
        photo=upload_data.get("photo"),
        server=upload_data.get("server"),
        hash=upload_data.get("hash"),
    )
    if save.get("error"):
        log.error(f"[VK] saveWallPhoto: {save['error']}")
        return None
    saved = (save.get("response") or [{}])[0]
    owner = saved.get("owner_id")
    pid = saved.get("id")
    if owner is None or pid is None:
        return None
    return f"photo{owner}_{pid}"


async def publish_to_vk_wall(token: str, channel_id: str,
                              text: str, media_url: Optional[str] = None) -> dict:
    """Опубликовать пост на стену сообщества.

    Возвращает {"ok": bool, "post_id": int?, "description": str?}.
    """
    gid = _normalize_group_id(channel_id)
    if not gid:
        return {"ok": False, "description": "Невалидный community_id"}

    params = {
        "owner_id": -gid,
        "from_group": 1,
        "message": text[:15000],   # VK лимит ~16k для wall.post
    }

    if media_url:
        attach = await _upload_photo_to_wall(token, gid, media_url)
        if attach:
            params["attachments"] = attach
        # Если upload упал — публикуем без картинки (не падаем целиком)

    r = await _vk_call("wall.post", token, **params)
    if r.get("error"):
        return {"ok": False, "description": f"VK: {r['error'].get('error_msg')}"}
    post_id = (r.get("response") or {}).get("post_id")
    return {"ok": True, "post_id": post_id}
