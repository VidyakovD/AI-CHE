"""
Yandex.Disk API helpers.
"""
import os, logging, httpx

log = logging.getLogger("ydisk")

BASE = "https://cloud-api.yandex.net/v1/disk"


def _headers(token: str | None = None) -> dict:
    token = token or os.getenv("YANDEX_DISK_TOKEN", "")
    return {"Authorization": f"OAuth {token}"}


async def yd_list_recent(token: str | None = None, limit: int = 30) -> list:
    """Список последних файлов (sort by -modified)."""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{BASE}/resources/files?sort=-modified&limit={limit}",
                        headers=_headers(token))
        if r.status_code != 200:
            log.error(f"[YD] list {r.status_code}: {r.text[:200]}")
            return []
        return r.json().get("items", [])


async def yd_upload(token: str | None, remote_path: str, local_path: str) -> dict:
    """Загрузить локальный файл на Я.Диск по пути remote_path."""
    if not os.path.exists(local_path):
        return {"error": "local file not found"}
    # 1. Получить upload URL
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.get(f"{BASE}/resources/upload",
                        params={"path": remote_path, "overwrite": "true"},
                        headers=_headers(token))
        if r.status_code not in (200, 201):
            return {"error": f"get upload URL: {r.status_code} {r.text[:200]}"}
        href = r.json().get("href")
        if not href:
            return {"error": "no upload URL"}
        # 2. PUT binary
        with open(local_path, "rb") as f:
            put = await c.put(href, content=f.read())
        if put.status_code not in (200, 201, 202):
            return {"error": f"PUT {put.status_code}: {put.text[:200]}"}
    return {"ok": True, "path": remote_path}


async def yd_download(token: str | None, remote_path: str, local_path: str) -> dict:
    """Скачать файл с Я.Диска."""
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.get(f"{BASE}/resources/download",
                        params={"path": remote_path},
                        headers=_headers(token))
        if r.status_code != 200:
            return {"error": f"get download URL: {r.status_code}"}
        href = r.json().get("href")
        if not href:
            return {"error": "no download URL"}
        dl = await c.get(href)
        if dl.status_code != 200:
            return {"error": f"download {dl.status_code}"}
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(dl.content)
    return {"ok": True, "local_path": local_path}
