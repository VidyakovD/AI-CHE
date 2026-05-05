"""Public API webhooks — доставка событий на пользовательские URL.

Поток:
  1. Где-то в коде происходит событие (КП открыт клиентом, заявка из бота...).
  2. Вызывается `dispatch_event(user_id, event_name, data)`.
  3. Хелпер находит все ApiWebhook с этим событием в `events` для юзера.
  4. Для каждого — асинхронный POST с JSON {event, timestamp, data} +
     заголовком `X-Aiche-Signature: sha256=<hex>` (HMAC-SHA256 по body bytes).
  5. На 5xx / network → fail_count++. После 10 фейлов is_active=False.
     Юзер увидит ошибку в кабинете.

Безопасность:
  - URL проходит whitelist (http/https, не private/loopback) при создании.
  - HMAC-подпись стандартная — юзер верифицирует в своём приложении.
  - Timeout 10 сек, body ≤ 1 МБ.

Простота: нет retry-очереди, нет broker'а — всё in-process через asyncio.
Если webhook упал — просто увидится в last_error, юзер сам разберётся.
"""
import asyncio
import hashlib
import hmac
import json
import logging
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import update

from server.db import db_session
from server.models import ApiWebhook

log = logging.getLogger(__name__)

WEBHOOK_TIMEOUT_SEC = 10
MAX_FAIL_BEFORE_DISABLE = 10


def _sign(secret: str, body: bytes) -> str:
    h = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={h}"


def _post_sync(webhook_id: int, payload: dict) -> dict:
    """Синхронный POST (для test-call'а). Возвращает {status, error}."""
    with db_session() as db:
        w = db.query(ApiWebhook).filter_by(id=webhook_id).first()
        if not w:
            return {"status": "error", "error": "webhook gone"}
        url = w.url
        secret = w.secret
    body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    signature = _sign(secret, body_bytes)
    out = {"status": None, "error": None, "delivered": False}
    try:
        with httpx.Client(timeout=WEBHOOK_TIMEOUT_SEC, follow_redirects=False) as client:
            r = client.post(
                url,
                content=body_bytes,
                headers={
                    "Content-Type": "application/json",
                    "X-Aiche-Signature": signature,
                    "X-Aiche-Event": payload.get("event", ""),
                    "User-Agent": "AI-Studio-Che-Webhook/1.0",
                },
            )
            out["status"] = r.status_code
            out["delivered"] = 200 <= r.status_code < 300
            if not out["delivered"]:
                out["error"] = f"HTTP {r.status_code}: {r.text[:200]}"
    except httpx.TimeoutException:
        out["error"] = "Timeout"
    except httpx.HTTPError as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"

    # Записываем в БД. fail_count и total_calls — atomic UPDATE
    # (без read-then-write race на multi-worker; иначе при одновременных
    # ошибках двух POST'ов counter мог застрять и auto-disable не сработать).
    with db_session() as db:
        if out["delivered"]:
            db.execute(
                update(ApiWebhook)
                .where(ApiWebhook.id == webhook_id)
                .values(
                    last_status=out["status"],
                    last_called_at=datetime.utcnow(),
                    last_error=None,
                    total_calls=ApiWebhook.total_calls + 1,
                    fail_count=0,
                )
            )
        else:
            db.execute(
                update(ApiWebhook)
                .where(ApiWebhook.id == webhook_id)
                .values(
                    last_status=out["status"],
                    last_called_at=datetime.utcnow(),
                    last_error=out["error"],
                    total_calls=ApiWebhook.total_calls + 1,
                    fail_count=ApiWebhook.fail_count + 1,
                )
            )
            # Auto-disable, если fail_count перешагнул threshold
            db.execute(
                update(ApiWebhook)
                .where(ApiWebhook.id == webhook_id)
                .where(ApiWebhook.fail_count >= MAX_FAIL_BEFORE_DISABLE)
                .values(is_active=False)
            )
        db.commit()
    return out


async def _post_async(webhook_id: int, payload: dict) -> dict:
    """Асинхронный POST. Используется при триггере событий."""
    return await asyncio.get_event_loop().run_in_executor(
        None, _post_sync, webhook_id, payload
    )


def deliver_webhook(webhook_id: int, payload: dict, sync: bool = False) -> dict:
    """Public-API: доставить payload на webhook.

    sync=True — блокирующий call (для test-эндпоинта). Возвращает результат.
    sync=False — fire-and-forget (для триггера событий из горячего пути).
    """
    if sync:
        return _post_sync(webhook_id, payload)
    # Fire-and-forget: создаём задачу но НЕ ждём
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(_post_async(webhook_id, payload))
        else:
            # В sync-контексте (например webhook handler) — крутим в потоке
            import threading
            threading.Thread(
                target=_post_sync, args=(webhook_id, payload), daemon=True
            ).start()
    except Exception as e:
        log.warning(f"[webhook] dispatch failed: {e}")
    return {"status": "queued"}


def dispatch_event(user_id: int, event: str, data: dict) -> int:
    """Найти все ApiWebhook у юзера с этим событием → дёрнуть.
    Возвращает количество отправленных webhook'ов.

    Это безопасно вызывать из любого horячего пути — fire-and-forget.
    """
    if not user_id or not event:
        return 0
    payload = {
        "event": event,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "data": data or {},
    }
    sent = 0
    try:
        with db_session() as db:
            rows = (db.query(ApiWebhook)
                      .filter_by(user_id=user_id, is_active=True).all())
            ids = [w.id for w in rows
                   if event in (w.events or "").split(",")]
        for wid in ids:
            deliver_webhook(wid, payload, sync=False)
            sent += 1
    except Exception as e:
        log.warning(f"[webhook] dispatch_event failed: {e}")
    return sent
