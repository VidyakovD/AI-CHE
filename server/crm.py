"""CRM-интеграции — Bitrix24, amoCRM, generic webhook.

Использование:
  from server.crm import dispatch_record_to_crm
  dispatch_record_to_crm(user_id, record_payload)
  # → найдёт все active CrmConnection юзера → POST в каждую

При успехе обновляет last_status / last_called_at / total_calls.
При ошибке — fail_count++. После 10 ошибок подряд auto-disable.

Безопасность: URL валидируется при создании + re-валидируется на dispatch-time
через server.proposal_builder.validate_outbound_url (DNS-резолв ловит rebinding).

Общий HTTP-флоу (POST + atomic UPDATE + auto-disable) вынесен в
server._outbound.dispatch_outbound — этот модуль только маппит record-поля
в формат конкретной CRM и делегирует.
"""
import json
import logging

from server.db import db_session
from server.models import CrmConnection
from server._outbound import (
    dispatch_outbound,
    DEFAULT_TIMEOUT_SEC as CRM_TIMEOUT_SEC,
    DEFAULT_MAX_FAIL as MAX_FAIL_BEFORE_DISABLE,
)

log = logging.getLogger(__name__)


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

    payload = _build_payload(provider, record, mapping)
    result = dispatch_outbound(
        model_cls=CrmConnection,
        row_id=conn_id,
        url=url,
        json_body=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "AI-Studio-Che-CRM/1.0",
        },
        timeout_sec=CRM_TIMEOUT_SEC,
        max_fail=MAX_FAIL_BEFORE_DISABLE,
        log_prefix=f"crm {conn_id}",
    )
    return {
        "status": result.status if result.status is not None else "error",
        "error": result.error,
        "delivered": result.delivered,
    }


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
