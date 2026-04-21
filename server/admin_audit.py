"""Helper для записи действий админа в audit-лог."""
import json
import logging
from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


def log_admin_action(
    db: Session,
    admin_user,
    action: str,
    target_type: str | None = None,
    target_id: Any = None,
    details: dict | None = None,
    request: Request | None = None,
) -> None:
    """Пишет запись в admin_audit_log. Не бросает исключение — если не смогли,
    просто логируем warning (audit-log не должен ломать основной flow)."""
    try:
        from server.models import AdminAuditLog
        ip = None
        if request is not None:
            try:
                from server.security import _get_client_ip
                ip = _get_client_ip(request)
            except Exception:
                pass
        db.add(AdminAuditLog(
            admin_id=admin_user.id,
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            details=json.dumps(details, ensure_ascii=False) if details else None,
            ip=ip,
        ))
        db.commit()
    except Exception as e:
        log.warning(f"audit-log write failed: {e}")
