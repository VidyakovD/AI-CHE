"""
Web Push API через VAPID.

Использование:
  - Браузер регистрирует подписку через PushManager.subscribe(applicationServerKey=VAPID_PUBLIC).
    JSON отправляется на /user/push/subscribe → создаётся PushSubscription.
  - При событиях вызывается push_to_user(user_id, title, body, url) — он
    делает pywebpush.webpush(...) для всех активных подписок юзера.
  - 410/404 ответы от push-сервера = подписка просрочена → удаляем.

VAPID ключи:
  - Public key — публичный, отдаётся фронту через /user/push/vapid-public.
  - Private key — секретный, в env VAPID_PRIVATE_KEY.
  - Если ключи не настроены → push выключен, /user/push/* возвращают 503.

Генерация ключей (один раз, локально):
  python -c "from py_vapid import Vapid; v = Vapid(); v.generate_keys(); v.save_key('private.pem'); print('PUBLIC:', v.public_key)"
"""
from __future__ import annotations
import os
import json
import logging

log = logging.getLogger("push")

VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "").strip()
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "").strip()
VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", "mailto:admin@aiche.ru").strip()


def is_configured() -> bool:
    return bool(VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY)


def push_to_user(user_id: int, title: str, body: str,
                  url: str | None = None, icon: str | None = None) -> int:
    """Шлёт push всем активным подписчикам юзера. Возвращает кол-во
    успешных доставок. Удаляет устаревшие подписки (410/404)."""
    if not is_configured():
        log.debug("[push] VAPID not configured — skipping")
        return 0
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        log.warning("[push] pywebpush not installed")
        return 0
    from server.db import db_session
    from server.models import PushSubscription

    payload = json.dumps({
        "title": title[:80],
        "body": body[:200],
        "url": url or "/",
        "icon": icon or "/logo-192.png",
    }, ensure_ascii=False)
    delivered = 0
    expired_ids: list[int] = []
    with db_session() as db:
        subs = db.query(PushSubscription).filter_by(user_id=user_id).all()
        for sub in subs:
            try:
                webpush(
                    subscription_info={
                        "endpoint": sub.endpoint,
                        "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                    },
                    data=payload,
                    vapid_private_key=VAPID_PRIVATE_KEY,
                    vapid_claims={"sub": VAPID_SUBJECT},
                )
                delivered += 1
            except WebPushException as e:
                code = getattr(e.response, "status_code", None) if e.response else None
                if code in (404, 410):
                    expired_ids.append(sub.id)
                    log.info(f"[push] expired sub user={user_id} id={sub.id}")
                else:
                    log.warning(f"[push] WebPushException user={user_id} sub={sub.id}: {code} {str(e)[:200]}")
            except Exception as e:
                log.warning(f"[push] error user={user_id} sub={sub.id}: {type(e).__name__}: {e}")
        if expired_ids:
            db.query(PushSubscription).filter(
                PushSubscription.id.in_(expired_ids)).delete(synchronize_session=False)
            db.commit()
    return delivered
