"""CRM-интеграции endpoints — управление подключениями + тест.

UI/API flow:
  - POST /crm/connections   — добавить (provider, webhook_url, name)
  - GET  /crm/connections   — список с метриками
  - DELETE /crm/connections/{id}
  - PUT /crm/connections/{id}/toggle {is_active}
  - POST /crm/connections/{id}/test — тестовый POST на webhook
"""
import json
import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from server.routes.deps import get_db, current_user
from server.models import User, CrmConnection

log = logging.getLogger(__name__)
router = APIRouter(prefix="/crm", tags=["crm"])


VALID_PROVIDERS = {
    "bitrix24": "Bitrix24 (входящий webhook crm.lead.add)",
    "amocrm":   "amoCRM (Webhooks → Свой URL)",
    "webhook":  "Generic webhook (любой URL принимающий JSON)",
}

MAX_PER_USER = 5


def _validate_url(url: str) -> str:
    """Только http(s), без private/loopback (SSRF).

    Делегирует общую логику в server.proposal_builder.validate_outbound_url
    которая делает DNS-резолв (защита от DNS rebinding) и покрывает
    IPv6/decimal IPv4 — раньше regex по lowered URL пропускал эти случаи.
    """
    from server.proposal_builder import validate_outbound_url
    try:
        return validate_outbound_url(url, allow_http=True)
    except ValueError as e:
        raise HTTPException(400, str(e))


def _serialize(c: CrmConnection) -> dict:
    return {
        "id": c.id,
        "provider": c.provider,
        "provider_label": VALID_PROVIDERS.get(c.provider, c.provider),
        "name": c.name,
        # webhook_url возвращаем замаскированный (показываем хост + первые символы пути)
        "webhook_url_preview": _mask_url(c.webhook_url),
        "is_active": bool(c.is_active),
        "last_status": c.last_status,
        "last_called_at": c.last_called_at.isoformat() + "Z" if c.last_called_at else None,
        "last_error": (c.last_error or "")[:200] or None,
        "fail_count": c.fail_count or 0,
        "total_calls": c.total_calls or 0,
        "field_mapping": (json.loads(c.field_mapping_json)
                           if c.field_mapping_json else None),
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


def _mask_url(url: str) -> str:
    """https://example.bitrix24.ru/rest/1/abc123token/ → https://example.bitrix24.ru/rest/1/***/"""
    if not url:
        return ""
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        host = p.netloc
        path = p.path or ""
        # Маскируем последние 2 сегмента path (там обычно токен)
        parts = [x for x in path.split("/") if x]
        if len(parts) >= 2:
            parts[-2] = "***"
        masked = "/" + "/".join(parts) if parts else "/"
        return f"{p.scheme}://{host}{masked}"
    except Exception:
        return url[:40] + "..." if len(url) > 40 else url


class _CrmCreateBody(BaseModel):
    provider: str
    webhook_url: str
    name: str | None = Field(None, max_length=100)
    field_mapping: dict | None = None


@router.post("/connections")
def crm_create(body: _CrmCreateBody, db: Session = Depends(get_db),
                user: User = Depends(current_user)):
    if not user.is_verified:
        raise HTTPException(403, "Подтвердите email")
    provider = (body.provider or "").lower().strip()
    if provider not in VALID_PROVIDERS:
        raise HTTPException(400,
            f"Provider должен быть из: {sorted(VALID_PROVIDERS.keys())}")
    cnt = db.query(CrmConnection).filter_by(user_id=user.id, is_active=True).count()
    if cnt >= MAX_PER_USER:
        raise HTTPException(409, f"Лимит {MAX_PER_USER} активных интеграций")
    url = _validate_url(body.webhook_url)
    mapping_json = None
    if body.field_mapping:
        if not isinstance(body.field_mapping, dict):
            raise HTTPException(400, "field_mapping должен быть объектом")
        # Защита от слишком большого mapping
        if len(json.dumps(body.field_mapping)) > 5000:
            raise HTTPException(413, "field_mapping слишком большой")
        mapping_json = json.dumps(body.field_mapping, ensure_ascii=False)

    c = CrmConnection(
        user_id=user.id,
        provider=provider,
        name=(body.name or VALID_PROVIDERS[provider])[:100],
        webhook_url=url,
        field_mapping_json=mapping_json,
        is_active=True,
    )
    db.add(c); db.commit(); db.refresh(c)
    try:
        from server.audit_log import log_action
        log_action("crm.connection_created", user_id=user.id,
                   target_type="crm_connection", target_id=str(c.id),
                   details={"provider": provider})
    except Exception:
        pass
    return _serialize(c)


@router.get("/connections")
def crm_list(db: Session = Depends(get_db),
              user: User = Depends(current_user)):
    rows = (db.query(CrmConnection).filter_by(user_id=user.id)
              .order_by(CrmConnection.id.desc()).all())
    return [_serialize(c) for c in rows]


@router.delete("/connections/{conn_id}")
def crm_delete(conn_id: int, db: Session = Depends(get_db),
                user: User = Depends(current_user)):
    c = db.query(CrmConnection).filter_by(id=conn_id, user_id=user.id).first()
    if not c:
        raise HTTPException(404, "Не найдено")
    db.delete(c); db.commit()
    return {"status": "deleted"}


class _ToggleBody(BaseModel):
    is_active: bool


@router.put("/connections/{conn_id}/toggle")
def crm_toggle(conn_id: int, body: _ToggleBody,
                db: Session = Depends(get_db),
                user: User = Depends(current_user)):
    c = db.query(CrmConnection).filter_by(id=conn_id, user_id=user.id).first()
    if not c:
        raise HTTPException(404, "Не найдено")
    c.is_active = bool(body.is_active)
    if c.is_active:
        c.fail_count = 0  # сброс счётчика при ре-активации
    db.commit()
    return {"status": "toggled", "is_active": c.is_active}


@router.post("/connections/{conn_id}/test")
def crm_test(conn_id: int, db: Session = Depends(get_db),
              user: User = Depends(current_user)):
    """Послать тестовую заявку на webhook чтобы проверить интеграцию."""
    c = db.query(CrmConnection).filter_by(id=conn_id, user_id=user.id).first()
    if not c:
        raise HTTPException(404, "Не найдено")
    from server.crm import test_connection
    return test_connection(conn_id)


@router.get("/providers")
def crm_providers():
    """Список поддерживаемых провайдеров — для UI dropdown."""
    return [{"id": k, "label": v} for k, v in VALID_PROVIDERS.items()]
