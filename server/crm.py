"""CRM-интеграции — Bitrix24, amoCRM, generic webhook.

Использование:
  from server.crm import dispatch_record_to_crm
  dispatch_record_to_crm(user_id, record_payload)
  # → найдёт все active CrmConnection юзера → POST в каждую

При успехе обновляет last_status / last_called_at / total_calls.
При ошибке — fail_count++. После 10 ошибок подряд auto-disable.

Безопасность: URL валидируется при создании (нет private/loopback).
Reuse существующая защита из server.routes.public_api._validate_webhook_url.
"""
import json
import logging
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import update

from server.db import db_session
from server.models import CrmConnection

log = logging.getLogger(__name__)

CRM_TIMEOUT_SEC = 10
MAX_FAIL_BEFORE_DISABLE = 10


# Дефолтный mapping для каждого provider'а — какие поля посылаем
_DEFAULT_BITRIX_MAPPING = {
    # наше поле → поле в Bitrix24 lead
    "customer_name":  "TITLE",
    "customer_phone": "PHONE",
    "customer_email": "EMAIL",
    "bot_name":       "SOURCE_DESCRIPTION",
    "record_type":    "TYPE_ID",  # LEAD/CONTACT/COMPANY
    "platform":       "UF_CRM_PLATFORM",
    "comment":        "COMMENTS",
}

_DEFAULT_AMOCRM_MAPPING = {
    "customer_name":  "name",
    "customer_phone": "PHONE",
    "customer_email": "EMAIL",
    "bot_name":       "source",
    "comment":        "notes",
}

_DEFAULT_GENERIC_MAPPING = {
    "customer_name":  "customer_name",
    "customer_phone": "customer_phone",
    "customer_email": "customer_email",
    "bot_name":       "bot_name",
    "record_type":    "record_type",
    "platform":       "platform",
    "comment":        "comment",
}


def _build_payload(provider: str, record: dict, mapping: dict | None) -> dict:
    """Маппит record-поля в формат конкретной CRM."""
    p = (provider or "").lower()
    if p == "bitrix24":
        defaults = _DEFAULT_BITRIX_MAPPING
    elif p == "amocrm":
        defaults = _DEFAULT_AMOCRM_MAPPING
    else:
        defaults = _DEFAULT_GENERIC_MAPPING
    use_mapping = mapping or defaults
    out = {}
    for src_key, target_key in use_mapping.items():
        v = record.get(src_key)
        if v is None or v == "":
            continue
        # Bitrix24 ожидает PHONE как массив объектов
        if p == "bitrix24" and target_key == "PHONE" and v:
            out[target_key] = [{"VALUE": str(v), "VALUE_TYPE": "WORK"}]
        elif p == "bitrix24" and target_key == "EMAIL" and v:
            out[target_key] = [{"VALUE": str(v), "VALUE_TYPE": "WORK"}]
        else:
            out[target_key] = v
    # Bitrix24 ждёт payload в виде {"fields": {...}}
    if p == "bitrix24":
        return {"fields": out}
    return out


def _post_to_crm(conn_id: int, record: dict) -> dict:
    """Синхронный POST на CRM webhook. Обновляет статус в БД.
    Возвращает {status, error, delivered}.
    """
    with db_session() as db:
        conn = db.query(CrmConnection).filter_by(id=conn_id).first()
        if not conn:
            return {"status": "error", "error": "connection gone", "delivered": False}
        url = conn.webhook_url
        provider = conn.provider
        mapping = None
        if conn.field_mapping_json:
            try:
                mapping = json.loads(conn.field_mapping_json)
            except Exception:
                mapping = None

    # Defense-in-depth: re-валидируем URL на dispatch-time. См. комментарий
    # в server/webhooks.py:_post_sync — DNS rebinding и пост-create мутации.
    from server.proposal_builder import validate_outbound_url
    try:
        validate_outbound_url(url, allow_http=True)
    except ValueError as e:
        log.warning(f"[crm {conn_id}] dispatch-time URL rejected: {e}")
        from sqlalchemy import update as _upd
        with db_session() as db:
            db.execute(
                _upd(CrmConnection)
                .where(CrmConnection.id == conn_id)
                .values(last_error=f"URL rejected: {e}"[:200], is_active=False)
            )
            db.commit()
        return {"status": "error", "error": f"URL rejected: {e}", "delivered": False}

    payload = _build_payload(provider, record, mapping)
    out = {"status": None, "error": None, "delivered": False}
    try:
        with httpx.Client(timeout=CRM_TIMEOUT_SEC, follow_redirects=False) as client:
            r = client.post(
                url,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "AI-Studio-Che-CRM/1.0",
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

    # Сохраняем статус — atomic UPDATE (без race на read-then-write при
    # одновременных POST'ах с разных воркеров: иначе fail_count мог застрять
    # ниже threshold и auto-disable не срабатывал).
    with db_session() as db:
        if out["delivered"]:
            db.execute(
                update(CrmConnection)
                .where(CrmConnection.id == conn_id)
                .values(
                    last_status=out["status"],
                    last_called_at=datetime.utcnow(),
                    last_error=None,
                    total_calls=CrmConnection.total_calls + 1,
                    fail_count=0,
                )
            )
        else:
            db.execute(
                update(CrmConnection)
                .where(CrmConnection.id == conn_id)
                .values(
                    last_status=out["status"],
                    last_called_at=datetime.utcnow(),
                    last_error=out["error"],
                    total_calls=CrmConnection.total_calls + 1,
                    fail_count=CrmConnection.fail_count + 1,
                )
            )
            db.execute(
                update(CrmConnection)
                .where(CrmConnection.id == conn_id)
                .where(CrmConnection.fail_count >= MAX_FAIL_BEFORE_DISABLE)
                .values(is_active=False)
            )
        db.commit()
    return out


def dispatch_record_to_crm(user_id: int, record: dict) -> int:
    """Найти все active CrmConnection юзера → отправить record.
    Fire-and-forget (через threading) — не блокирует caller'а.
    Возвращает количество соединений в которые отправили.
    """
    if not user_id or not isinstance(record, dict):
        return 0
    sent = 0
    try:
        with db_session() as db:
            rows = (db.query(CrmConnection)
                      .filter_by(user_id=user_id, is_active=True).all())
            ids = [c.id for c in rows]
        for cid in ids:
            try:
                # Fire-and-forget через thread (httpx sync)
                import threading
                threading.Thread(
                    target=_post_to_crm, args=(cid, record), daemon=True
                ).start()
                sent += 1
            except Exception as e:
                log.warning(f"[crm] dispatch thread {cid}: {e}")
    except Exception as e:
        log.warning(f"[crm] dispatch_record_to_crm: {e}")
    return sent


def test_connection(conn_id: int) -> dict:
    """Тестовый POST для проверки URL юзером."""
    test_record = {
        "customer_name": "Тестовая заявка",
        "customer_phone": "+79991234567",
        "customer_email": "test@example.com",
        "bot_name": "AI Студия Че",
        "comment": "Это тестовое событие из CRM-интеграции. Всё работает!",
        "platform": "test",
        "record_type": "lead",
    }
    return _post_to_crm(conn_id, test_record)
